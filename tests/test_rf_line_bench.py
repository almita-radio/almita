"""scripts/rf_line_bench.py (BL-071): gates (Bias-T rule, busy receivers), analysis on synthetic IQ, never the mount."""
import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rf_line_bench", ROOT / "scripts" / "rf_line_bench.py")
bench = importlib.util.module_from_spec(spec)
sys.modules["rf_line_bench"] = bench
spec.loader.exec_module(bench)


def refusal(step, **kw):
    base = dict(dc_block_confirmed=False, rfi_bias_t=None, simulate=False, busy=[], main_bias=True, main_listening=True)
    return bench.capture_refusal(step, **{**base, **kw})


def test_terminator_on_the_main_sdr_needs_a_dc_block_while_rtl_tcp_has_minus_t():
    assert "DC block" in refusal("M1")
    assert "Bias-T state unknown" in refusal("M1", main_bias=None)
    assert refusal("M1", dc_block_confirmed=True) is None
    assert refusal("M1", main_bias=False) is None
    assert refusal("M2") is None                                   # 50 ohm behind the LNA: Bias-T must stay on


def test_terminator_on_the_rfi_dongle_is_refused_with_bias_t_on():
    assert refusal("R1") is None                                   # the step's default: Bias-T OFF
    assert "Bias-T ON" in refusal("R1", rfi_bias_t=True)
    assert refusal("R1", rfi_bias_t=True, dc_block_confirmed=True) is None


def test_refused_while_an_observation_or_wizard_owns_the_receivers_and_r4_needs_main_stopped():
    assert "capture.py" in refusal("M2", busy=["capture.py (pid 1)"])
    assert "not listening" in refusal("M2", main_listening=False)
    assert "still running" in refusal("R4")
    assert refusal("R4", main_listening=False) is None


def test_analysis_finds_the_injected_main_line_and_the_rfi_comb_bursts():
    main = bench.analyze_iq(bench.synthetic_iq(4, "M2"))
    assert abs(main["main_line"]["freq_mhz"] - 1419.50) < 0.01 and main["main_line"]["excess_db"] > 3
    assert main["rfi_comb_burst_fraction_max"] == 0
    assert bench.analyze_iq(bench.synthetic_iq(4, "M3"))["main_line"]["excess_db"] < 0.5   # line absent
    rfi = bench.analyze_iq(bench.synthetic_iq(20, "R0"))
    assert 0.05 <= rfi["rfi_comb_burst_fraction_max"] <= 0.2 and rfi["rfi_fixed_1420_035"] > 10
    assert 0.5 < main["adc_std_codes"] < 1.0


def test_the_bench_never_imports_mount_or_capture_code():
    tree = ast.parse((ROOT / "scripts" / "rf_line_bench.py").read_text())
    names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not names & {"capture", "indi_telescope_control", "mount_control", "mount_stop"}


def test_simulated_capture_writes_only_under_the_given_session(tmp_path):
    rc = bench.main(["capture", "--step", "M2", "--seconds", "2", "--simulate", "--session", str(tmp_path)])
    assert rc == 0 and (tmp_path / "M2_00.h5").is_file() and (tmp_path / "manifest.json").is_file()
    assert bench.main(["analyze", "--session", str(tmp_path)]) == 0
    assert (tmp_path / "analysis.json").is_file()


def test_main_private_mode_needs_the_service_stopped_and_satisfies_the_bias_t_rule():
    assert "still running" in refusal("M1", main_private=True)
    assert refusal("M1", main_private=True, main_listening=False) is None   # no -T at all: no DC block needed
