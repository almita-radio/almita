"""Regression tests for the operating-frequency single source and explicit SDR tuning (sdr_tuning.py).

Incidents these guard against:
- 2026-09-28: a stale 1420405000 Hz default built a calibration profile that QUICKLOOK rejected for a whole
  625-point observation at the real 1420405752 Hz.
- 2026-10-01: the wizard's 50 ohm captures never retuned the SDR, ran at the 1420405000 Hz rtl_tcp was left at,
  and declared 1420405752 Hz in their HDF5 attrs (the service argv was taken as "verified").

No hardware: the acknowledgement source is a fake that behaves like rtl_tcp's log."""
import asyncio
import io
import json
import re
import subprocess
import tokenize
from pathlib import Path

import pytest

import sdr_tuning

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.real_tuning     # the conftest stub must not replace what is tested here


# ------------------------------------------------------------------ single source
def test_operating_config_is_the_agreed_frequency():
    op = sdr_tuning.operating_config()
    assert op == {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2}


@pytest.mark.parametrize("defaults", [{}, {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000},
                                      {"center_frequency_hz": 1420.405752, "sample_rate_hz": 2400000, "gain_db": 40.2},
                                      {"center_frequency_hz": "1420405752", "sample_rate_hz": 2400000, "gain_db": 40.2}])
def test_operating_config_never_falls_back_to_a_literal(tmp_path, defaults):
    """Missing gain, a MHz-looking value (Hz/MHz confusion) or a string all raise - no silent default."""
    path = tmp_path / "observer_config.json"
    path.write_text(json.dumps({"observation_defaults": defaults}))
    with pytest.raises(sdr_tuning.OperatingConfigError):
        sdr_tuning.operating_config(path)


def test_versioned_rtl_tcp_unit_matches_operating_config_and_is_line_buffered():
    unit = (ROOT / "systemd" / "rtl_tcp.service").read_text()
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    args = exec_start.split("=", 1)[1].split()
    op = sdr_tuning.operating_config()
    assert args[:2] == ["/usr/bin/stdbuf", "-oL"]
    assert int(args[args.index("-f") + 1]) == op["center_frequency_hz"]
    assert int(args[args.index("-s") + 1]) == op["sample_rate_hz"]
    assert float(args[args.index("-g") + 1]) == op["gain_db"]
    assert args[args.index("-d") + 1] == "00000001"


# Frequencies that must never appear as a code literal outside the single source. Comments and docstrings that
# narrate the incidents are fine; numbers in code are not.
_FORBIDDEN = {1420405000, 1420405752, 1420405752.0, 1420405000.0}
# Literals that legitimately describe something else than "the frequency to use".
_ALLOWED = {
    "reduce_single_capture.py",        # detects sdr_capture's historic placeholder value in OLD captures
    "science_forensics/experiment/plan.py",   # audited record of what a 2026-09-02 campaign actually used
    "reduce_engine/simulation.py",     # REDUCE V1 frozen at 2afc4c5: synthetic-capture default only, never tunes
    # historical audit: OLD_UNIT_STARTUP_HZ records the pre-2026-10-01 unit argv; narrowed by the test below
    "scripts/calibration/audit_frequency_declared_vs_actual.py",
}


def test_the_frequency_audit_holds_only_the_historical_startup_literal():
    rel = "scripts/calibration/audit_frequency_declared_vs_actual.py"
    source = (ROOT / rel).read_text()
    found = [(tok.start[0], tok.string) for tok in tokenize.generate_tokens(io.StringIO(source).readline)
             if tok.type == tokenize.NUMBER and _is_forbidden(tok.string)]
    assert [s for _, s in found] == ["1420405000"]
    line = source.splitlines()[found[0][0] - 1]
    assert line.startswith("OLD_UNIT_STARTUP_HZ = ")


def _is_forbidden(text):
    try:
        return float(text.replace("_", "")) in _FORBIDDEN
    except ValueError:
        return False


def _tracked_python_files():
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [p for p in out.split() if not p.startswith(("tests/", "docs/", "data/")) and p not in _ALLOWED
            and (ROOT / p).is_file()]


