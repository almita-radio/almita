#!/usr/bin/env python3
"""SYNC the mount on the Sun after the operator pointed the antenna at it BY HAND (clutches released, shadow method,
clutches locked again) - operator, 2026-10-18, after two solar ALIGNs saw no Sun.

`show`          read only: where OnStep believes it points, its pier side, the Sun (EOD) now, and the separation.
`sync --yes`    the Sun is recomputed at that instant; refused if a web job runs, the mount lock is held, or the
                separation is above --max-sep-deg (a large one is far more likely a pier-side flip than a pointing
                error). Sends the same INDITelescopeControl.sync() ALIGN uses, then tracking ON (--no-track skips it),
                reads back, and saves the evidence to data/alignment/MANUAL-SUN-SYNC-<utc>.json. No GOTO is sent.
`power`         passive 30 s read of MAIN over rtl_tcp (no command sent): rms and relative dB, for a hand ON/OFF."""
import argparse
import asyncio
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time
import astropy.units as u

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import almita_web_ops  # noqa: E402
import mount_device  # noqa: E402
import mount_lock  # noqa: E402
from alignment import sun_eod  # noqa: E402
from indi_telescope_control import INDITelescopeControl  # noqa: E402


def state():
    device = mount_device.current()
    m = almita_web_ops.read_mount(device=device)
    now = Time.now()
    sun = sun_eod(now)
    out = {"utc": now.utc.isot, "device": device, "mount": {k: m.get(k) for k in
           ("ra_h", "dec_deg", "tracking", "parked", "mount_state", "onstep_error", "pier", "error")},
           "sun_eod": {"ra_h": float(sun.ra.hour), "dec_deg": float(sun.dec.deg)}}
    if m.get("ra_h") is not None:
        believed = SkyCoord(ra=m["ra_h"] * u.hourangle, dec=m["dec_deg"] * u.deg)
        out["separation_deg"] = float(believed.separation(SkyCoord(ra=sun.ra, dec=sun.dec)).deg)
    return out


async def do_sync(args):
    if mount_device.is_simulator(mount_device.current()):
        sys.exit("REFUSED: the simulator is selected")
    jobs = almita_web_ops.running_jobs()
    if jobs:
        sys.exit(f"REFUSED: web job(s) running: {[j.get('job_id') for j in jobs]}")
    if mount_lock.holder():
        sys.exit(f"REFUSED: mount lock held by {mount_lock.holder()}")
    pre = state()
    print(json.dumps(pre, indent=1))
    if pre.get("separation_deg") is None:
        sys.exit("REFUSED: mount coordinates unreadable")
    if pre["separation_deg"] > args.max_sep_deg:
        sys.exit(f"REFUSED: separation {pre['separation_deg']:.2f} deg > {args.max_sep_deg} deg - check the pier side")
    tel = INDITelescopeControl("localhost", 7624, pre["device"], False)
    if not await tel.connect():
        sys.exit("REFUSED: INDI connect failed")
    try:
        sun = sun_eod(Time.now())                      # the Sun at the instant of the SYNC, not of `pre`
        sent = {"utc": Time.now().utc.isot, "ra_h": float(sun.ra.hour), "dec_deg": float(sun.dec.deg)}
        ok = bool(await tel.sync(sent["ra_h"], sent["dec_deg"]))
        tracking = None
        if ok and not args.no_track:
            tracking = bool(await tel.set_tracking(True))
        ra, dec = await tel.get_coordinates(force_refresh=True)
    finally:
        await tel.disconnect()
    post = state()
    record = {"pre": pre, "sync_sent": sent, "sync_ok": ok, "tracking_on_ok": tracking,
              "readback": {"ra_h": ra, "dec_deg": dec}, "post": post,
              "method": "operator pointed by hand (shadow), clutches locked, then SYNC on the Sun EOD"}
    path = ROOT / "data" / "alignment" / f"MANUAL-SUN-SYNC-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(record, indent=1))
    print(json.dumps({k: record[k] for k in ("sync_sent", "sync_ok", "tracking_on_ok", "readback")}, indent=1))
    print("post separation from the Sun (deg):", post.get("separation_deg"), "| saved", path)


def power(seconds):
    s = socket.create_connection(("127.0.0.1", 1234), timeout=10)
    s.recv(12)

    def rd(n):
        b = bytearray(n); v = memoryview(b); g = 0
        while g < n:
            g += s.recv_into(v[g:], n - g)
        return b
    rd(2400000)
    vals = []
    for _ in range(seconds):
        x = np.frombuffer(rd(4800000), np.uint8).astype(np.float32) - 127.5
        vals.append(float(x.var()))
    s.close()
    p = np.array(vals)
    print(f"{datetime.now(timezone.utc):%H:%M:%S} UTC  rms {np.sqrt(p.mean()):.3f}  power {10*np.log10(p.mean()):.3f} dB"
          f"  (1 s spread {10*np.log10(p.max()/p.min()):.2f} dB, n={len(p)})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    s = sub.add_parser("sync"); s.add_argument("--yes", action="store_true"); s.add_argument("--max-sep-deg", type=float, default=10.0)
    s.add_argument("--no-track", action="store_true")
    pw = sub.add_parser("power"); pw.add_argument("--seconds", type=int, default=30)
    a = ap.parse_args()
    if a.cmd == "show":
        print(json.dumps(state(), indent=1))
    elif a.cmd == "power":
        power(a.seconds)
    elif not a.yes:
        sys.exit("sync needs --yes")
    else:
        asyncio.run(do_sync(a))


if __name__ == "__main__":
    main()
