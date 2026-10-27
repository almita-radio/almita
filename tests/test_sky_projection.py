"""ALIGN/OBSERVE sky time slider (operator 2026-10-27): the projected sky layer (no hardware; HI4PI layer stubbed)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import almita_web_ops as ops

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _stub_layer(monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "_hi_plan_sky", lambda plan: calls.append(plan) or {"hi4pi_grid": {"n": 1, "values_1e20cm2": [1.0],
                                                                                                  "value_range_1e20cm2": [0.0, 2.0]}})
    ops._SKY_PROJECTION_CACHE.clear()
    return calls


def _at(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_projection_gives_the_sun_and_the_layer_at_that_minute(_stub_layer):
    r = ops.sky_projection(_at(8), 20.0)
    assert r["projected_utc"].endswith(":00Z") and set(r["sun"]) == {"az_deg", "alt_deg", "above_horizon"}
    assert r["hi4pi_grid"]["n"] == 1 and r["observer"]["latitude_deg"] < 0
    assert _stub_layer[-1] == {"generated_utc": r["projected_utc"][:-1], "beam_fwhm_deg": 20.0}


def test_projection_is_cached_per_minute_and_beam(_stub_layer):
    at = _at(4)
    ops.sky_projection(at, 20.0)
    ops.sky_projection(at, 20.0)
    assert len(_stub_layer) == 1
    ops.sky_projection(at, 14.0)
    assert len(_stub_layer) == 2


@pytest.mark.parametrize("bad", ["nope", "2026-01-01T00:00:00Z", None])
def test_instants_outside_now_to_plus_25_h_are_refused(bad):
    with pytest.raises(ValueError):
        ops.sky_projection(_at(30) if bad is None else bad, 20.0)


def test_pages_carry_the_slider_and_the_magellanic_clouds():
    common = (ROOT / "console" / "common.js").read_text()
    for page in ("align", "observe"):
        js = (ROOT / "console" / f"{page}.js").read_text()
        assert "U.skyTimeSlider(" in js and "U.magellanicMarkers(" in js and "U.fetchSkyProjection(" in js
    assert 'id="real-sky-time"' in (ROOT / "console" / "align.html").read_text()
    assert 'id="est-sky-time"' in (ROOT / "console" / "observe.html").read_text()
    assert "U.SKY_PROJECTION_STEP_H = 2" in common and '"LMC"' in common and '"SMC"' in common
    assert '"/api/ops/sky/projection"' in (ROOT / "almita_orchestrator_server.py").read_text()
    # a projected OBSERVE map never centres the region (that would plan another hour's sky)
    assert "if (proj) return;" in (ROOT / "console" / "observe.js").read_text()
