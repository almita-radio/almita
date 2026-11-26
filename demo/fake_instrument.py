"""ALMITA DEMO - SIMULATED instrument (operator 2026-11-25: the demo must carry the real OBSERVE / REDUCE / SCIENCE /
STATUS / EXPERIMENT pages). Runs ONLY inside the demo container (demo/docker), never next to the real instrument:
it binds the same ports the real capture Pi serves (rtl_tcp 1234, instrument agent 8099).

  rtl_tcp (1234)  - the real protocol: "RTL0" header, 5-byte commands (freq, sample rate, gain mode, gain, AGC,
                    Bias-T), and a real-time uint8 I/Q stream. Each command is acknowledged in the fake journal with
                    rtl_tcp's own wording ("set freq N"), which ALMITA's sdr_tuning reads back through the agent.
  agent (8099)    - instrument_agent.py's surface (/health, /run with the same allow-list): journalctl of
                    rtl_tcp.service, systemctl show/is-active, ps, /proc/<pid>/cmdline, DS18 w1_slave files.
  the sky         - read from INDI's Telescope Simulator (EQUATORIAL_EOD_COORD on 7624): inside the HI4PI tile CAR_E01
                    (l -1..21, |b| <= 11) the HI4PI spectrum through a 20 deg beam (demo/site/sky.json); elsewhere the
                    nearest tile spectrum scaled by the HI4PI column density through the same beam (hi4pi_expected); the
                    Sun as a continuum when it is inside the beam. LSRK -> topocentric for the current time and site.
  the receiver    - Tsys 120 K, eta 0.5, a band shape that rolls off beyond +/-1 MHz, a DC spike, gain from the
                    requested dB. Every number is a model; ALMITA's pages show SIMULATED.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import socket
import struct
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

HERE = Path(__file__).resolve().parent
C_KMS = 299792.458
F_REST = 1420405752.0
TSYS_K, ETA = 120.0, 0.5
SUN_TA_K = 15.0                      # the Sun's antenna temperature with the whole disc in a 20 deg beam (model)
BEAM_FWHM_DEG = 20.0
SITE = (-33.45, -70.67, 570.0)       # city level
BLOCK = 1 << 16
FAKE_PID = 4242
UNIT = "rtl_tcp.service"
STARTED = datetime.now(timezone.utc)
EXECSTART = "{ path=/usr/bin/stdbuf ; argv[]=/usr/bin/stdbuf -oL /usr/bin/rtl_tcp -a 0.0.0.0 -p 1234 -T ; }"
DS18 = {"28-082471f4e41b": 31.0, "28-2c5acd1e64ff": 27.5, "28-0000006fad9d": 22.0, "28-0000006f80a5": 21.0}


# ------------------------------------------------------------------ journal of the fake rtl_tcp.service
class Journal:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines: List[tuple] = []          # (seq, utc datetime, text)
        self.add("Found 1 device(s):")
        self.add("  0:  RTLSDRBlog, Blog V4, SN: 00000001 (SIMULATED)")
        self.add("activated bias-T on GPIO PIN 0")
        self.add("listening...")

    def add(self, text: str) -> None:
        with self.lock:
            self.lines.append((len(self.lines) + 1, datetime.now(timezone.utc), text))

    def cursor(self) -> str:
        with self.lock:
            return f"s=demo;i={len(self.lines)}"

    def query(self, args: List[str]) -> str:
        with self.lock:
            rows = list(self.lines)
        out_fmt = args[args.index("-o") + 1] if "-o" in args else "short"
        after = next((a.split("i=")[-1] for a in args if a.startswith("--after-cursor=")), None)
        if after is not None:
            rows = [r for r in rows if r[0] > int(after)]
        if "--since" in args:
            rows = rows                                             # the service started with this process
        if "-n" in args:
            n = int(args[args.index("-n") + 1])
            rows = rows[-n:] if n else []
        text = []
        for seq, t, line in rows:
            if out_fmt == "cat":
                text.append(line)
            else:
                text.append(f"{t.strftime('%Y-%m-%dT%H:%M:%S%z')} almita-demo rtl_tcp[{FAKE_PID}]: {line}")
        body = "\n".join(text) + ("\n" if text else "")
        if "--show-cursor" in args:
            body += f"-- cursor: {self.cursor()}\n"
        return body


JOURNAL = Journal()


# ------------------------------------------------------------------ the sky
class Sky:
    def __init__(self):
        d = json.loads((HERE / "site" / "sky.json").read_text())
        bm = next(b for b in d["beams"] if b["fwhm_deg"] == BEAM_FWHM_DEG)
        q = np.frombuffer(base64.b64decode(bm["spectra_u16_b64"]), dtype="<u2").astype(np.float32)
        self.l_grid, self.b_grid = np.array(d["l_grid"]), np.array(d["b_grid"])
        n = d["v_km_s"]["n"]
        self.v = d["v_km_s"]["start"] + d["v_km_s"]["step"] * np.arange(n)
        self.spec = (q / 65535.0 * bm["scale_K"]).reshape(self.b_grid.size, self.l_grid.size, n)
        self._nhi = None
        self._cache: Dict[tuple, tuple] = {}

    def _nhi_beam(self, ra_deg: float, dec_deg: float) -> Optional[float]:
        try:
            if self._nhi is None:
                import sys
                sys.path.insert(0, str(HERE.parent))
                import hi4pi_expected as he
                sky, vec, w = he._load_sky(he.DEFAULT_NHI_FITS, BEAM_FWHM_DEG)
                self._nhi = he._Beam(sky, vec, w, BEAM_FWHM_DEG)
            return float(self._nhi.at(ra_deg, dec_deg))
        except Exception:  # noqa: BLE001 - no map: no line outside the tile
            return None

    def spectrum_lsrk(self, ra_deg: float, dec_deg: float):
        """(v km/s LSRK, T_B K through the beam, where it came from)."""
        from astropy import units as u
        from astropy.coordinates import SkyCoord
        g = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg).galactic
        l, b = float(g.l.wrap_at(180 * u.deg).deg), float(g.b.deg)
        if 0 <= l <= 20 and -10 <= b <= 10:
            return self.v, self._tile(l, b), "HI4PI CAR_E01 through a 20 deg beam"
        # outside the tile: the spectrum of the nearest tile point (its SHAPE), scaled by the HI4PI column density
        # here over the column density there (its AMOUNT) - no hard edge at the tile border
        lc, bc = min(max(l, 0.0), 20.0), min(max(b, -10.0), 10.0)
        edge = self._tile(lc, bc)
        nhi, nhi_edge = self._nhi_beam(ra_deg, dec_deg), self._nhi_beam(*self._radec(lc, bc))
        if nhi and nhi_edge:
            return self.v, edge * (nhi / nhi_edge), "nearest HI4PI tile spectrum scaled by the HI4PI column density here"
        return self.v, edge, "nearest HI4PI tile spectrum (no column-density map to scale it)"

    def _tile(self, l: float, b: float) -> np.ndarray:
        """Bilinear on the 1-deg grid; (l, b) is clamped into the tile, so this never leaves it."""
        x = min(max(l - self.l_grid[0], 0.0), self.l_grid.size - 1.0001)
        y = min(max(b - self.b_grid[0], 0.0), self.b_grid.size - 1.0001)
        i, j = int(x), int(y)
        fx, fy = x - i, y - j
        return (self.spec[j, i] * (1 - fx) * (1 - fy) + self.spec[j, i + 1] * fx * (1 - fy)
                + self.spec[j + 1, i] * (1 - fx) * fy + self.spec[j + 1, i + 1] * fx * fy)

    @staticmethod
    def _radec(l: float, b: float):
        from astropy import units as u
        from astropy.coordinates import SkyCoord
        c = SkyCoord(l=l * u.deg, b=b * u.deg, frame="galactic").icrs
        return float(c.ra.deg), float(c.dec.deg)

    def at(self, ra_deg: float, dec_deg: float):
        """(LSRK shift km/s, spectrum, source, sun K) for now, cached per 0.5 deg and minute."""
        key = (round(ra_deg * 2) / 2, round(dec_deg * 2) / 2, int(time.time() // 60))
        if key in self._cache:
            return self._cache[key]
        from astropy import units as u
        from astropy.coordinates import FK4, EarthLocation, SkyCoord, get_body
        from astropy.time import Time
        t = Time.now()
        loc = EarthLocation(lat=SITE[0] * u.deg, lon=SITE[1] * u.deg, height=SITE[2] * u.m)
        c = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
        apex = SkyCoord(ra=18 * u.hourangle, dec=30 * u.deg, frame=FK4(equinox="B1900")).icrs
        try:
            bary = c.radial_velocity_correction(kind="barycentric", obstime=t, location=loc).to(u.km / u.s).value
        except Exception:  # noqa: BLE001 - offline IERS: the solar-motion part alone
            bary = 0.0
        shift = bary + 20.0 * float(np.dot(c.cartesian.xyz.value, apex.cartesian.xyz.value))
        v, s, src = self.spectrum_lsrk(ra_deg, dec_deg)
        try:
            sun = get_body("sun", t).icrs
            sep = float(c.separation(sun).deg)
        except Exception:  # noqa: BLE001
            sep = 180.0
        sun_k = SUN_TA_K * math.exp(-4 * math.log(2) * (sep / BEAM_FWHM_DEG) ** 2)
        out = (shift, (v, s), src, sun_k)
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[key] = out
        return out


# ------------------------------------------------------------------ mount (INDI Telescope Simulator)
class Mount:
    def __init__(self, host="127.0.0.1", port=7624, device="Telescope Simulator"):
        self.host, self.port, self.device = host, port, device
        self.ra_deg, self.dec_deg = 270.0, -20.0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        pat = re.compile(r'<oneNumber\s+name="(RA|DEC)"[^>]*>\s*([-+0-9.eE]+)\s*</oneNumber>')
        while True:
            try:
                with socket.create_connection((self.host, self.port), timeout=10) as s:
                    s.sendall(f'<getProperties version="1.7" device="{self.device}" name="EQUATORIAL_EOD_COORD"/>'.encode())
                    s.settimeout(30)
                    buf = ""
                    while True:
                        chunk = s.recv(65536).decode(errors="replace")
                        if not chunk:
                            break
                        buf = (buf + chunk)[-200000:]
                        for m in re.finditer(r"<(def|set)NumberVector[^>]*EQUATORIAL_EOD_COORD.*?</\1NumberVector>", buf, re.S):
                            vals = dict((k, float(v)) for k, v in pat.findall(m.group(0)))
                            if "RA" in vals and "DEC" in vals:
                                self.ra_deg, self.dec_deg = vals["RA"] * 15.0, vals["DEC"]
                        buf = buf[buf.rfind("NumberVector>") + 13:] if "NumberVector>" in buf else buf
            except OSError:
                time.sleep(2)


# ------------------------------------------------------------------ rtl_tcp
class RtlTcp:
    def __init__(self, sky: Sky, mount: Mount, port=1234):
        self.sky, self.mount, self.port = sky, mount, port
        self.freq, self.rate, self.gain_tenths, self.manual = int(F_REST), 2400000, 402, True
        self.rng = np.random.default_rng()

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))          # where the real notebook forwarder listens (preflight checks it)
        srv.listen(1)
        while True:
            conn, addr = srv.accept()
            JOURNAL.add(f"client accepted! {addr[0]} {addr[1]}")
            self._client(conn)
            JOURNAL.add("all threads dead..")
            JOURNAL.add("listening...")

    def _commands(self, conn, stop):
        buf = b""
        while not stop.is_set():
            try:
                chunk = conn.recv(64)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 5:
                cmd, val = struct.unpack(">BI", buf[:5])
                buf = buf[5:]
                if cmd == 0x01:
                    self.freq = val
                    JOURNAL.add(f"set freq {val}")
                elif cmd == 0x02:
                    self.rate = val
                    JOURNAL.add(f"set sample rate {val}")
                elif cmd == 0x03:
                    self.manual = bool(val)
                    JOURNAL.add(f"set gain mode {val}")
                elif cmd == 0x04:
                    self.gain_tenths = val
                    JOURNAL.add(f"set gain {val}")
                elif cmd == 0x08:
                    JOURNAL.add(f"set agc mode {val}")
                elif cmd == 0x0E:
                    JOURNAL.add(f"set bias tee {val}")
                else:
                    JOURNAL.add(f"set command 0x{cmd:02x} {val}")
        stop.set()

    def _block(self) -> bytes:
        rate, f0 = float(self.rate), float(self.freq)
        bb = np.fft.fftfreq(BLOCK, 1.0 / rate)
        shape = (1 + 0.12 * np.sin(2 * np.pi * bb / 9e5 + 0.7)) * np.exp(-0.5 * (bb / (0.46 * rate)) ** 4)
        shift, (v, t_k), _, sun_k = self.sky.at(self.mount.ra_deg, self.mount.dec_deg)
        v_lsrk = C_KMS * (1 - (f0 + bb) / F_REST) + shift
        line = np.interp(v_lsrk, v, t_k, left=0.0, right=0.0)
        sky = 1.0 + (ETA * line + sun_k) / TSYS_K
        w = self.rng.standard_normal(BLOCK) + 1j * self.rng.standard_normal(BLOCK)
        z = np.fft.ifft(np.fft.fft(w) * np.sqrt(shape * sky))
        amp = 9.0 * 10 ** ((self.gain_tenths / 10.0 - 40.2) / 20.0)
        z = z * amp + 0.6 * amp * 0.05                                 # a small DC offset -> the LO spike
        out = np.empty(2 * BLOCK, np.uint8)
        out[0::2] = np.clip(np.round(z.real + 127.5), 0, 255)
        out[1::2] = np.clip(np.round(z.imag + 127.5), 0, 255)
        return out.tobytes()

    def _client(self, conn):
        stop = threading.Event()
        try:
            conn.sendall(b"RTL0" + struct.pack(">II", 6, 29))             # tuner 6 = R828D (V4)
            threading.Thread(target=self._commands, args=(conn, stop), daemon=True).start()
            t0, sent = time.monotonic(), 0
            while not stop.is_set():
                data = self._block()
                conn.sendall(data)
                sent += BLOCK
                ahead = sent / float(self.rate) - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(ahead)
        except OSError:
            pass
        except Exception as exc:  # noqa: BLE001 - a sky-model error ends this client, never the server
            JOURNAL.add(f"SIMULATED sky error: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
        finally:
            stop.set()
            try:
                conn.close()
            except OSError:
                pass


# ------------------------------------------------------------------ the agent
def run_command(argv: List[str]) -> dict:
    def ok(text: str, rc: int = 0) -> dict:
        return {"returncode": rc, "stdout_b64": base64.b64encode(text.encode()).decode(), "stderr_b64": ""}
    if argv[0] == "journalctl":
        if "-k" in argv:
            return ok("")
        return ok(JOURNAL.query(argv[1:]))
    if argv[0] == "systemctl":
        if argv[1] == "is-active":
            return ok("active\n")
        prop = argv[3]
        return ok({"ExecStart": EXECSTART, "MainPID": str(FAKE_PID), "ActiveState": "active",
                   "ActiveEnterTimestamp": STARTED.strftime("%a %Y-%m-%d %H:%M:%S UTC")}.get(prop, "") + "\n")
    if argv[0] == "ps":
        et = int((datetime.now(timezone.utc) - STARTED).total_seconds())
        rows = ["    PID ELAPSED COMMAND" if "etimes" in argv[2] else "    PID COMMAND",
                f"   {FAKE_PID} {et} /usr/bin/rtl_tcp -a 0.0.0.0 -p 1234 -T" if "etimes" in argv[2]
                else f"   {FAKE_PID} /usr/bin/rtl_tcp -a 0.0.0.0 -p 1234 -T"]
        return ok("\n".join(rows) + "\n")
    if argv[0] == "cat":
        p = argv[1]
        if p.endswith("/cmdline"):
            return ok("/usr/bin/rtl_tcp\0-a\x000.0.0.0\0-p\x001234\0-T\0")
        if p.endswith("/environ"):
            return ok("PATH=/usr/bin\0")
        m = re.search(r"(28-[0-9a-f]{12})", p)
        if m and m.group(1) in DS18:
            base = DS18[m.group(1)] + 0.6 * math.sin(time.time() / 900.0 + len(m.group(1)))
            t = int(round(base * 1000))
            return ok(f"72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t={t}\n")
        return ok("", 1)
    return ok("", 1)


class Agent(BaseHTTPRequestHandler):
    server_version = "almita-instrument-agent/1 (SIMULATED)"

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._send(200, {"agent": "almita-instrument-agent", "hostname": "almita-demo (SIMULATED)",
                                    "utc": datetime.now(timezone.utc).isoformat(), "rtl_tcp_service": "active",
                                    "rtlsdr_serials": ["00000001"], "ds18_ids": sorted(DS18), "nanovna": {"present": False, "devices": [], "detail": "no NanoVNA on the capture Pi's USB (SIMULATED)"},
                                    "cpu_temp_c": 48.0, "simulated": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/run":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(min(length, 65536)).decode() or "{}")
        argv = body.get("argv") or []
        import sys
        sys.path.insert(0, str(HERE.parent))
        import instrument_agent
        if not instrument_agent.allowed(argv):
            return self._send(403, {"error": "command not allowed", "argv": argv})
        return self._send(200, run_command(list(argv)))

    def log_message(self, fmt, *args):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rtl-port", type=int, default=1234)
    ap.add_argument("--agent-port", type=int, default=8099)
    ap.add_argument("--indi-port", type=int, default=7624)
    a = ap.parse_args(argv)
    if os.environ.get("ALMITA_DEMO") != "1":
        raise SystemExit("refusing to run outside the demo container (set ALMITA_DEMO=1): this binds the instrument ports")
    sky = Sky()
    mount = Mount(port=a.indi_port)
    srv = ThreadingHTTPServer(("0.0.0.0", a.agent_port), Agent)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"SIMULATED instrument: rtl_tcp :{a.rtl_port}, agent :{a.agent_port}, mount from INDI :{a.indi_port}", flush=True)
    RtlTcp(sky, mount, a.rtl_port).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
