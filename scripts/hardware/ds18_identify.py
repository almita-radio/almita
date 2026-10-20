#!/usr/bin/env python3
"""Identify DS18B20 sensors on the 1-Wire bus, live.

Lists every 28-* device, labels the ones already known in observer_config.json
(temperature_sensors: role -> id), and refreshes each reading in place with its
delta from the start and from the previous read. Warm one sensor with your
fingers: the row that climbs (marked <<< CALIENTE) is that sensor.

Read-only: never writes the config. Ctrl+C to exit.
Usage: python3 scripts/hardware/ds18_identify.py [--interval 1.0] [--config observer_config.json]
"""
import argparse
import glob
import json
import os
import sys
import time

W1 = "/sys/bus/w1/devices"
HOT_DELTA_C = 0.5  # rise over the baseline that marks a sensor as "being warmed"


def known_roles(config_path):
    try:
        with open(config_path) as f:
            sensors = json.load(f).get("temperature_sensors", {}) or {}
        return {sid: role for role, sid in sensors.items()}
    except (OSError, ValueError):
        return {}


def read_c(sid):
    try:
        with open(os.path.join(W1, sid, "temperature")) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "..", "observer_config.json"))
    args = ap.parse_args()

    roles = known_roles(args.config)
    ids = sorted(os.path.basename(p) for p in glob.glob(os.path.join(W1, "28-*")))
    if not ids:
        sys.exit("No hay sensores DS18B20 en %s (¿dtoverlay=w1-gpio activo?)" % W1)

    base, prev, peak = {}, {}, {}
    t0 = time.time()
    try:
        while True:
            # rescan so a sensor that drops or appears shows up
            ids = sorted(set(ids) | {os.path.basename(p) for p in glob.glob(os.path.join(W1, "28-*"))})
            rows = []
            for sid in ids:
                c = read_c(sid)
                if c is not None:
                    base.setdefault(sid, c)
                    peak[sid] = max(peak.get(sid, c), c)
                rows.append((sid, c))
            out = ["\033[H\033[J",
                   "DS18B20 en vivo — %d sensores — %.0f s — Ctrl+C para salir\n" % (len(ids), time.time() - t0),
                   "%-17s %-10s %8s %8s %8s %8s\n" % ("SERIAL", "ROL", "T °C", "Δinicio", "Δprev", "máx")]
            for sid, c in rows:
                role = roles.get(sid, "?? NUEVO")
                if c is None:
                    out.append("%-17s %-10s %8s\n" % (sid, role, "ERROR"))
                    continue
                d0 = c - base[sid]
                dp = c - prev.get(sid, c)
                mark = "  <<< CALIENTE" if d0 >= HOT_DELTA_C else ""
                out.append("%-17s %-10s %8.3f %+8.3f %+8.3f %8.3f%s\n" % (sid, role, c, d0, dp, peak[sid], mark))
                prev[sid] = c
            missing = [r for s, r in roles.items() if s not in ids]
            if missing:
                out.append("\nOJO: roles configurados sin sensor en el bus: %s\n" % ", ".join(missing))
            out.append("\nConocidos (observer_config.json): %s\n"
                       % (", ".join("%s=%s" % (r, s) for s, r in roles.items()) or "ninguno"))
            sys.stdout.write("".join(out))
            sys.stdout.flush()
            time.sleep(max(0.0, args.interval))
    except KeyboardInterrupt:
        print("\nResumen (subida máxima sobre el inicio):")
        for sid in ids:
            if sid in base:
                print("  %-17s %-10s %+.3f °C" % (sid, roles.get(sid, "?? NUEVO"), peak[sid] - base[sid]))


if __name__ == "__main__":
    main()
