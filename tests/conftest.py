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
