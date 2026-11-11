"""lobe_aeff.py: the dish's effective area from a solar transit and the Sun's published 1415 MHz flux."""
import json
import math

from pathlib import Path

import pytest

import lobe_aeff as la


def _run(tmp_path, fit, fit_dec=None, beam=None, plan=None):
    d = tmp_path / "LOBE"
    d.mkdir()
    res = {"status": "COMPLETE", "fit": fit, "plan": plan or {"computed_utc": "2026-11-11T13:00:00Z", "duration_s": 1344}}
    if fit_dec:
        res.update(fit_dec=fit_dec, beam=beam)
    (d / "lobe_result.json").write_text(json.dumps(res))
    return d


TSYS = {"tsys_k": 300.0, "tsys_low_k": 270.0, "tsys_high_k": 330.0, "source": "test"}


def test_effective_area_from_a_known_case(tmp_path):
    # a 0.25 m^2 dish, 60 sfu, one polarisation: delta T_A = 0.25 * 60e-22 / (2k) = 54.3 K; over Tsys 300 K -> ratio 0.181
    ratio = 0.25 * 60e-22 / (2 * la.K_BOLTZMANN) / 300.0
    d = _run(tmp_path, {"status": "FIT", "peak_over_background": ratio})
    r = la.analyse(d, 60.0, TSYS, geometric_area_m2=0.42)
    assert r["sun_flux"]["source"] == "given by hand"
    assert r["effective_area_m2"] == pytest.approx(0.25, rel=1e-6) and r["delta_t_a_k"] == pytest.approx(54.3, abs=0.1)
    lo, hi = r["effective_area_range_m2"]
    assert lo < 0.25 < hi and r["aperture_efficiency"] == pytest.approx(0.25 / 0.42)
    assert r["peak"]["cuts"] == 1 and "CROSS" in r["peak"]["note"]
    assert (d / "aeff" / "lobe_aeff.json").is_file() and sorted(p.name for p in d.iterdir()) == ["aeff", "lobe_result.json"]


def test_a_cross_run_corrects_each_cut_for_the_pointing_error_and_checks_the_beam(tmp_path):
    true = 0.18
    beam = {"fwhm_ra_deg": 18.0, "fwhm_dec_deg": 26.0, "peak_offset_ra_deg": 3.0, "peak_offset_dec_deg": -4.0,
            "solid_angle_sr": 1.1331 * math.radians(18) * math.radians(26)}
    seen_ra = true * la.cross_axis_loss(-4.0, 26.0)        # the RA cut passed 4 deg beside the Sun in Dec
    seen_dec = true * la.cross_axis_loss(3.0, 18.0)
    d = _run(tmp_path, {"status": "FIT", "peak_over_background": seen_ra}, {"status": "FIT", "peak_over_background": seen_dec}, beam)
    r = la.analyse(d, 60.0, TSYS)
    assert r["peak"]["ratio"] == pytest.approx(true, rel=1e-9) and r["peak"]["cuts"] == 2
    a = r["effective_area_m2"]
    assert r["main_beam_efficiency"] == pytest.approx(a * beam["solid_angle_sr"] / la.WAVELENGTH_M ** 2)
    assert r["max_effective_area_from_beam_m2"] == pytest.approx(la.WAVELENGTH_M ** 2 / beam["solid_angle_sr"])


def test_an_impossible_main_beam_efficiency_is_flagged(tmp_path):
    beam = {"fwhm_ra_deg": 40.0, "fwhm_dec_deg": 40.0, "peak_offset_ra_deg": 0.0, "peak_offset_dec_deg": 0.0,
            "solid_angle_sr": 1.1331 * math.radians(40) ** 2}
    f = {"status": "FIT", "peak_over_background": 0.5}
    r = la.analyse(_run(tmp_path, f, f, beam), 60.0, TSYS)
    assert r["main_beam_efficiency"] > 1 and "impossible" in r["warning"]


