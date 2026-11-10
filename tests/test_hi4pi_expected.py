"""BL-099 (operator 2026-11-06): under SCIENCE's A/B/C, what HI4PI says the same area should look like - same frame,
angle and extent, A in SCIENCE's own cells, B/C only where SCIENCE has data. Comparison only: a missing, disabled
or broken HI4PI file is reported and never sinks a SCIENCE run."""
import json

import numpy as np
import pytest
from astropy.io import fits

import hi4pi_expected
from tests.test_science_polar_grid import _campaign
from tests.test_science_web_bridge import cfg_with
from tests.test_web_frontend import SCIENCE_DRIVER, SCIENCE_RESULT_ROUTES, SCIENCE_ROUTES, _science_facts, run_page

BAND_DEC = -26.0


def _sky(path, band_dec=BAND_DEC):
    """A synthetic all-sky equatorial CAR NHI map (1 deg pixels): a faint floor plus a bright band along Dec=band_dec."""
    ra = np.arange(360) * -1.0          # CDELT1 = -1, like HI4PI's own CAR maps
    dec = np.arange(-90, 91) * 1.0
    data = 1e20 + 2e21 * np.exp(-0.5 * ((dec[:, None] - band_dec) / 3.0) ** 2) * np.ones((1, ra.size))
    h = fits.Header()
    h.update(CTYPE1="RA---CAR", CTYPE2="DEC--CAR", CDELT1=-1.0, CDELT2=1.0, CRPIX1=1.0, CRPIX2=91.0, CRVAL1=0.0, CRVAL2=0.0)
    fits.PrimaryHDU(data.astype(np.float32), header=h).writeto(path)
    return path


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    import science_web_bridge as swb
    si, _ = _campaign(tmp_path_factory.mktemp("camp"), 6.0, -30.0, 8.0, 5)
    cfg = cfg_with(beam_fwhm_deg=3.0)
    return swb.build_all_products(si, cfg), cfg


def test_expected_map_is_drawn_in_sciences_own_cells_and_follows_the_sky(tmp_path, built):
    b, cfg = built
    out = hi4pi_expected.expected_products(b, cfg, tmp_path, path=_sky(tmp_path / "nhi.fits"))
    assert out["status"] == "OK", out
    assert (tmp_path / "map_hi4pi_expected.png").stat().st_size > 10_000 and (tmp_path / "map_hi4pi_expected.svg").exists()
    assert out["n_points_compared"] == 25 and out["beam_fwhm_deg"] == 3.0
    lo, hi = out["expected_range_k_km_s"]
    assert 0 < lo < hi
    # the band sits north of the field: what HI4PI expects at each of SCIENCE's own cells rises with Dec
    import science_web_bridge as swb
    cells = swb.board_json_payload(b)["cells"]
    sky = hi4pi_expected._Beam(*hi4pi_expected._load_sky(tmp_path / "nhi.fits", 3.0), 3.0)
    exp = np.array([sky.at(c["ra_deg"], c["dec_degrees"]) for c in cells])
    dec = np.array([c["dec_degrees"] for c in cells])
    assert np.corrcoef(exp, dec)[0, 1] > 0.95


def test_missing_disabled_or_broken_hi4pi_is_reported_never_raised(tmp_path, built, monkeypatch):
    b, cfg = built
    assert hi4pi_expected.expected_products(b, cfg, tmp_path, path=tmp_path / "nope.fits")["status"] == "UNAVAILABLE"
    assert hi4pi_expected.expected_products(b, cfg, tmp_path, path=None)["status"] == "DISABLED"
    monkeypatch.setenv(hi4pi_expected.ENV, "off")
    assert hi4pi_expected.expected_products(b, cfg, tmp_path)["status"] == "DISABLED"
    broken = tmp_path / "broken.fits"
    broken.write_bytes(b"not a fits file")
    out = hi4pi_expected.expected_products(b, cfg, tmp_path, path=broken)
    assert out["status"] == "FAILED" and out["reason"]
    assert not (tmp_path / "map_hi4pi_expected.png").exists()


def _routes(hi):
    manifest_extra = {"hi4pi_expected": hi, "exports": {"map_hi4pi_expected": ["map_hi4pi_expected.png"]}} if hi else {}
    routes = SCIENCE_ROUTES % json.dumps(_science_facts({})) + SCIENCE_RESULT_ROUTES
    if manifest_extra:
        routes += ('Object.assign(window.__routes["GET /api/ops/file"].body, ' + json.dumps(manifest_extra) + ');\n')
    return routes


DRIVER = SCIENCE_DRIVER + r"""
$("btn-run").click();
await until(() => !$("results-panel").hidden && $("hi4pi-note").textContent !== "", 8000);
const h3 = Array.from(document.querySelectorAll("#results-panel h3")).map((e) => e.textContent);
out.order = [h3.findIndex((t) => t.startsWith("COMBINED A/B/C")), h3.findIndex((t) => t.startsWith("HI4PI")), h3.findIndex((t) => t.startsWith("DOWNLOADS"))];
out.hidden = $("link-hi4pi").hidden; out.src = $("img-hi4pi").getAttribute("src") || ""; out.note = $("hi4pi-note").textContent;
"""


def test_science_page_shows_hi4pi_right_under_the_abc(tmp_path):
    hi = {"status": "OK", "files": ["map_hi4pi_expected.png"], "beam_fwhm_deg": 20, "correlation_with_a": 0.6995,
          "n_points_compared": 80, "units": "K km/s (NHI / 1.823e18, optically thin)", "velocity_note": "compare shapes"}
    out = run_page(tmp_path, "science", _routes(hi), DRIVER, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["errors"] == []
    a, h, d = out["order"]
    assert 0 <= a < h < d                       # immediately under the combined A/B/C, before the downloads
    assert out["hidden"] is False and "map_hi4pi_expected.png" in out["src"]
    assert "r = 0.70 (80 points)" in out["note"] and "20° beam" in out["note"]


def test_science_page_says_so_when_a_run_has_no_hi4pi_panel(tmp_path):
    out = run_page(tmp_path, "science", _routes(None), DRIVER, budget=20000)
    assert "driver_error" not in out, out.get("driver_error")
    assert out["hidden"] is True and "not available for this run" in out["note"]
