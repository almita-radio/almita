#!/usr/bin/env python3
"""Fast DS18B20 log for thermal step tests (e.g. heating the 50 ohm load with a battery).

Reads the given sensors back to back (~0.75 s each), appends one CSV row per round and prints it.
Type a note + Enter at any time (e.g. "ON", "OFF") and it is written to the CSV with the next row.
Read-only on the bus; nothing else touched. Ctrl+C to stop.

Usage: python3 scripts/hardware/ds18_step_log.py <out.csv> inside=28-0000006f80a5 outside=28-0000006fad9d
"""
import os
import select
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from temperature_sensors import DS18B20Reader  # noqa: E402  same checks as the service (CRC, -127, all-zero, range)

_READER = DS18B20Reader({}, warning=lambda m: None)


def read_c(sid):
    _READER.sensors[sid] = sid
    _READER._consecutive_85.setdefault(sid, 0)
    r = _READER.read(sid)
    return r["temperature_c"] if r["valid"] else None


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    out = sys.argv[1]
    sensors = [a.split("=", 1) for a in sys.argv[2:]]
    new = not os.path.exists(out)
    t0 = time.time()
    with open(out, "a", buffering=1) as f:
        if new:
            f.write("utc,t_s," + ",".join(n for n, _ in sensors) + ",note\n")
        print("t_s     " + "  ".join("%8s" % n for n, _ in sensors) + "   (escribe una nota + Enter: ON / OFF)")
        try:
            while True:
                note = ""
                if select.select([sys.stdin], [], [], 0)[0]:
                    note = sys.stdin.readline().strip().replace(",", ";")
                vals = [read_c(s) for _, s in sensors]
                t = time.time() - t0
                f.write("%s,%.1f,%s,%s\n" % (datetime.now(timezone.utc).isoformat(timespec="seconds"), t,
                                             ",".join("" if v is None else "%.3f" % v for v in vals), note))
                print("%6.0f  " % t + "  ".join("%8s" % ("ERR" if v is None else "%.3f" % v) for v in vals)
                      + ("   <- " + note if note else ""), flush=True)
        except KeyboardInterrupt:
            print("\nguardado en", out)


if __name__ == "__main__":
    main()
