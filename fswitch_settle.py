"""How long must fswitch.py throw away after each retune? Measured, not guessed (operator 2026-11-09: "haz todos los
ejercicios necesarios para calcular esa pausa a la perfección").

One continuous capture per trial; part-way through it the frequency command is sent to rtl_tcp while the stream keeps
flowing, and the byte count received at that instant is recorded. Everything received before that byte was produced
before the command, so it is pure LO-old; the end of the capture is pure LO-new. Each 1-2 ms block in between is
projected onto the difference (new - old) of the two reference spectra (log power, usable band, DC excluded):
x = 0 means "still the old LO", x = 1 "the new LO, settled". Also tracked: total power against the new reference (a
PLL glitch or a dropout shows there first).

  step_ms     - per trial, from the command to the step where the new LO's samples start: the bytes that were
                already in flight (tuner, USB, rtl_tcp queue, TCP, forwarder), found by a least-squares step fit;
  tail        - all trials aligned at their own step and averaged (noise down by sqrt(n)): any slow settling after
                the step (PLL creeping, filter ringing) or any early start before it shows there.

Control trials send the SAME frequency again: they must show nothing (no glitch from the command itself).
The pause fswitch.py needs = (worst step + measured tail) x SAFETY, rounded up to 10 ms, never below FLOOR_S.
Nothing in capture.py changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import struct
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

import fswitch as F

NFFT_FAST = 256            # 0.107 ms per frame at 2.4 MS/s
BLOCK_FRAMES = 8           # one block = 8 frames = 0.85 ms
SIGMA = 5.0                # "outside the noise band" = beyond 5 sigma of the new LO's own blocks
SAFETY = 1.5               # recommended pause = worst settled time x 1.5, rounded up to 10 ms
FLOOR_S = 0.05             # never below 50 ms whatever the measurement says


def frame_spectra(raw: np.ndarray, nfft: int = NFFT_FAST) -> np.ndarray:
    """|FFT|^2 (Hann) of consecutive nfft-sample frames of interleaved uint8 I/Q, fftshifted. Shape (frames, nfft)."""
    x = raw.astype(np.float32) - 127.5
    z = x[0::2] + 1j * x[1::2]
    k = len(z) // nfft
    win = np.hanning(nfft).astype(np.float32)
    return np.fft.fftshift(np.abs(np.fft.fft(z[:k * nfft].reshape(k, nfft) * win, axis=1)) ** 2, axes=1)


def transition_profile(raw: np.ndarray, cmd_byte: int, sample_rate: float, tail_s: float = 0.3,
                       nfft: int = NFFT_FAST, block: int = BLOCK_FRAMES, control: bool = False) -> Dict[str, Any]:
    """x(t) and total power around one retune sent when cmd_byte bytes had been received (see the module doc)."""
    P = frame_spectra(raw, nfft)
    nb = P.shape[0] // block
    P = P[:nb * block].reshape(nb, block, nfft).mean(axis=1)            # blocks
    t_block = (np.arange(nb) + 0.5) * block * nfft / sample_rate         # block centre, s from capture start
    t_cmd = (cmd_byte // 2) / sample_rate
    bb = F.baseband_hz(nfft, sample_rate)
    band = (np.abs(bb) <= F.USABLE_HZ) & (np.abs(bb) >= 3 * sample_rate / nfft)
    L = np.log(P[:, band])
    before = np.flatnonzero(t_block + 0.5 * block * nfft / sample_rate <= t_cmd)   # blocks wholly before the command
    after = np.flatnonzero(t_block >= t_block[-1] - tail_s)
    if before.size < 20 or after.size < 20:
        raise ValueError("capture too short around the command")
    a, b = L[before].mean(axis=0), L[after].mean(axis=0)
    pw = P[:, band].sum(axis=1)
    p_rel = pw / pw[after].mean() - 1.0
    d = b - a
    dd = float(d @ d)
    if control or dd == 0:
        # same frequency again: there is no "new LO"; distance from the reference itself, in its own noise units
        x = (L - b) @ np.ones(band.sum()) / band.sum()
    else:
        x = (L - a) @ d / dd
    ref = x[after]
    mu, sd = float(ref.mean()), float(ref.std()) or 1e-9
    t_rel = t_block - t_cmd
    half_block_ms = 0.5 * block * nfft / sample_rate * 1e3
    win = np.flatnonzero((t_rel > -0.1) & (t_rel < 0.6))         # the step is searched from 100 ms before to 600 after
    step_ms = None
    if not control and dd > 0 and win.size > 40:
        # the step: one cut that best splits the window into "old" and "new" (least squares on x and on power, each
        # in its own noise units). Resolution one block; the whole curve is kept for the averaged tail check.
        q = (pw / pw[before].mean() - 1.0)
        q_step = float(q[after].mean() - q[before].mean())
        sig = [x[win] / sd]
        if abs(q_step) > SIGMA * (float(q[after].std()) or 1e-9):
            sig.append(q[win] / (float(q[after].std()) or 1e-9))
        best, k_best = np.inf, None
        for k in range(5, win.size - 5):
            sse = sum(float(((y[:k] - y[:k].mean()) ** 2).sum() + ((y[k:] - y[k:].mean()) ** 2).sum()) for y in sig)
            if sse < best:
                best, k_best = sse, k
        step_ms = float(t_rel[win[k_best]] * 1e3 - half_block_ms)
    return {
        "t_ms": t_rel * 1e3, "x": x, "p_rel": p_rel,
        "step_ms": step_ms,                                      # when the new LO's samples start arriving
        "step_size": float(np.sqrt(dd / band.sum())),           # rms log-power difference new vs old LO
        "noise_x": sd, "x_new_mean": mu, "block_ms": block * nfft / sample_rate * 1e3, "control": control,
        "control_max_dev_sigma": float(np.max(np.abs(x[t_rel > 0] - mu)) / sd) if control else None,
    }


def aligned_tail(trials: List[Dict[str, Any]], pre_ms: float = 20.0, post_ms: float = 150.0,
                 bin_ms: float = 2.0, nsig: float = 4.0) -> Dict[str, Any]:
    """All switch trials aligned at their own step and averaged, in shape (x) and in total power: any slow settling
    after the step (a PLL creeping in, a filter ringing) leaves a mean that has not reached its final level yet; a
    relock dip or an early partial change shows before the step. Bins beyond nsig standard errors count, in pairs of neighbours.
    settled_after_step_ms = end of the last such bin after the step; early_start_ms = how far before the step the
    first such bin starts (already inside the step time, so already covered by it)."""
    edges = np.arange(-pre_ms, post_ms + bin_ms, bin_ms)
    centres = edges[:-1] + bin_ms / 2
    chans = {"x": [], "p": []}
    for t in trials:
        if t.get("step_ms") is None:
            continue
        idx = np.digitize(np.asarray(t["t_ms"]) - t["step_ms"], edges) - 1
        for key, src in (("x", "x"), ("p", "p_rel")):
            y = np.asarray(t[src])
            chans[key].append([float(y[idx == i].mean()) if np.any(idx == i) else np.nan for i in range(len(edges) - 1)])
    n = len(chans["x"])
    if not n:
        return {"n": 0}
    out = {"n": n, "centres_ms": centres}
    after_ms, early_ms = 0.0, 0.0
    for key, rows in chans.items():
        m = np.array(rows)
        mean = np.nanmean(m, axis=0)
        se = np.maximum(np.nanstd(m, axis=0) / np.sqrt(np.sum(np.isfinite(m), axis=0)), 1e-9)
        final, start = np.nanmean(mean[centres > post_ms * 0.5]), np.nanmean(mean[centres < -pre_ms * 0.5])
        # two neighbouring bins must agree: with ~12-40 trials per bin a lone 4-sigma bin is a fluke (t tails),
        # while any real settling lasts several 2 ms bins
        dev_a, dev_b = np.abs(mean - final) > nsig * se, np.abs(mean - start) > nsig * se
        def paired(d):
            nb = np.zeros_like(d)
            nb[1:] |= d[:-1]
            nb[:-1] |= d[1:]
            return d & nb
        pair_a, pair_b = paired(dev_a), paired(dev_b)
        off = np.flatnonzero((centres > 0) & pair_a)
        lead = np.flatnonzero((centres < 0) & pair_b)
        if off.size:
            after_ms = max(after_ms, float(centres[off[-1]] + bin_ms / 2))
        if lead.size:
            early_ms = max(early_ms, float(-centres[lead[0]] + bin_ms / 2))
        out[key] = {"mean": mean, "se": se, "final": float(final)}
    out.update(final=out["x"]["final"], mean=out["x"]["mean"], se=out["x"]["se"],
               settled_after_step_ms=after_ms, early_start_ms=early_ms)
    return out


def recommend(settled_ms: List[float]) -> float:
    """Pause in seconds: worst time-to-settled (step + tail) x SAFETY, rounded up to 10 ms, never below FLOOR_S."""
    worst = max(settled_ms) if settled_ms else 0.0
    return max(FLOOR_S, float(np.ceil(worst * SAFETY / 10.0) * 10.0) / 1e3)


async def measure(trials: int, controls: int, capture_s: float, switch_at_s: float, offset_hz: float,
                  gain_db: Optional[float], host: str = "127.0.0.1", port: int = 1234, progress=print) -> Dict[str, Any]:
    import sdr_tuning
    from sdr_capture import SDRCapture, read_hdf5_iq_components
    import h5py
    operating = sdr_tuning.operating_config()
    f0 = int(operating["center_frequency_hz"])
    rate = int(operating["sample_rate_hz"])
    gain = float(gain_db if gain_db is not None else operating.get("gain_db", 40.2))
    f_a, f_b = int(round(f0 - offset_hz)), int(round(f0 + offset_hz))
    ack = sdr_tuning.JournalAckSource()
    fd, h5 = tempfile.mkstemp(prefix="almita_fsettle_", suffix=".h5", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
    os.close(fd)
    sdr = SDRCapture("network", host, port, verbose=False)
    sdr.compact_console = True
    # the plan: real retunes alternating A->B / B->A, with "same frequency again" controls spread among them
    plan = ["switch"] * trials
    for i in range(controls):
        plan.insert(int((i + 1) * len(plan) / (controls + 1)), "control")
    trials_out, current = [], f_a
    try:
        await sdr.connect()
        tuning = await sdr_tuning.tune_explicitly(sdr, f_a, rate, gain, operating=operating)
        if not str(tuning.get("rtl_agc", "")).startswith("off"):
            raise RuntimeError(f"RTL AGC not confirmed off: {tuning.get('rtl_agc')}")
        ack.mark()
        await asyncio.sleep(1.0)                                   # the initial tune itself settles well before trial 1
        for n, kind in enumerate(plan):
            target = current if kind == "control" else (f_b if current == f_a else f_a)
            await sdr.flush_buffer()
            task = asyncio.create_task(sdr.capture(capture_s, h5, rate, {"purpose": "fswitch_settle"}))
            want = int(switch_at_s * rate) * 2
            while True:                                            # wait in the stream, not on the wall clock
                await asyncio.sleep(0.001)
                with sdr._consumer_condition:                      # measurement tool: reads the consumer's own count
                    req = sdr._active_capture
                    got = req["received"] if req else None
                if got is not None and got >= want:
                    break
                if task.done():
                    raise RuntimeError("capture ended before the switch point")
            with sdr._consumer_condition:
                cmd_byte = sdr._active_capture["received"]
            t_send = time.monotonic()
            sdr.socket.sendall(struct.pack(">BI", 0x01, target))
            send_ms = (time.monotonic() - t_send) * 1e3
            sdr.current_frequency = target
            await task
            with h5py.File(h5, "r") as f:
                i_, q_ = read_hdf5_iq_components(f)
            raw = np.empty(i_.size * 2, dtype=np.uint8)
            raw[0::2], raw[1::2] = i_, q_
            prof = transition_profile(raw, cmd_byte, rate, control=(kind == "control"))
            prof.update(kind=kind, from_hz=current, to_hz=target, cmd_byte=int(cmd_byte), send_ms=send_ms,
                        adc_rms=float(np.std(raw.astype(np.float32) - 127.5)))
            trials_out.append(prof)
            progress(f"trial {n + 1}/{len(plan)} {kind:7s} {current/1e6:.4f}->{target/1e6:.4f} MHz: "
                     + (f"step at {prof['step_ms']:.1f} ms" if prof['step_ms'] is not None else
                      f"control, max deviation {prof['control_max_dev_sigma']:.1f} sigma"))
            current = target
        await asyncio.sleep(0.5)
        lines = ack.lines()
    finally:
        await sdr.close()
        try:
            os.unlink(h5)
        except OSError:
            pass
    seen = {int(m.group(1)) for ln in lines for m in [re.search(r"\bset freq (\d+)\b", ln)] if m}
    return {"trials": trials_out, "f_a_hz": f_a, "f_b_hz": f_b, "sample_rate_hz": rate, "gain_db": gain,
            "capture_s": capture_s, "switch_at_s": switch_at_s, "tuning": tuning,
            "lo_acknowledged": {"f_a": f_a in seen, "f_b": f_b in seen},
            "pll_not_locked_count": sdr_tuning.parse_acks(lines).get("pll_not_locked_count")}


def summarise(run: Dict[str, Any]) -> Dict[str, Any]:
    sw = [t for t in run["trials"] if t["kind"] == "switch"]
    ct = [t for t in run["trials"] if t["kind"] == "control"]
    steps = [t["step_ms"] for t in sw if t["step_ms"] is not None]
    tail = aligned_tail(sw)
    after = tail.get("settled_after_step_ms", 0.0)
    return {
        "n_switch": len(sw), "n_control": len(ct),
        "step_ms": {"min": min(steps, default=None), "median": float(np.median(steps)) if steps else None,
                    "max": max(steps, default=None), "all": [round(v, 1) for v in steps]},
        "tail_settled_after_step_ms": after, "tail_early_start_ms": tail.get("early_start_ms"),
        "tail_final_level": tail.get("final"),
        "control_max_dev_sigma": [round(t["control_max_dev_sigma"], 1) for t in ct],
        "switch_missed": len(sw) - len(steps),
        "recommended_settle_s": recommend([v + after for v in steps]),
        "block_ms": sw[0]["block_ms"] if sw else None,
        "step_size_log": float(np.median([t["step_size"] for t in sw])) if sw else None,
        "lo_acknowledged": run["lo_acknowledged"], "pll_not_locked_count": run["pll_not_locked_count"],
        "adc_rms_range": [min(t["adc_rms"] for t in run["trials"]), max(t["adc_rms"] for t in run["trials"])],
        "_tail": tail,
    }


def plot(run: Dict[str, Any], summ: Dict[str, Any], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(3, 1, figsize=(11, 11))
    for t in run["trials"]:
        c = "0.6" if t["kind"] == "control" else ("C0" if t["to_hz"] > t["from_hz"] else "C3")
        ax[0].plot(t["t_ms"], t["x"], color=c, lw=.7)
        ax[1].plot(t["t_ms"], t["x"], color=c, lw=.7)
        ax[2].plot(t["t_ms"], t["p_rel"] * 100, color=c, lw=.7)
    rec = summ["recommended_settle_s"] * 1e3
    for a in ax:
        a.axvline(0, color="k", lw=.8)
        a.axvline(rec, color="g", ls="--", lw=1)
    ax[0].set_xlim(-50, max(250.0, rec * 1.3))
    ax[0].set_title(f"x = 0 old LO, 1 new LO (blue A→B, red B→A, grey = same frequency again) · step after the "
                    f"command: median {summ['step_ms']['median']:.0f} ms, worst {summ['step_ms']['max']:.0f} ms · "
                    f"green = recommended pause {rec:.0f} ms")
    ax[1].cla()
    tl = summ["_tail"]
    if tl.get("n"):
        ax[1].errorbar(tl["centres_ms"], tl["mean"], yerr=3 * tl["se"], fmt="o-", ms=3, color="C2", lw=.8)
        ax[1].axhline(tl["final"], color="0.5", lw=.6)
        ax[1].axhline(0, color="0.5", lw=.6)
        ax[1].axvline(0, color="k", lw=.8)
    ax[1].set_xlabel("ms after each trial's own step")
    ax[1].set_title(f"All {tl.get('n', 0)} switches aligned at their step, mean ± 3 s.e.: settles "
                    f"{summ['tail_settled_after_step_ms']:.0f} ms after the step, starts early "
                    f"{summ['tail_early_start_ms']:.0f} ms")
    ax[2].set_xlim(-20, max(160.0, rec * 1.2))
    ax[2].set_title("Total power against the new LO's own level [%] (glitches/dropouts show here)")
    ax[2].set_xlabel("ms after the frequency command was sent")
    for a in ax[:2]:
        a.set_ylabel("x")
    ax[2].set_ylabel("%")
    fig.suptitle(f"ALMITA retune settling · {run['f_a_hz']/1e6:.4f} ↔ {run['f_b_hz']/1e6:.4f} MHz · "
                 f"{summ['n_switch']} switches + {summ['n_control']} controls · block {summ['block_ms']:.2f} ms")
    plt.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--controls", type=int, default=4)
    ap.add_argument("--capture", type=float, default=1.6, help="seconds per trial capture")
    ap.add_argument("--switch-at", type=float, default=0.5, help="seconds into the capture when the command is sent")
    ap.add_argument("--offset-hz", type=float, default=600e3)
    ap.add_argument("--gain-db", type=float, default=None)
    a = ap.parse_args(argv)
    from transit_logger import sdr_busy
    busy = sdr_busy()
    if busy:
        print(f"REFUSED: the SDR is in use by {busy}")
        return 2
    run = asyncio.run(measure(a.trials, a.controls, a.capture, a.switch_at, a.offset_hz, a.gain_db))
    summ = summarise(run)
    out = F.OUT_ROOT / ("settle_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out.mkdir(parents=True, exist_ok=False)
    (out / "settle.json").write_text(json.dumps({
        "summary": {k: v for k, v in summ.items() if k != "_tail"},
        "trials": [{k: v for k, v in t.items() if k not in ("t_ms", "x", "p_rel")} for t in run["trials"]],
        "tuning": {k: run["tuning"].get(k) for k in ("requested", "applied", "evidence", "rtl_agc", "bias_tee")},
    }, indent=1, default=str))
    np.savez_compressed(out / "profiles.npz", **{f"t{i}_{k}": np.asarray(t[k]) for i, t in enumerate(run["trials"])
                                                 for k in ("t_ms", "x", "p_rel")})
    plot(run, summ, out / "settle.png")
    print(json.dumps({"out": str(out), **{k: v for k, v in summ.items() if k != "_tail"}}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
