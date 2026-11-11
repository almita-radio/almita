"""kelvin_scale.py: relative spectra x the wizard's cold-sky Tsys = antenna temperature; refuses without a VALID
Y-factor; never touches REDUCE's own files; eta against HI4PI only where a tile holds the beam."""
import json

import numpy as np
import pytest

import kelvin_scale as ks
import rotation_curve as rc
from tests.test_rotation_curve import _write_session


def _wizard(tmp_path, y):
    d = tmp_path / "WIZARD-X"
    d.mkdir()
    (d / "wizard_state.json").write_text(json.dumps({"y_factor": y}))
    return d


VALID = {"verdict": "VALID", "tsys_sky_k": 300.0, "y_db": 1.53, "t_load_k": 293.0,
         "t_rx_assumption": "datasheet", "spread_for_t_rx": [{"tsys_sky_k": 270.0}, {"tsys_sky_k": 330.0}]}


def test_relative_times_tsys_and_the_session_left_untouched(tmp_path):
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    c = SkyCoord(l=40 * u.deg, b=0 * u.deg, frame="galactic").icrs
    v = np.arange(-150, 150, 0.5)
    rel = 0.10 * np.exp(-0.5 * ((v - 20) / 12) ** 2)
    sess = _write_session(tmp_path / "R", [(c.ra.hour, c.dec.deg, v, rel)])
    before = sorted(p.name for p in sess.rglob("*"))
    r = ks.scale(sess, _wizard(tmp_path, VALID), hi4pi=rc.Hi4piTiles(tmp_path / "none"))
    p = r["points"][0]
    assert p["peak_ta_k"] == pytest.approx(30.0, rel=0.03) and p["peak_v_km_s"] == pytest.approx(20, abs=1)
    assert p["peak_ta_k_range"][0] == pytest.approx(27.0, rel=0.03) and p["peak_ta_k_range"][1] == pytest.approx(33.0, rel=0.03)
    assert p["integral_k_km_s"] == pytest.approx(300 * 0.10 * 12 * np.sqrt(2 * np.pi), rel=0.02)
    assert r["eta_median"] is None and "hi4pi" not in p
    after = sorted(p.name for p in sess.rglob("*") if "kelvin" not in p.parts)
    assert after == before                                          # REDUCE's own files untouched


def test_it_refuses_without_a_valid_y_factor(tmp_path):
    cases = ((None, "no Y-factor"), ({"verdict": "Y_ONLY", "reason": "no load temperature"}, "not VALID"))
    for k, (y, why) in enumerate(cases):
        base = tmp_path / f"case{k}"
        base.mkdir()
        w = _wizard(base, y)
        with pytest.raises(ValueError, match=why):
            ks.load_y_factor(w)
        assert ks.main([str(tmp_path / "missing"), str(w)]) == 2
