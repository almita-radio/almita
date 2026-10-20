#!/usr/bin/env python3
"""Cross scan around the Sun with MAIN total power (operator, 2026-10-18: hand SYNC on the Sun showed ON-OFF ~0.1 dB).

C, E5..E25, C, W5..W25, C, N5..N25, C, S5..S25, C  (degrees on the sky from the Sun, recomputed at each point).
Each point: GOTO, settle, passive rtl_tcp read (no command sent to the SDR). Every point is checked BEFORE its GOTO:
hour angle must stay >= +0.1 h (same pier side, west of the meridian) and altitude >= 20 deg; otherwise skipped.
Writes data/alignment/CROSS-SUN-<utc>/points.jsonl (+ temperatures from the logger) and prints a summary.
Usage: .venv/bin/python scripts/mount/sun_cross_scan.py --yes [--seconds 20] [--step 5] [--max 25]"""
import argparse
import asyncio
import json
import math
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import almita_web_ops  # noqa: E402
import mount_device  # noqa: E402
from alignment import sun_eod  # noqa: E402
from indi_telescope_control import INDITelescopeControl  # noqa: E402

LOC = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=550 * u.m)
TEMP_DIR = ROOT / "data" / "runtime" / "temperature"


def read_power(seconds):
    s = socket.create_connection(("127.0.0.1", 1234), timeout=10)
    s.recv(12)

    def rd(n):
        b = bytearray(n); v = memoryview(b); g = 0
        while g < n:
            g += s.recv_into(v[g:], n - g)
        return b
    rd(2400000)
    p = []
    for _ in range(seconds):
        x = np.frombuffer(rd(4800000), np.uint8).astype(np.float32) - 127.5
        p.append(float(x.var()))
    s.close()
    p = np.array(p)
    return float(10 * np.log10(p.mean())), float(10 * np.log10(p.max() / p.min()))


def last_temps():
    f = TEMP_DIR / f"{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    try:
        r = json.loads(f.read_text(errors="replace").strip().splitlines()[-1].strip("\x00 "))
        return {k: (v or {}).get("c") for k, v in r["readings"].items()}
    except Exception:
        return {}


def target(east, north):
    t = Time.now()
    s = sun_eod(t)
    ra = (s.ra.deg + east / math.cos(math.radians(s.dec.deg + north))) % 360   # east = +RA on the sky
    dec = s.dec.deg + north
    lst = t.sidereal_time("apparent", longitude=LOC.lon).hour
    ha = ((lst - ra / 15 + 12) % 24) - 12
    alt = SkyCoord(ra=ra * u.deg, dec=dec * u.deg).transform_to(AltAz(obstime=t, location=LOC)).alt.deg
    return ra / 15, dec, ha, alt


async def main(a):
    if mount_device.is_simulator(mount_device.current()):
        sys.exit("REFUSED: simulator selected")
    if almita_web_ops.running_jobs():
        sys.exit("REFUSED: a web job is running")
    plan = [("C", 0, 0)]
    offs = [a.step * k for k in range(1, int(a.max // a.step) + 1)]
    for name, de, dn in (("E", 1, 0), ("W", -1, 0), ("N", 0, 1), ("S", 0, -1)):
        if name not in a.arms:
            continue
        plan += [(f"{name}{o:g}", de * o, dn * o) for o in offs] + [("C", 0, 0)]
    out = ROOT / "data" / "alignment" / f"CROSS-SUN-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out.mkdir(parents=True)
    tel = INDITelescopeControl("localhost", 7624, mount_device.current(), False)
    if not await tel.connect():
        sys.exit("REFUSED: INDI connect failed")
    rows = []
    try:
        for label, e, n in plan:
            ra, dec, ha, alt = target(e, n)
            if ha < 0.1 or alt < 20:
                print(f"SKIP {label}: HA {ha:+.2f} h alt {alt:.1f}", flush=True)
                continue
            ok = await tel.goto(ra, dec)
            await asyncio.sleep(a.settle)
            db, spread = read_power(a.seconds)
            row = {"label": label, "east": e, "north": n, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "goto_ok": ok, "ra_h": ra, "dec_deg": dec, "ha_h": ha, "alt_deg": alt, "power_db": db,
                   "spread_db": spread, "temps": last_temps()}
            rows.append(row)
            with (out / "points.jsonl").open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(f"{row['utc'][11:19]} {label:4} alt {alt:4.1f} HA {ha:+.2f}  {db:7.3f} dB  spread {spread:.2f}", flush=True)
        ra, dec, _, _ = target(0, 0)
        await tel.goto(ra, dec)                      # end on the Sun, tracking as before
    finally:
        await tel.disconnect()
    # drift: interpolate the centre visits in time, report each arm relative to it
    t = lambda r: datetime.fromisoformat(r["utc"]).timestamp()
    cs = [r for r in rows if r["label"] == "C"]
    if len(cs) >= 2:
        ct, cp = np.array([t(r) for r in cs]), np.array([r["power_db"] for r in cs])
        print("\ncentre visits:", [round(x, 3) for x in cp])
        for r in rows:
            r["rel_db"] = r["power_db"] - float(np.interp(t(r), ct, cp))
        for arm in "EWNS":
            print(arm, " ".join(f"{r['label']}:{r['rel_db']:+.2f}" for r in rows if r["label"].startswith(arm)))
        (out / "summary.json").write_text(json.dumps(rows, indent=1))
    print("saved", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true"); ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--step", type=float, default=5.0); ap.add_argument("--max", type=float, default=25.0)
    ap.add_argument("--settle", type=float, default=4.0)
    ap.add_argument("--arms", default="EWNS", help="which arms, e.g. E or EW (default EWNS)")
    a = ap.parse_args()
    if not a.yes:
        sys.exit("needs --yes")
    asyncio.run(main(a))
