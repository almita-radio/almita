"""Runs the real capture.py entry point (capture.cli) in this process, for the STOP integration tests.

Only ONE thing is replaced: the explicit-tuning acknowledgement, which in production is read from the journal of
the system's rtl_tcp.service and cannot exist for a test server. The stand-in still sends the configuration to the
(fake) rtl_tcp. Everything the tests are about - signal handling, the session loop, GOTO, the mount stop and its
INDI readback, the end-of-session announcement, HDF5 writing - is the real code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sdr_tuning  # noqa: E402


async def _acknowledged(sdr, center_frequency_hz, sample_rate_hz, gain_db, *, operating=None, **_):
    requested = {"center_frequency_hz": int(round(center_frequency_hz)), "sample_rate_hz": int(round(sample_rate_hz)),
                 "gain_db": float(gain_db)}
    await sdr.configure(center_freq=requested["center_frequency_hz"], sample_rate=requested["sample_rate_hz"], gain=requested["gain_db"])
    return {"requested": requested, "applied": dict(requested), "evidence": "TEST_STUB",
            "evidence_detail": "stop-test launcher: no rtl_tcp.service journal", "pll_not_locked_count": 0,
            "tuned_utc": "1970-01-01T00:00:00+00:00"}


sdr_tuning.tune_explicitly = _acknowledged
sdr_tuning.main_device_reenumerated_since_rtl_tcp_start = lambda *a, **k: None

import os  # noqa: E402

import mount_stop  # noqa: E402

if os.environ.get("ALMITA_STOP_TEST_MOTION_TIMEOUT_S"):      # tests only: do not wait the production 120 s
    mount_stop.MOTION_TIMEOUT_S = float(os.environ["ALMITA_STOP_TEST_MOTION_TIMEOUT_S"])
    mount_stop.stop_mount_and_confirm.__kwdefaults__["motion_timeout_s"] = mount_stop.MOTION_TIMEOUT_S

import capture  # noqa: E402

if __name__ == "__main__":
    sys.exit(capture.cli())
