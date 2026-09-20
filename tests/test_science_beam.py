"""Beam weight formula (section 24-25)."""
import numpy as np
import pytest

from science_engine.beam import beam_footprint_radius_deg, beam_weight
from science_engine.models import BeamModel


def test_beam_weight_is_one_at_center():
    beam = BeamModel(fwhm_deg=20.0)
    assert np.isclose(beam_weight(np.array([0.0]), beam)[0], 1.0)


def test_beam_weight_is_half_at_half_fwhm():
    """The literal definition of FWHM: response at theta=FWHM/2 is 0.5."""
    beam = BeamModel(fwhm_deg=20.0)
    assert np.isclose(beam_weight(np.array([10.0]), beam)[0], 0.5, atol=1e-9)


@pytest.mark.parametrize("fwhm", [1.0, 5.0, 20.0, 45.0])
def test_beam_weight_half_at_half_fwhm_for_various_fwhm(fwhm):
    beam = BeamModel(fwhm_deg=fwhm)
    assert np.isclose(beam_weight(np.array([fwhm / 2]), beam)[0], 0.5, atol=1e-9)


def test_beam_weight_decreases_monotonically_with_theta():
    beam = BeamModel(fwhm_deg=20.0)
    thetas = np.linspace(0, 30, 50)
    weights = beam_weight(thetas, beam)
    assert np.all(np.diff(weights) <= 0)


def test_beam_weight_is_zero_beyond_cutoff():
    beam = BeamModel(fwhm_deg=10.0, cutoff_n_fwhm=3.0)
    weights = beam_weight(np.array([29.9, 30.0, 30.1, 100.0]), beam)
    assert weights[0] > 0
    assert weights[2] == 0.0
    assert weights[3] == 0.0


def test_beam_footprint_radius():
    beam = BeamModel(fwhm_deg=10.0, cutoff_n_fwhm=3.0)
    assert beam_footprint_radius_deg(beam) == 30.0


def test_beam_model_rejects_nonpositive_fwhm():
    with pytest.raises(ValueError):
        BeamModel(fwhm_deg=0.0)
    with pytest.raises(ValueError):
        BeamModel(fwhm_deg=-5.0)