def test_no_frequency_literal_in_operational_python():
    offenders = []
    for rel in _tracked_python_files() + ["sdr_tuning.py"]:
        source = (ROOT / rel).read_text(errors="replace")
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        except (tokenize.TokenError, SyntaxError):
            continue
        for tok in tokens:
            if tok.type == tokenize.NUMBER:
                try:
                    value = float(tok.string.replace("_", ""))
                except ValueError:
                    continue
                if value in _FORBIDDEN:
                    offenders.append(f"{rel}:{tok.start[0]}: {tok.string}")
    assert offenders == [], "frequency literals outside observer_config.json:\n" + "\n".join(offenders)


def test_no_frequency_literal_in_the_web_console():
    offenders = []
    for path in sorted((ROOT / "console").glob("*.*")):
        if path.suffix not in (".html", ".js"):
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            code = line.split("//", 1)[0] if path.suffix == ".js" else line
            if re.search(r"(?<!\d)14204050(00|52)(?!\d)", code):
                offenders.append(f"{path.name}:{n}: {line.strip()[:120]}")
    assert offenders == [], "\n".join(offenders)


# ------------------------------------------------------------------ explicit tuning
class _FakeSDR:
    """Records configure() calls; cached state starts as if a previous configure had matched (no-op risk)."""

    def __init__(self, ack_log):
        self.ack_log = ack_log
        self.calls = []
        self.current_frequency = 1420405752
        self.current_sample_rate = 2400000
        self.manual_gain_enabled = True
        self.current_gain = 40.2

    async def configure(self, center_freq, sample_rate, gain):
        self.calls.append((center_freq, sample_rate, gain, self.current_frequency))
        self.ack_log(center_freq, sample_rate, gain)


class _FakeAckSource:
    def __init__(self):
        self.all, self.offset = [], 0
        self.describe = "fake rtl_tcp log"

    def mark(self):
        self.offset = len(self.all)

    def lines(self):
        return self.all[self.offset:]


def _acking(source, freq_override=None):
    def log(freq, rate, gain):
        source.all += [f"set freq {freq_override if freq_override is not None else freq}", f"set sample rate {rate}",
                       "set gain mode 1", "[R82XX] PLL not locked!", f"set gain {int(round(gain * 10))}"]
    return log


def test_tune_forces_all_commands_even_when_cache_says_already_tuned_and_records_ack():
    source = _FakeAckSource()
    source.all = ["set freq 1420405000"]                      # stale line from before: must not count
    sdr = _FakeSDR(_acking(source))
    rec = asyncio.run(sdr_tuning.tune_explicitly(sdr, 1420405752, 2400000, 40.2, ack_source=source,
                                                 operating=sdr_tuning.operating_config()))
    assert sdr.calls == [(1420405752, 2400000, 40.2, None)]   # cache cleared before configure
    assert rec["requested"] == rec["applied"] == {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000,
                                                  "gain_db": 40.2}
    assert rec["evidence"] == sdr_tuning.EVIDENCE_SERVER_ACK
    assert rec["pll_not_locked_count"] == 1
    assert rec["matches_operating"] is True
    assert "readback" not in rec["evidence"].lower()


def test_wrong_acknowledged_frequency_blocks():
    source = _FakeAckSource()
    sdr = _FakeSDR(_acking(source, freq_override=1420405000))
    with pytest.raises(sdr_tuning.TuningIncoherent, match="center_frequency_hz"):
        asyncio.run(sdr_tuning.tune_explicitly(sdr, 1420405752, 2400000, 40.2, ack_source=source, ack_timeout=0.3))


def test_missing_acknowledgement_blocks():
    source = _FakeAckSource()
    sdr = _FakeSDR(lambda *a: None)                            # rtl_tcp printed nothing (e.g. not line-buffered)
    with pytest.raises(sdr_tuning.TuningIncoherent, match="NO capture taken"):
        asyncio.run(sdr_tuning.tune_explicitly(sdr, 1420405752, 2400000, 40.2, ack_source=source, ack_timeout=0.3))


def test_tuning_attrs_keep_requested_and_applied_apart():
    rec = {"requested": {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2},
           "applied": {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000, "gain_db": 40.2},
           "evidence": "RTL_TCP_SERVER_ACK", "evidence_detail": "x", "pll_not_locked_count": 0, "tuned_utc": "t"}
    attrs = sdr_tuning.tuning_attrs(rec)
    for key in ("requested_center_frequency_hz", "applied_center_frequency_hz", "requested_gain_db",
                "applied_gain_db", "tuning_evidence"):
        assert key in attrs
    assert attrs["center_frequency_hz"] == attrs["applied_center_frequency_hz"]


