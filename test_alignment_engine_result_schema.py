"""Pre-hardware pass, item 8: alignment_result.json must distinguish
tangent-plane offset, RA/Dec coordinate difference, and absolute
expected/measured coordinates - never conflate them under an ambiguous
"offset_ra_deg" name alone (legacy aliases are kept, not removed).
"""
import warnings

import astropy.units as u
from astropy.coordinates import EarthLocation

warnings.filterwarnings("ignore", module="astropy")

from alignment_engine.config import AlignmentConfig
from alignment_engine.engine import AlignmentEngine
from alignment_engine.mount_adapter import SimulatedMountAdapter
from alignment_engine.simulation import HIMapSimConfig, SolarBeamSimConfig
from alignment_engine.targets.hi_reference import SyntheticHIReferenceProvider
from alignment_engine.targets.solar import SolarTarget
from alignment_engine.tracking import SimulatedTrackingBackend, TrackingMode

LOCATION = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m)


def _solar_result(tmp_path):
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    config.solar.coarse_span_deg, config.solar.coarse_spacing_deg = 14.0, 3.5
    config.solar.fine_span_deg, config.solar.fine_spacing_deg = 5.0, 1.5
    config.solar.min_altitude_deg = -90.0
    engine = AlignmentEngine("solar", config, LOCATION, SimulatedMountAdapter(),
                              SimulatedTrackingBackend(TrackingMode.SIDEREAL))
    engine.plan()
    engine.preflight(SolarTarget(LOCATION))
    sim = SolarBeamSimConfig(true_offset_east_deg=1.2, true_offset_north_deg=-0.7,
                              fwhm_deg=20.0, noise_fraction=0.0, seed=7)
    return engine.run_solar_simulated(sim)


def test_tangent_offset_and_legacy_aliases_agree(tmp_path):
    result = _solar_result(tmp_path)
    payload = result.to_dict()
    assert payload["tangent_east_deg"] == payload["offset_ra_deg"]
    assert payload["tangent_north_deg"] == payload["offset_dec_deg"]


def test_expected_and_measured_coordinates_are_present_and_icrs(tmp_path):
    result = _solar_result(tmp_path)
    payload = result.to_dict()
    for key in ("expected_coordinate", "measured_coordinate"):
        assert payload[key]["frame"] == "ICRS"
        assert isinstance(payload[key]["ra_hours"], float)
        assert isinstance(payload[key]["dec_deg"], float)
    # measured must actually differ from expected by the injected offset,
    # not be an accidental copy of it
    assert payload["measured_coordinate"] != payload["expected_coordinate"]


def test_ra_dec_delta_is_not_silently_equal_to_the_tangent_offset(tmp_path):
    """The whole point of Fase 8: at a non-zero declination, a raw RA
    coordinate difference (in degrees) is NOT the same number as the
    tangent-plane east offset - this test fails if a future refactor
    collapses the two back into one ambiguous quantity."""
    result = _solar_result(tmp_path)
    payload = result.to_dict()
    delta = payload["ra_dec_delta_deg"]
    assert "note" in delta  # documents the caveat, not just raw numbers
    # only an exact equality would indicate the two concepts were conflated;
    # a real declination away from 0 makes this a meaningfully different
    # number (cos(dec) factor), which the reference center here (an actual
    # Sun position) essentially always is.
    if abs(payload["expected_coordinate"]["dec_deg"]) > 1.0:
        assert delta["delta_ra_deg"] != payload["tangent_east_deg"]


def test_hi_result_also_carries_the_normalized_schema(tmp_path):
    config = AlignmentConfig.load()
    config.global_.output_root = str(tmp_path)
    config.global_.orchestrator_runtime_dir = str(tmp_path / "orchestrator_runtime")
    config.hi.raster_span_deg, config.hi.raster_spacing_deg = 12.0, 3.0
    config.hi.min_altitude_deg = -90.0
    engine = AlignmentEngine("hi", config, LOCATION, SimulatedMountAdapter(),
                              SimulatedTrackingBackend(TrackingMode.SIDEREAL))
    engine.plan()
    provider = SyntheticHIReferenceProvider()
    engine.preflight(provider)
    sim = HIMapSimConfig(true_offset_east_deg=-0.8, true_offset_north_deg=1.5,
                          gain_a=2.3, baseline_b=5.0, noise_fraction=0.02, seed=11)
    result = engine.run_hi_simulated(provider, sim)
    payload = result.to_dict()
    assert "tangent_east_deg" in payload
    assert "expected_coordinate" in payload
    assert "measured_coordinate" in payload


def test_reconstructed_result_without_reference_center_omits_coordinate_fields_not_crashes():
    """almita_align.py's cmd_sync reconstructs an AlignmentResult from
    persisted JSON without recomputing an ephemeris position - to_dict()
    must degrade gracefully (tangent/legacy fields still present), never
    raise, when reference_center is unknown."""
    from alignment import AlignmentEstimate
    from alignment_engine.engine import AlignmentResult
    from alignment_engine.fitting import FitQuality, FitResult

    estimate = AlignmentEstimate(1.0, -1.0, 1.4, 0.9, 0.05, 0.95, 20)
    quality = FitQuality(confidence=0.9, correlation=0.95, residual_rms=0.05,
                          valid_fraction=1.0, peak_contrast=0.5, distance_from_edge_deg=2.0, rating="GOOD")
    fit_result = FitResult(estimate=estimate, quality=quality, raw_values=[], valid_count=20,
                            total_count=20, rejected_indices=[])
    result = AlignmentResult(mode="SOLAR", session_id="X", coarse_fit=None, fine_fit=None,
                              final_fit=fit_result, tracking_mode=None, warnings=[])
    payload = result.to_dict()
    assert payload["tangent_east_deg"] == 1.0
    assert "expected_coordinate" not in payload
    assert "measured_coordinate" not in payload
