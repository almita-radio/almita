"""QUALITY ADVERSARIAL MATRIX + FALSE-GOOD / FALSE-BAD (2nd-pass sections
22-24). Builds mask/n_contributing/etc combinations representing each
named scenario, records expected-vs-actual quality state, and reports an
explicit false-GOOD and false-BAD count - never just "looks reasonable".
"""
import numpy as np
import pytest

from reduce_engine.models import MaskFlag, QualityState
from reduce_engine.quality import assess_quality

GOOD = MaskFlag.GOOD.value
N = 200


def _mask(rfi_fraction=0.0, edge_fraction=0.0, invalid_fraction=0.0):
    mask = np.full(N, GOOD, dtype=np.int64)
    n_rfi = int(N * rfi_fraction)
    n_edge = int(N * edge_fraction)
    n_invalid = int(N * invalid_fraction)
    mask[:n_rfi] = MaskFlag.RFI.value
    mask[n_rfi:n_rfi + n_edge] = MaskFlag.EDGE.value
    mask[n_rfi + n_edge:n_rfi + n_edge + n_invalid] = MaskFlag.INVALID.value
    return mask


# Each scenario: kwargs for assess_quality + the "healthy" (never-BAD,
# reasonable) expectation this scenario represents. "expected_bucket" is
# GOOD/WARNING/BAD/UNKNOWN groups a human would accept as correct;
# anything outside that bucket for a clearly-healthy or clearly-bad
# scenario counts toward false-GOOD / false-BAD below.
SCENARIOS = {
    "healthy": dict(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.0,
                    calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                    baseline_fit_quality_rms_fraction=0.03, velocity_frame="lsrk",
                    truly_bad=False, truly_healthy=True),
    "moderate_rfi": dict(mask=_mask(rfi_fraction=0.15), n_contributing=np.ones(N), clipping_fraction=0.0,
                         calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                         baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk",
                         truly_bad=False, truly_healthy=True),
    "extreme_rfi": dict(mask=_mask(rfi_fraction=0.85), n_contributing=np.ones(N), clipping_fraction=0.0,
                        calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                        baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk",
                        truly_bad=True, truly_healthy=False),
    "heavy_mask": dict(mask=_mask(edge_fraction=0.7), n_contributing=np.ones(N), clipping_fraction=0.0,
                       calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                       baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk",
                       truly_bad=True, truly_healthy=False),
    "bad_baseline": dict(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.0,
                         calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                         baseline_fit_quality_rms_fraction=0.9, velocity_frame="lsrk",
                         truly_bad=True, truly_healthy=False),
    "no_calibration": dict(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.0,
                           calibration_level="UNCALIBRATED", calibration_compatibility_status="NOT_ATTEMPTED",
                           baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk",
                           truly_bad=False, truly_healthy=False),  # honest limitation, not "healthy" nor "bad"
    "missing_velocity": dict(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.0,
                             calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                             baseline_fit_quality_rms_fraction=0.05, velocity_frame="UNAVAILABLE",
                             truly_bad=False, truly_healthy=False),
    "single_capture": dict(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.0,
                           calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                           baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk",
                           truly_bad=False, truly_healthy=True),
    "clipping": dict(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.02,
                     calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                     baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk",
                     truly_bad=True, truly_healthy=False),
    "no_contributing_spectra": dict(mask=_mask(), n_contributing=np.zeros(N), clipping_fraction=0.0,
                                    calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                                    baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk",
                                    truly_bad=True, truly_healthy=False),
    "nan_baseline_rms": dict(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.0,
                             calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                             baseline_fit_quality_rms_fraction=float("nan"), velocity_frame="lsrk",
                             truly_bad=True, truly_healthy=False),  # unknown baseline quality must never read as GOOD
}


def test_quality_adversarial_matrix_and_false_rate_report():
    false_good = []
    false_bad = []
    report_rows = []
    for name, scenario in SCENARIOS.items():
        kwargs = {k: v for k, v in scenario.items() if k not in ("truly_bad", "truly_healthy")}
        result = assess_quality(**kwargs)
        report_rows.append((name, result.state, scenario["truly_bad"], scenario["truly_healthy"]))
        if result.state == QualityState.GOOD and scenario["truly_bad"]:
            false_good.append(name)
        if result.state == QualityState.BAD and scenario["truly_healthy"]:
            false_bad.append(name)

    print("\nQUALITY ADVERSARIAL MATRIX")
    for name, state, truly_bad, truly_healthy in report_rows:
        print(f"  {name:24s} -> {state:8s}  (truly_bad={truly_bad}, truly_healthy={truly_healthy})")
    print(f"  FALSE-GOOD count: {len(false_good)} {false_good}")
    print(f"  FALSE-BAD count:  {len(false_bad)} {false_bad}")

    assert false_good == [], f"FALSE-GOOD (data clearly bad but rated GOOD): {false_good}"
    assert false_bad == [], f"FALSE-BAD (healthy data rated BAD): {false_bad}"


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_scenario_has_at_least_one_reason(name):
    scenario = SCENARIOS[name]
    kwargs = {k: v for k, v in scenario.items() if k not in ("truly_bad", "truly_healthy")}
    result = assess_quality(**kwargs)
    assert len(result.reasons) > 0


def test_mixed_gains_and_mixed_sample_rates_are_a_calibration_incompatibility_not_a_quality_free_pass():
    """A mixed-gain or mixed-sample-rate scenario is caught upstream, at
    the calibration compatibility check (test_reduce_calibration_matrix.py),
    not by quality directly - quality only ever sees the resulting
    UNCALIBRATED/INCOMPATIBLE state and must not paper over it as GOOD."""
    result = assess_quality(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.0,
                            calibration_level="UNCALIBRATED", calibration_compatibility_status="INCOMPATIBLE",
                            baseline_fit_quality_rms_fraction=0.05, velocity_frame="lsrk")
    assert result.state != QualityState.GOOD
    assert any("INCOMPATIBLE" in r or "calibration" in r.lower() for r in result.reasons)


def test_nan_and_inf_baseline_rms_never_crash_quality_assessment():
    for value in (float("nan"), float("inf"), float("-inf")):
        result = assess_quality(mask=_mask(), n_contributing=np.ones(N), clipping_fraction=0.0,
                                calibration_level="RELATIVE", calibration_compatibility_status="COMPATIBLE",
                                baseline_fit_quality_rms_fraction=value, velocity_frame="lsrk")
        assert result.state in QualityState.VALID
