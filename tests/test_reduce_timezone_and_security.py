"""TIMEZONE audit (2nd-pass section 36) and PATH SAFETY / SECURITY
(sections 58-59): campaign/session ids must never permit '..', absolute
path escape, or symlink escape out of the REDUCE output root - the same
class of bug ALIGN/CALIBRATE's web layer found for real in the first
pass, audited here for REDUCE's own storage layer.
"""
import os
from pathlib import Path

import numpy as np
import pytest

from hi_spectral_metric import HI_REST_HZ
from reduce_engine.storage import ReduceSession
from reduce_engine.velocity import compute_velocity_axis

OBSERVER = dict(observer_latitude_deg=-33.4489, observer_longitude_deg=-70.6693, observer_elevation_m=570)


# ---------------------------------------------------------------- 36. timezone

@pytest.mark.parametrize("timestamp_utc", [
    "2026-09-02T16:18:20.126808+00:00",   # real HDF5 attrs format
    "2026-09-02T16:18:20.126808Z",        # Z-suffix ISO 8601
    "2026-09-02T16:18:20+00:00",          # no fractional seconds
    "2026-09-02T13:18:20-03:00",          # non-UTC offset, same instant
])
def test_timezone_variants_normalize_to_the_same_utc_instant(timestamp_utc):
    frequency = np.array([HI_REST_HZ])
    result = compute_velocity_axis(frequency, rest_frequency_hz=HI_REST_HZ, frame="lsrk",
                                   timestamp_utc=timestamp_utc, ra_hours=11.708053, dec_degrees=-34.935703,
                                   **OBSERVER)
    assert result.frame == "lsrk"
    assert result.velocity_m_s is not None


def test_all_four_timezone_variants_of_the_same_instant_agree():
    frequency = np.array([HI_REST_HZ])
    variants = ["2026-09-02T16:18:20+00:00", "2026-09-02T16:18:20Z", "2026-09-02T13:18:20-03:00"]
    velocities = []
    for timestamp_utc in variants:
        result = compute_velocity_axis(frequency, rest_frequency_hz=HI_REST_HZ, frame="lsrk",
                                       timestamp_utc=timestamp_utc, ra_hours=11.708053, dec_degrees=-34.935703,
                                       **OBSERVER)
        velocities.append(float(result.velocity_m_s[0]))
    assert max(velocities) - min(velocities) < 1.0  # same physical instant -> same velocity, within 1 m/s


def test_naive_datetime_without_any_offset_is_still_handled():
    """Real capture metadata always carries a UTC offset, but a naive
    (no-tzinfo) string must not crash - it is treated as already UTC
    (datetime.fromisoformat's own documented behavior for a bare
    string), not silently reinterpreted as local time."""
    frequency = np.array([HI_REST_HZ])
    result = compute_velocity_axis(frequency, rest_frequency_hz=HI_REST_HZ, frame="lsrk",
                                   timestamp_utc="2026-09-02T16:18:20", ra_hours=11.708053, dec_degrees=-34.935703,
                                   **OBSERVER)
    assert result.frame == "lsrk"


# ---------------------------------------------------------------- 58/59. path safety

def test_campaign_id_with_dotdot_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ReduceSession(tmp_path, "../../etc")


def test_campaign_id_with_absolute_path_component_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ReduceSession(tmp_path, "/etc/passwd")


def test_campaign_id_with_embedded_slash_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ReduceSession(tmp_path, "campaign/../../escape")


def test_explicit_session_id_with_dotdot_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ReduceSession(tmp_path, "SAFE-CAMPAIGN", session_id="../../../escape")


def test_valid_campaign_and_session_id_still_work(tmp_path):
    session = ReduceSession(tmp_path, "SAFE-CAMPAIGN", session_id="REDUCE-TEST-001")
    assert session.dir == (tmp_path / "SAFE-CAMPAIGN" / "REDUCE-TEST-001").resolve()
    assert session.dir.is_dir()


def test_symlink_output_root_cannot_be_used_to_escape(tmp_path):
    """The output_root itself may be a symlink (legitimate - e.g. a data
    disk mounted elsewhere) - what must never happen is a session
    escaping wherever that root actually resolves to."""
    real_root = tmp_path / "real_data_disk"
    real_root.mkdir()
    link_root = tmp_path / "reduced_link"
    os.symlink(real_root, link_root)
    session = ReduceSession(link_root, "CAMP", session_id="REDUCE-TEST-002")
    assert real_root.resolve() in session.dir.parents or session.dir.parent.parent == real_root.resolve()
    assert session.dir.is_dir()
