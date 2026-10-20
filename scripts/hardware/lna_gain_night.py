"""Overnight hunt for MAIN's two gain levels (~14.5 vs ~17.5 rms with a 50 ohm load at the LNA input, 2026-10-17/18).

One variable at a time, over ONE rtl_tcp connection that is read without pause (1 s blocks, every block logged):
  P1 warm-up       LNA on, untouched                      P2 retune      100 MHz for 2 s, back to F0
  P3 same-freq     re-send F0 only                        P4 bias        Bias-T off 3/30/120 s, then on
  P5 gain          re-send the same gain                  P6 full setup  every command, as the bench scripts do
  P7 reconnect     close the client, connect again        P8 long        LNA on, untouched (stability, spontaneous jumps)
  P9 restart       systemctl restart rtl_tcp (-T: the service powers the LNA itself)
Ends with Bias-T OFF. Temperatures come from the temperature logger's jsonl (no second reader on the 1-Wire bus).
Only with the LNA in the chain - the Bias-T is switched (never a 50 ohm load straight on the SDR).

Usage: .venv/bin/python scripts/hardware/lna_gain_night.py <output_dir> [long_hours=4]"""
import csv
import json
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HOST, PORT = "127.0.0.1", 1234
F0, FS, G0 = 1420405752, 2400000, 402
NFFT = 4096
OUT = Path(sys.argv[1])
LONG_H = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
OUT.mkdir(parents=True, exist_ok=True)


def now():
    return datetime.now(timezone.utc)


def log(*a):
    print(f"[{now().strftime('%H:%M:%S')}]", *a, flush=True)


class Rtl:
    def connect(self):
        for attempt in range(60):
            try:
                self.s = socket.create_connection((HOST, PORT), timeout=10)
                self.s.recv(12)
                return
            except OSError as exc:
                log("connect failed", exc, "- retry", attempt)
                time.sleep(5)
        raise RuntimeError("rtl_tcp unreachable for 5 min")

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def cmd(self, c, v):
        self.s.sendall(struct.pack(">BI", c, v & 0xFFFFFFFF))

    def block(self, seconds=1.0):
        n = int(FS * 2 * seconds) & ~1
        buf = bytearray(n); view = memoryview(buf); got = 0
        while got < n:
            k = self.s.recv_into(view[got:], min(1 << 20, n - got))
            if not k:
                raise RuntimeError("rtl_tcp closed")
            got += k
        x = np.frombuffer(buf, np.uint8).astype(np.float32) - 127.5
        return x[0::2] + 1j * x[1::2], x

    def full_setup(self, bias):
        self.cmd(0x02, FS); self.cmd(0x03, 1); self.cmd(0x04, G0); self.cmd(0x08, 0); self.cmd(0x0E, bias); self.cmd(0x01, F0)


rtl = Rtl()
rows = open(OUT / "power_1s.csv", "w", newline="")
w = csv.writer(rows)
w.writerow(["utc", "epoch", "phase", "cycle", "event", "bias", "power", "rms", "dc_i", "dc_q", "clip_frac"])
spectra, spec_meta = [], []
state = {"bias": 1, "phase": "", "cycle": 0, "event": ""}


def record(seconds=1, spectrum=False):
    """Read `seconds` 1 s blocks, log each one, optionally keep their mean spectrum. Returns the mean power."""
    acc, pw = None, []
    for _ in range(int(seconds)):
        z, x = rtl.block()
        p = float(np.mean(np.abs(z) ** 2))
        pw.append(p)
        clip = float(np.mean((x <= -127) | (x >= 127)))
        t = now()
        w.writerow([t.isoformat(timespec="milliseconds"), f"{t.timestamp():.3f}", state["phase"], state["cycle"], state["event"],
                    state["bias"], f"{p:.4f}", f"{np.sqrt(p):.4f}", f"{z.real.mean():.4f}", f"{z.imag.mean():.4f}", f"{clip:.2e}"])
        state["event"] = ""
        if spectrum:
            m = len(z) // NFFT
            s = np.mean(np.abs(np.fft.fft(z[:m * NFFT].reshape(m, NFFT) * np.hanning(NFFT), axis=1)) ** 2, axis=0)
            acc = s if acc is None else acc + s
    rows.flush()
    if spectrum and acc is not None:
        spectra.append(np.fft.fftshift(acc / seconds).astype(np.float32))
        spec_meta.append({"utc": now().isoformat(timespec="seconds"), "phase": state["phase"], "cycle": state["cycle"],
                          "power": float(np.mean(pw))})
        np.save(OUT / "spectra.npy", np.array(spectra))
        (OUT / "spectra_meta.json").write_text(json.dumps(spec_meta))
    return float(np.mean(pw))


