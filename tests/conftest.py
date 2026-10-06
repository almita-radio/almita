"""Test-wide hardware stand-in for explicit SDR tuning.

Every real acquisition calls sdr_tuning.tune_explicitly(), which waits for rtl_tcp's acknowledgement in the
rtl_tcp journal (RFI_REF: in its dedicated rtl_tcp log). Tests use fake SDRs with no rtl_tcp behind them, so by default the call is replaced by a stub
that still sends configure() to the fake and records the request as applied, with evidence "TEST_STUB" (never
the real RTL_TCP_SERVER_ACK). Tests of the real tuning logic opt out with @pytest.mark.real_tuning."""
import pytest

import sdr_tuning


@pytest.fixture(autouse=True)
def _stub_sdr_tuning(request, monkeypatch):
    if "real_tuning" in request.keywords:
        return

    async def _stub(sdr, center_frequency_hz, sample_rate_hz, gain_db, *, operating=None, **_):
        requested = {"center_frequency_hz": int(round(center_frequency_hz)),
                     "sample_rate_hz": int(round(sample_rate_hz)), "gain_db": float(gain_db)}
        await sdr.configure(center_freq=requested["center_frequency_hz"], sample_rate=requested["sample_rate_hz"],
                        gain=requested["gain_db"])
        return {"requested": requested, "applied": dict(requested), "evidence": "TEST_STUB",
                "evidence_detail": "tests/conftest.py stub - no rtl_tcp", "pll_not_locked_count": 0,
                "tuned_utc": "1970-01-01T00:00:00+00:00"}
    monkeypatch.setattr(sdr_tuning, "tune_explicitly", _stub)

    import rfi_monitor

    async def _rfi_stub(self, writer):
        return {"requested": {"center_frequency_hz": self.center_frequency_hz}, "evidence": "TEST_STUB"}
    monkeypatch.setattr(rfi_monitor.RFIReferenceMonitor, "_tune_explicitly", _rfi_stub)


# ------------------------------------------------------------------ fixture data (no dependency on data/)
# data/ holds generated, operator-owned results and is not versioned test input. Tests use:
#   - tests/fixtures/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/: the real V1 relative profile
#     (restored byte for byte from git history, where it lived under data/calibration/), and
#   - synthetic captures written to a temporary directory by the `rf_captures` fixture below.
from pathlib import Path as _Path

FIXTURES = _Path(__file__).resolve().parent / "fixtures"
FOUNDATION_PROFILE = FIXTURES / "calibration" / "CALIBRATION-FOUNDATION-V1-20260827T005049Z" / "calibration_profile_v1.npz"


def write_capture(path, *, rf_input="ANTENNA_AT_LNA_INPUT_INDOOR", center_frequency_hz=1420405752,
                  sample_rate_hz=2400000, gain_db=40.2, n_complex=8192 * 96, seed=20260827):
    """A MAIN-like capture: uint8 interleaved I/Q around 127.5 plus the attrs quicklook / calibration read."""
    import h5py
    import numpy as np
    rng = np.random.default_rng(seed)
    iq = np.clip(np.round(127.5 + rng.normal(0.0, 6.0, size=2 * n_complex)), 0, 255).astype(np.uint8)
    with h5py.File(path, "w") as capture:
        capture.create_dataset("iq_data", data=iq)
        capture.attrs.update(capture_status="success", center_frequency_hz=center_frequency_hz,
                             sample_rate_hz=sample_rate_hz, gain_requested_db=gain_db, rf_input=rf_input,
                             capture_start_utc="2026-08-27T00:31:04+00:00")
    return _Path(path)


@pytest.fixture(scope="session")
def rf_captures(tmp_path_factory):
    """`source`: an antenna capture compatible with FOUNDATION_PROFILE; `direct`: the same receiver settings
    through a different instrument chain (RTL-SDR input direct on 50 ohm) - INCOMPATIBLE by topology."""
    from types import SimpleNamespace
    d = tmp_path_factory.mktemp("rf_captures")
    return SimpleNamespace(source=write_capture(d / "antenna_a.h5"),
                           direct=write_capture(d / "rtl_direct_50ohm_gain_40.2.h5", rf_input="RTL_SDR_DIRECT_50_OHM", seed=7))


def transiting_ra_hours():
    """RA (hours) crossing the observer's meridian right now (local apparent sidereal time at observer_config.json's
    longitude). A FIXED_CENTER test field placed there is high in the sky whenever the test runs, instead of a
    fixed RA that is below the horizon at some hours of the day (which made plan tests fail by time of day)."""
    import json as _json
    from astropy.time import Time
    import astropy.units as u
    lon = _json.loads((_Path(__file__).resolve().parents[1] / "observer_config.json").read_text())["observer"]["longitude_deg"]
    return round(float(Time.now().sidereal_time("apparent", longitude=lon * u.deg).hour), 4)