def test_it_refuses_a_bad_fit_a_bad_flux_and_a_missing_y_factor(tmp_path):
    d = _run(tmp_path, {"status": "FIT WITH WARNINGS", "peak_over_background": 0.1, "flags": ["PEAK AT THE EDGE"]})
    with pytest.raises(ValueError, match="no clean transit"):
        la.analyse(d, 60.0, TSYS)
    with pytest.raises(ValueError, match="sfu"):
        la.analyse(d, 0.0, TSYS)
    w = tmp_path / "WIZ"
    w.mkdir()
    (w / "wizard_state.json").write_text(json.dumps({"y_factor": {"verdict": "Y_ONLY", "reason": "no load temperature"}}))
    assert la.main([str(d), "--sun-flux-sfu", "60", "--wizard", str(w)]) == 2
    (w / "wizard_state.json").write_text(json.dumps({"y_factor": {"verdict": "VALID", "tsys_sky_k": 300.0, "y_db": 2.3,
                                                                  "spread_for_t_rx": [{"tsys_sky_k": 280.0}, {"tsys_sky_k": 320.0}]}}))
    t = la.load_tsys(w)
    assert (t["tsys_k"], t["tsys_low_k"], t["tsys_high_k"]) == (300.0, 280.0, 320.0)


FIXTURE = Path(__file__).parent / "fixtures" / "solar_radio_flux_20261111.json"     # NOAA SWPC, fetched 2026-11-11


def test_the_flux_is_the_good_1415_value_closest_to_the_transit_and_the_spread_its_uncertainty():
    from datetime import datetime, timezone
    recs = json.loads(FIXTURE.read_text())
    f = la.flux_from_rstn(recs, datetime(2026, 11, 10, 16, 0, tzinfo=timezone.utc))
    st = sorted((v["station"], v["flux_sfu"]) for v in f["values"])
    # within 18 h of 2026-11-10 16:00 UTC: Learmonth 89 (11-10 04:52), San Vito 93, Sagamore Hill 82, Learmonth 92 (11-11 04:0x,
    # reported twice: counted once); Kaena Point 74 is "Uncertain" -> left out; San Vito 79 (11-11 10:34) is 18.6 h away
    assert st == [("Learmonth", 89.0), ("Learmonth", 92.0), ("Sagamore Hill", 82.0), ("San Vito", 93.0)]
    assert f["chosen"]["station"] == "Sagamore Hill" and f["flux_sfu"] == 82.0          # 17:19 UTC: 1.3 h from 16:00
    assert f["median_sfu"] == 90.5 and f["spread_fraction"] == pytest.approx((93 - 82) / 2 / 82.0)
    with pytest.raises(ValueError, match="by hand"):
        la.flux_from_rstn(recs, datetime(2025, 1, 1, tzinfo=timezone.utc))


def test_download_keeps_a_copy_and_offline_reuses_it(tmp_path):
    plan = {"computed_utc": "2026-11-10T15:50:00Z", "duration_s": 1200}
    d = _run(tmp_path, {"status": "FIT", "peak_over_background": 0.2}, plan=plan)

    def fake_download(dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(FIXTURE.read_text())
        return dest
    r = la.analyse(d, None, TSYS, download=fake_download)
    assert r["sun_flux_sfu"] == 82.0 and r["sun_flux"]["fetched"] == "downloaded"
    assert r["flux_uncertainty"] == pytest.approx(max(0.10, 5.5 / 82.0))
    assert (d / "aeff" / "solar_radio_flux.json").is_file()

    def offline(dest):
        raise OSError("no network")
    r2 = la.analyse(d, None, TSYS, download=offline)
    assert r2["sun_flux_sfu"] == 82.0 and r2["sun_flux"]["fetched"].startswith("offline")
    d2 = tmp_path / "other"
    d2.mkdir()
    (d2 / "lobe_result.json").write_text((d / "lobe_result.json").read_text())
    with pytest.raises(ValueError, match="no copy is kept"):
        la.analyse(d2, None, TSYS, download=offline)
