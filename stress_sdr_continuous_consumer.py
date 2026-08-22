#!/usr/bin/env python3
"""No-mount endurance harness for the rtl_tcp continuous consumer."""

import argparse
import asyncio
import csv
import json
import os
import subprocess
import time
from pathlib import Path

from sdr_capture import SDRCapture, validate_hdf5_capture


def rss_kib(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


async def run(args):
    work = Path(args.output)
    work.mkdir(parents=True, exist_ok=True)
    csv_path = work / "stress.csv"
    fields = ["cycle", "status", "duration", "error_code", "error_detail"]
    rows = [{"cycle": str(i), "status": "planned"} for i in range(1, args.cycles + 1)]

    def persist():
        with csv_path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    persist()
    rtl_pid = int(subprocess.check_output(
        ["systemctl", "show", "rtl_tcp", "-p", "MainPID", "--value"], text=True
    ).strip())
    python_pid = os.getpid()
    rtl_rss = [rss_kib(rtl_pid)]
    python_rss = [rss_kib(python_pid)]
    capture = SDRCapture(mode="network", host=args.host, port=args.port, verbose=True)
    await capture.connect()
    await capture.configure(args.frequency, args.sample_rate, args.gain)
    failures = 0
    buffer_peak = 0
    start = time.monotonic()
    try:
        for index, row in enumerate(rows, 1):
            duration = args.short if index % 2 else args.long
            wait = args.wait_scale * (5.0 + (index % 6))
            await asyncio.sleep(wait)                  # GOTO-equivalent
            await asyncio.sleep(args.wait_scale * 0.5)  # tracking
            await asyncio.sleep(args.wait_scale * 2.0)  # settle
            await capture.flush_buffer()
            row["status"] = "capturing"
            persist()
            output = work / f"cycle_{index:03d}.h5"
            cycle_start = time.monotonic()
            try:
                metrics = await capture.capture(duration, str(output), args.sample_rate, {
                    "point_id": index, "session_id": "sdr-stress",
                    "scan_order": index, "actual_capture_order": index,
                })
                validate_hdf5_capture(output, expected_samples=int(duration * args.sample_rate))
                row["status"] = "success"
                row["duration"] = f"{time.monotonic() - cycle_start:.6f}"
                output.unlink()
            except Exception as exc:
                failures += 1
                row["status"] = "failed"
                row["error_code"] = getattr(exc, "code", type(exc).__name__)
                row["error_detail"] = str(exc)
            persist()
            await asyncio.sleep(args.wait_scale * 0.5)  # disk/processing-equivalent
            telemetry = capture.get_network_telemetry()
            buffer_peak = max(buffer_peak, metrics.buffer_used if row["status"] == "success" else 0)
            rtl_rss.append(rss_kib(rtl_pid))
            python_rss.append(rss_kib(python_pid))
            print(
                f"CYCLE {index:03d}/{args.cycles} status={row['status']} capture={duration:.1f}s "
                f"wall={row.get('duration', 'N/D')} discard={telemetry['bytes_discarded']} "
                f"kept={telemetry['bytes_kept']} rate={telemetry['instantaneous_throughput']:.0f} "
                f"last_age={telemetry['seconds_since_last_byte']:.3f} "
                f"rtl_rss={rtl_rss[-1]}KiB py_rss={python_rss[-1]}KiB",
                flush=True,
            )
    finally:
        await capture.close()

    statuses = [row["status"] for row in rows]
    result = {
        "cycles": args.cycles,
        "success": statuses.count("success"),
        "failed": failures,
        "orphan_capturing": statuses.count("capturing"),
        "part_files": len(list(work.glob("*.part"))),
        "wall_seconds": time.monotonic() - start,
        "rtl_pid": rtl_pid,
        "rtl_rss_start_kib": rtl_rss[0], "rtl_rss_end_kib": rtl_rss[-1],
        "rtl_rss_max_kib": max(rtl_rss),
        "python_rss_start_kib": python_rss[0], "python_rss_end_kib": python_rss[-1],
        "python_rss_max_kib": max(python_rss),
        "buffer_peak": buffer_peak,
    }
    (work / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    print("STRESS_RESULT " + json.dumps(result, sort_keys=True), flush=True)
    return 0 if failures == 0 and result["part_files"] == 0 and result["orphan_capturing"] == 0 else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=200)
    parser.add_argument("--short", type=float, default=2.0)
    parser.add_argument("--long", type=float, default=20.0)
    parser.add_argument("--wait-scale", type=float, default=1.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--frequency", type=int, default=1420405000)
    parser.add_argument("--sample-rate", type=int, default=2400000)
    parser.add_argument("--gain", default="40.2")
    parser.add_argument("--output", default="/tmp/sdr_continuous_stress")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
