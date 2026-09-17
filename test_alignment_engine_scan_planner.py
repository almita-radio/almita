import pytest

from alignment_engine.scan_planner import build_coarse_then_fine, build_raster, total_angular_span_deg


def test_build_raster_covers_requested_span():
    points = build_raster(span_deg=14.0, spacing_deg=3.5)
    eastings = [p.east_deg for p in points]
    northings = [p.north_deg for p in points]
    assert max(eastings) - min(eastings) == pytest.approx(14.0, abs=1e-9)
    assert max(northings) - min(northings) == pytest.approx(14.0, abs=1e-9)
    assert min(eastings) == pytest.approx(-7.0, abs=1e-9)
    assert max(eastings) == pytest.approx(7.0, abs=1e-9)


def test_build_raster_is_serpentine_ordered():
    points = build_raster(span_deg=6.0, spacing_deg=3.0)
    rows = sorted(set(p.row for p in points))
    for row in rows:
        row_points = [p for p in points if p.row == row]
        cols = [p.col for p in row_points]
        assert cols == sorted(cols) or cols == sorted(cols, reverse=True)


def test_build_raster_rejects_nonpositive_span_or_spacing():
    with pytest.raises(ValueError):
        build_raster(span_deg=0, spacing_deg=1.0)
    with pytest.raises(ValueError):
        build_raster(span_deg=10.0, spacing_deg=0)


def test_default_solar_spans_from_brief_produce_reasonable_point_counts():
    coarse = build_raster(span_deg=14.0, spacing_deg=3.5)
    fine = build_raster(span_deg=5.5, spacing_deg=1.5)
    assert 9 <= len(coarse) <= 49  # a handful of rows/cols, not a huge raster
    assert 4 <= len(fine) <= 25


def test_build_coarse_then_fine_shifts_fine_stage_to_preliminary_peak():
    coarse, fine = build_coarse_then_fine(14.0, 3.5, 5.0, 1.5,
                                           coarse_peak_east_deg=2.0, coarse_peak_north_deg=-1.0)
    fine_center_east = (max(p.east_deg for p in fine) + min(p.east_deg for p in fine)) / 2
    fine_center_north = (max(p.north_deg for p in fine) + min(p.north_deg for p in fine)) / 2
    assert fine_center_east == pytest.approx(2.0, abs=1e-9)
    assert fine_center_north == pytest.approx(-1.0, abs=1e-9)
    coarse_center_east = (max(p.east_deg for p in coarse) + min(p.east_deg for p in coarse)) / 2
    assert coarse_center_east == pytest.approx(0.0, abs=1e-9)


def test_total_angular_span_matches_corner_distance():
    points = build_raster(span_deg=10.0, spacing_deg=5.0)
    span = total_angular_span_deg(points)
    assert span == pytest.approx((5.0 ** 2 + 5.0 ** 2) ** 0.5, abs=1e-6)


def test_total_angular_span_of_empty_list_is_zero():
    assert total_angular_span_deg([]) == 0.0