def phase(name):
    state["phase"], state["cycle"] = name, 0
    log("=== phase", name)


def ev(name):
    state["event"] = name


def bias(v):
    rtl.cmd(0x0E, v); state["bias"] = v; ev(f"bias{v}")


def settle_and_measure(label, seconds=30):
    record(2)                                    # transient after the action, logged but kept out of the summary
    p = record(seconds - 10)
    record(8, spectrum=True)
    log(f"{label} cycle {state['cycle']}: rms {np.sqrt(p):.2f}")


summary = {"started_utc": now().isoformat(timespec="seconds"), "long_hours": LONG_H}
rtl.connect()
rtl.full_setup(1); state["bias"] = 1; ev("connect+setup")
try:
    phase("P1_warmup")
    for i in range(20):                          # 20 min untouched
        state["cycle"] = i
        p = record(50); record(10, spectrum=True)
        log(f"warm-up min {i}: rms {np.sqrt(p):.2f}")

    phase("P2_retune_100MHz")
    for i in range(30):
        state["cycle"] = i
        rtl.cmd(0x01, 100000000); ev("tune100M"); record(2)
        rtl.cmd(0x01, F0); ev("tuneF0")
        settle_and_measure("retune")

    phase("P3_same_freq")
    for i in range(20):
        state["cycle"] = i
        rtl.cmd(0x01, F0); ev("tuneF0_same")
        settle_and_measure("same-freq")

    phase("P4_bias_cycle")
    for i in range(30):
        state["cycle"] = i
        off_s = (3, 30, 120)[i % 3]
        bias(0); record(off_s)
        bias(1)
        settle_and_measure(f"bias off {off_s}s", 60)

    phase("P5_gain_resend")
    for i in range(15):
        state["cycle"] = i
        rtl.cmd(0x04, G0); ev("gain_same")
        settle_and_measure("gain")

    phase("P6_full_setup")
    for i in range(15):
        state["cycle"] = i
        rtl.full_setup(1); ev("full_setup")
        settle_and_measure("full setup")

    phase("P7_reconnect")
    for i in range(10):
        state["cycle"] = i
        rtl.close(); time.sleep(1); rtl.connect(); ev("reconnect")
        settle_and_measure("reconnect")

    phase("P8_long")
    for i in range(int(LONG_H * 60)):
        state["cycle"] = i
        p = record(50); record(10, spectrum=(i % 5 == 0))
        if i % 15 == 0:
            log(f"long min {i}: rms {np.sqrt(p):.2f}")

    phase("P9_rtl_tcp_restart")
    for i in range(8):
        state["cycle"] = i
        busy = subprocess.run(["pgrep", "-f", "capture.py|calibrate_reference_wizard|observation_orchestrator|alignment.py"],
                              capture_output=True, text=True).stdout.strip()
        if busy:                                 # restart gate: never restart under a running job
            log("restart skipped - jobs running:", busy.replace("\n", ",")); break
        rtl.close()
        r = subprocess.run(["sudo", "-n", "systemctl", "restart", "rtl_tcp.service"], capture_output=True, text=True)
        if r.returncode:
            log("restart failed:", r.stderr.strip()); break
        time.sleep(4); rtl.connect(); ev("restart")         # the service powers the LNA itself (-T); nothing re-sent
        state["bias"] = 1
        settle_and_measure("restart", 60)
finally:
    try:
        bias(0); record(2)
        log("Bias-T OFF")
    except Exception as exc:                     # the connection may be the thing that failed
        log("could not switch Bias-T off over this connection:", exc)
        try:
            rtl.close(); rtl.connect(); rtl.cmd(0x0E, 0); time.sleep(1); log("Bias-T OFF (new connection)")
        except Exception as exc2:
            log("Bias-T OFF FAILED:", exc2)
    summary["ended_utc"] = now().isoformat(timespec="seconds")
    (OUT / "run.json").write_text(json.dumps(summary, indent=1))
    rows.close()
    rtl.close()
    log("done")
