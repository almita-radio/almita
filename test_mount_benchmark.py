import asyncio
import csv
import math
from pathlib import Path

import astropy.units as u
import pytest
from astropy.coordinates import AltAz, CIRS, EarthLocation, ICRS, SkyCoord
from astropy.time import Time

import mount_benchmark as mb


LOCATION = EarthLocation(lat=-33.4489 * u.deg, lon=-70.6693 * u.deg, height=570 * u.m)
OBSTIME = Time("2026-08-20T01:00:00", scale="utc")


def plan(samples=100, seed=20260819, minimum=30.0):
    return mb.generate_plan(samples, minimum, seed, LOCATION, OBSTIME, "test-benchmark")


def test_plan_has_exact_count_valid_unique_targets():
    rows = plan()
    assert len(rows) == 100
    assert all(row["target_alt_deg"] >= 30.0 for row in rows)
    values = [(row["target_ra_icrs_hours"], row["target_dec_icrs_deg"]) for row in rows]
    assert len(values) == len(set(values))
    assert all(math.isfinite(value) for pair in values for value in pair)


def test_plan_has_reasonable_azimuth_quadrants_and_all_distance_bands():
    rows = plan()
    quadrants = [0, 0, 0, 0]
    for row in rows:
        quadrants[min(int(row["target_az_deg"] // 90), 3)] += 1
    assert all(count >= 10 for count in quadrants)
    assert {row["distance_band"] for row in rows[1:]} == {"<=10", ">10-30", ">30-60", ">60"}


def test_seed_is_reproducible():
    first = plan()
    second = plan()
    keys = ("target_point_id", "target_alt_deg", "target_az_deg", "target_ra_icrs_hours")
    assert [[row[key] for key in keys] for row in first] == [
        [row[key] for key in keys] for row in second
    ]


def test_icrs_to_eod_matches_astropy_conversion():
    row = plan(samples=3)[0]
    icrs = SkyCoord(
        ra=row["target_ra_icrs_hours"] * u.hourangle,
        dec=row["target_dec_icrs_deg"] * u.deg,
        frame=ICRS(),
    )
    expected = icrs.transform_to(CIRS(obstime=OBSTIME, location=LOCATION))
    assert expected.ra.hour == pytest.approx(row["target_ra_eod_hours"], abs=1e-10)
    assert expected.dec.deg == pytest.approx(row["target_dec_eod_deg"], abs=1e-10)
    local = icrs.transform_to(AltAz(obstime=OBSTIME, location=LOCATION))
    assert local.alt.deg == pytest.approx(row["target_alt_deg"], abs=1e-8)
    assert local.az.deg == pytest.approx(row["target_az_deg"], abs=1e-8)


class FakeController:
    def __init__(self, positions, goto_result=True):
        self.positions = iter(positions)
        self.goto_result = goto_result
        self.goto_calls = []
        self.last_slew_command_to_ok_sec = 1.25

    async def get_coordinates(self, force_refresh=False):
        return next(self.positions)

    async def goto(self, ra, dec):
        self.goto_calls.append((ra, dec))
        return self.goto_result


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        current = self.value
        self.value += 2.0
        return current


def test_execution_uses_real_position_and_external_duration():
    rows = plan(samples=1)
    target = rows[0]
    controller = FakeController([(5.0, -20.0), (target["target_ra_eod_hours"], target["target_dec_eod_deg"])])
    result = asyncio.run(mb.execute_plan(
        controller, rows, LOCATION, -90.0, 0.0,
        perf_counter=Clock(), time_now=lambda: OBSTIME,
    ))[0]
    assert result["start_ra_eod_hours"] == 5.0
    assert result["angular_distance_deg"] == pytest.approx(mb.angular_distance_deg(
        5.0, -20.0, result["target_ra_eod_hours"], result["target_dec_eod_deg"]
    ))
    assert result["goto_duration_external_sec"] == 2.0
    assert result["goto_duration_controller_sec"] == 1.25


def test_failed_goto_is_recorded_without_inventing_final_position():
    rows = plan(samples=1)
    controller = FakeController([(5.0, -20.0), (None, None)], goto_result=False)
    result = asyncio.run(mb.execute_plan(
        controller, rows, LOCATION, -90.0, 0.0,
        perf_counter=Clock(), time_now=lambda: OBSTIME,
    ))[0]
    assert result["success"] is False
    assert result["result"] == "failed"
    assert result["end_ra_eod_hours"] is None
    assert result["final_pointing_error_deg"] is None
    assert result["error_message"] == "controller.goto returned False"


def test_dry_run_writes_outputs_and_never_constructs_controller(tmp_path, monkeypatch):
    config = tmp_path / "observer.json"
    config.write_text(
        '{"observer":{"name":"Test","latitude_deg":-33.4489,'
        '"longitude_deg":-70.6693,"elevation_m":570}}', encoding="utf-8"
    )
    monkeypatch.setattr(mb, "_git_hash", lambda: "deadbeef")
    args = mb.build_parser().parse_args([
        "--dry-run", "--samples", "100", "--seed", "20260819",
        "--config", str(config), "--output-dir", str(tmp_path / "out"),
    ])
    assert asyncio.run(mb.async_main(args)) == 0
    output = tmp_path / "out"
    expected = {
        "mount_benchmark_plan.csv", "mount_benchmark.csv",
        "mount_benchmark_metadata.json", "mount_benchmark_summary.json",
        "mount_benchmark_plan.png", "mount_benchmark_distances.png",
    }
    assert {path.name for path in output.iterdir()} == expected
    with (output / "mount_benchmark_plan.csv").open() as handle:
        assert len(list(csv.DictReader(handle))) == 100
    with (output / "mount_benchmark.csv").open() as handle:
        assert list(csv.DictReader(handle)) == []


def test_load_plan_restores_numeric_fields(tmp_path):
    rows = plan(samples=2)
    path = tmp_path / "plan.csv"
    mb._write_csv(path, rows, mb.PLAN_FIELDS)
    restored = mb.load_plan(path)
    assert [row["sample_id"] for row in restored] == [1, 2]
    assert isinstance(restored[0]["target_ra_icrs_hours"], float)
    assert restored == rows