def test_parse_acks_reads_real_rtl_tcp_journal_lines():
    lines = ["client accepted! localhost 52554", "set freq 1420405752", "set sample rate 2400000",
             "set gain mode 1", "set gain 402", "[R82XX] PLL not locked!"]
    assert sdr_tuning.parse_acks(lines) == {"center_frequency_hz": 1420405752, "sample_rate_hz": 2400000,
                                            "gain_tenths_db": 402, "pll_not_locked_count": 1, "device_failures": []}


# ------------------------------------------------------------------ every acquisition path tunes explicitly
@pytest.mark.parametrize("rel", ["capture.py", "alignment.py", "alignment_engine/hi/acquisition.py",
                                 "observation_gain_pilot.py", "calibrate_reference_wizard.py",
                                 "calibration_engine/acquisition.py", "calibrate.py"])
def test_acquisition_paths_never_configure_without_evidence(rel):
    """Real acquisition code tunes through sdr_tuning.tune_explicitly(), never a bare sdr.configure()."""
    source = (ROOT / rel).read_text()
    bare = [n for n, line in enumerate(source.splitlines(), 1)
            if re.search(r"\.configure\(", line) and "probe.configure" not in line]
    assert bare == [], f"{rel}: bare configure() at lines {bare}"
    assert "tune_explicitly" in source or "backend.tune(" in source or ".tune(" in source


# ------------------------------------------------------------------ 2026-10-07 incident: "set freq" printed, librtlsdr failed

def _failing(source):
    def log(freq, rate, gain):        # the real rtl_tcp journal of the incident (USB device re-enumerated: -4)
        source.all += [f"set freq {freq}", "rtlsdr_demod_write_reg failed with -4", "r82xx_set_freq: failed=-4",
                       f"set sample rate {rate}", "set gain mode 1", f"set gain {int(round(gain * 10))}",
                       "r82xx_write: i2c wr failed=-4 reg=05 len=1"]
    return log


def test_acknowledged_settings_that_librtlsdr_failed_to_apply_block_the_capture():
    source = _FakeAckSource()
    sdr = _FakeSDR(_failing(source))
    with pytest.raises(sdr_tuning.TuningIncoherent, match="librtlsdr FAILED to apply them .*restart rtl_tcp.service - NO capture taken"):
        asyncio.run(sdr_tuning.tune_explicitly(sdr, 1420405752, 2400000, 40.2, ack_source=source, ack_timeout=0.3))


def test_zero_copy_fallback_warning_alone_is_not_a_failure():
    source = _FakeAckSource()

    def log(freq, rate, gain):
        source.all += ["Allocating 15 zero-copy buffers", "Failed to allocate zero-copy buffer for transfer 0",
                       "Falling back to buffers in userspace", f"set freq {freq}", f"set sample rate {rate}",
                       f"set gain {int(round(gain * 10))}"]
    rec = asyncio.run(sdr_tuning.tune_explicitly(_FakeSDR(log), 1420405752, 2400000, 40.2, ack_source=source, ack_timeout=0.3))
    assert rec["evidence"] == sdr_tuning.EVIDENCE_SERVER_ACK


class _Run:
    def __init__(self, started, kernel):
        self.started, self.kernel = started, kernel

    def __call__(self, argv, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(stdout=self.started if argv[0] == "systemctl" else self.kernel)


def test_a_main_dongle_reattached_after_rtl_tcp_started_is_reported():
    kernel = ("2026-10-07T00:11:57+0000 stellarmate kernel: usb 1-1: USB disconnect, device number 2\n"
              "2026-10-07T00:11:58+0000 stellarmate kernel: usb 1-1: SerialNumber: 00000001\n")
    reason = sdr_tuning.main_device_reenumerated_since_rtl_tcp_start(run=_Run("Fri 2026-10-02 21:29:46 UTC", kernel))
    assert "re-attached by the kernel at 2026-10-07T00:11:58+0000" in reason and "restart rtl_tcp.service" in reason
    assert sdr_tuning.main_device_reenumerated_since_rtl_tcp_start(run=_Run("Wed 2026-10-07 00:20:00 UTC", "")) is None
    other = "2026-10-07T00:11:58+0000 stellarmate kernel: usb 3-1: SerialNumber: 00000002\n"   # the RFI dongle
    assert sdr_tuning.main_device_reenumerated_since_rtl_tcp_start(run=_Run("x", other)) is None
