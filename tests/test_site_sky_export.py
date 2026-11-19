"""STATUS / site sky map: valid-session selection and the projection (site_sky_export.py)."""
import math

import numpy as np

import site_sky_export as sse


def test_only_frequency_switched_points_count_as_switched():
    assert sse.is_frequency_switched({"capture_refs": [{"fswitch_offset_hz": 600000}]})
    assert not sse.is_frequency_switched({"capture_refs": [{"center_frequency_hz": 1420405752}]})


def test_mollweide_inverse_centre_edges_and_outside():
    l, b, inside = sse.mollweide_inverse(np.array([0.0, 0.0, 2 * math.sqrt(2) - 1e-9, 2.8]), np.array([0.0, math.sqrt(2) - 1e-9, 0.0, 1.4]))
    assert inside.tolist() == [True, True, True, False]
    assert abs(l[0]) < 1e-9 and abs(b[0]) < 1e-9          # galactic centre in the middle
    assert b[1] > 89.9                                     # top = north galactic pole
    assert abs(l[2] - 180.0) < 1e-6                         # right edge = anticentre (l grows to the left)


def test_left_of_centre_is_positive_longitude():
    l, _, _ = sse.mollweide_inverse(np.array([-0.5]), np.array([0.0]))
    assert 0 < l[0] < 90


def test_sessions_from_catalog_keeps_real_mount_with_reduce_only(tmp_path, monkeypatch):
    cat = tmp_path / "data" / "sessions"
    cat.mkdir(parents=True)
    (cat / "catalog_index.json").write_text(
        '{"sessions":['
        '{"campaign_id":"A","started_utc":"2026-11-12T22:38:42Z","mount":"LX200 OnStep","reduce":{"runs":["R1"]}},'
        '{"campaign_id":"B","started_utc":"2026-11-18T02:54:01Z","mount":"Telescope Simulator","reduce":{"runs":["R2"]}},'
        '{"campaign_id":"C","started_utc":"2026-11-11T15:15:47Z","mount":"LX200 OnStep","reduce":{"runs":[]}}]}')
    monkeypatch.setattr(sse, "ROOT", tmp_path)
    out = sse.sessions_from_catalog()
    assert [s["campaign_id"] for s in out] == ["A"]


def test_line_area_ignores_a_flat_offset_and_counts_only_the_line():
    v = np.arange(-84, 85, 2.0)
    line = 10.0 * np.exp(-0.5 * (v / 10.0) ** 2)
    a = sse.line_area(v, line)
    assert abs(a - 10.0 * 10.0 * math.sqrt(2 * math.pi)) < 15       # ~ the Gaussian's area
    assert abs(sse.line_area(v, line + 3.0) - a) < 1e-6             # a baseline offset is removed at the edges
    assert sse.line_area(v, np.zeros_like(v)) == 0.0


def test_deconvolution_sharpens_a_bright_band_and_leaves_a_flat_sky_flat():
    # a 5x5 grid every 7.5 deg across a narrow bright band at b=0, seen through the 20 deg beam
    pts = []
    for i in range(5):
        for j in range(5):
            l, b = 20 + (i - 2) * 7.5, (j - 2) * 7.5
            pts.append({"l": l, "b": b, "integral_pct_kms": 100 + 500 * math.exp(-0.5 * (b / 9.0) ** 2)})
    out = sse.deconvolve_session(pts, 7.5, iterations=20)
    centre = max(v for l, b, v in out["grid"] if abs(b) < 1)
    assert centre > 600                     # sharper than the measured peak (600): the beam's blur partly undone
    flat = [{"l": p["l"], "b": p["b"], "integral_pct_kms": 50.0} for p in pts]
    fo = sse.deconvolve_session(flat, 7.5)
    assert all(abs(v - 50.0) < 1.0 for _, _, v in fo["grid"])
    assert fo["iterations"] in sse.ITERATION_CHOICES


def test_survey_batches_share_one_key_and_other_campaigns_have_none():
    assert sse.survey_key("ALMITA-SURVEY-SCP-PETORCA-20261207-01:02:03") == "ALMITA-SURVEY-SCP-PETORCA"
    assert sse.survey_key("ALMITA-OBSERVE-20261112-22:38:42") is None


def test_merge_survey_passes_averages_one_tile_and_leaves_other_sessions_alone():
    v = list(range(-80, 81, 2))
    def pt(s, l, b, val):
        return {"s": s, "l": l, "b": b, "integral_pct_kms": val, "spec_v": v, "spec_y": [val / 10] * len(v), "t_utc": "x"}
    pts = [pt(0, 300.0, -27.0, 10.0), pt(0, 300.2, -27.1, 20.0), pt(0, 310.0, -27.0, 5.0), pt(1, 300.0, -27.0, 99.0)]
    out = sse.merge_survey_passes(pts, {0})
    merged = [p for p in out if p["s"] == 0]
    assert len(merged) == 2 and len([p for p in out if p["s"] == 1]) == 1
    first = next(p for p in merged if p["passes"] == 2)
    assert first["integral_pct_kms"] == 15.0 and first["spec_y"][0] == 1.5
