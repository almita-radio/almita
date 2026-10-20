"""temperature_logger (2026-10-17): every configured DS18B20 role is logged each minute and STATUS says when each one
settled (moved <= 0.2 C in 10 min). Fake 1-Wire sysfs and log directory in tmp_path; no hardware."""
import json
from datetime import datetime, timedelta, timezone

import temperature_logger as tl

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def _w1(root, sensor_id, celsius):
    d = root / sensor_id; d.mkdir(parents=True, exist_ok=True)
    (d / "w1_slave").write_text(f"aa 01 4b 46 7f ff 0c 10 1c : crc=1c YES\naa 01 4b 46 7f ff 0c 10 1c t={int(celsius * 1000)}\n")


def test_every_configured_role_is_sampled_and_a_missing_sensor_is_logged_as_missing(tmp_path):
    from temperature_sensors import DS18B20Reader
    sysfs = tmp_path / "w1"
    _w1(sysfs, "28-aaa", 31.5); _w1(sysfs, "28-bbb", 40.25)
    sensors = {"sdr": "28-aaa", "lna": "28-bbb", "load": "28-missing"}
    rec = tl.sample(sensors, reader_factory=lambda s, warning: DS18B20Reader(s, sysfs_root=sysfs, warning=warning))
    assert rec["readings"]["sdr"]["c"] == 31.5 and rec["readings"]["lna"]["c"] == 40.25
    assert rec["readings"]["load"]["c"] is None and "error" in rec["readings"]["load"]       # never invented


def test_config_roles_are_read_fresh_and_any_role_is_accepted(tmp_path):
    cfg = tmp_path / "observer_config.json"
    cfg.write_text(json.dumps({"temperature_sensors": {"SDR": "28-a", "lna": "28-b", "cable": "28-c", "load": ""}}))
    assert tl.configured_sensors(cfg) == {"sdr": "28-a", "lna": "28-b", "cable": "28-c"}
    cfg.write_text("not json")
    assert tl.configured_sensors(cfg) == {}


def _log(tmp_path, values, start=T0, step_s=60, role="lna"):
    for i, v in enumerate(values):
        tl.append({"utc": (start + timedelta(seconds=i * step_s)).isoformat(timespec="seconds"),
                   "readings": {role: {"c": v, "id": "28-x"}}}, tmp_path)


def test_warming_then_stable_reports_since_when(tmp_path):
    vals = [20 + i * 0.5 for i in range(30)] + [35.0 + 0.01 * (i % 3) for i in range(30)]    # 30 min heating, 30 min flat
    _log(tmp_path, vals)
    now = T0 + timedelta(seconds=59 * 60 + 5)
    s = tl.summary(2, tmp_path, now, config_path=tmp_path / "none.json")["status"]["lna"]
    assert s["state"] == "STABLE" and abs(s["change_last_window_c"]) <= 0.2
    since = datetime.fromisoformat(s["stable_since"])
    assert T0 + timedelta(minutes=29) <= since <= T0 + timedelta(minutes=31)                  # where the plateau began


def test_changing_stale_no_data_and_too_little_history(tmp_path):
    _log(tmp_path, [20 + i * 0.1 for i in range(30)])                                       # +1.0 C per 10 min
    end = T0 + timedelta(minutes=29, seconds=5)
    assert tl.summary(1, tmp_path, end, config_path=tmp_path / "none.json")["status"]["lna"]["state"] == "CHANGING"
    later = end + timedelta(minutes=10)
    assert tl.summary(1, tmp_path, later, config_path=tmp_path / "none.json")["status"]["lna"]["state"] == "STALE"
    fresh = tmp_path / "fresh"; _log(fresh, [30.0] * 5)
    assert tl.summary(1, fresh, T0 + timedelta(minutes=4, seconds=5), config_path=tmp_path / "none.json")["status"]["lna"]["state"] == "WARMING_UP_LOG"
    cfg = tmp_path / "c.json"; cfg.write_text(json.dumps({"temperature_sensors": {"load": "28-z"}}))
    assert tl.summary(1, tmp_path / "empty", T0, config_path=cfg)["status"]["load"]["state"] == "NO_DATA"


def test_logs_older_than_the_retention_are_pruned(tmp_path):
    (tmp_path / "20260801.jsonl").write_text("{}\n"); (tmp_path / "20261016.jsonl").write_text("{}\n")
    tl.prune(tmp_path, keep_days=60, now=T0)
    assert sorted(f.name for f in tmp_path.iterdir()) == ["20261016.jsonl"]


def test_85_c_power_on_reset_is_no_reading_in_new_samples_and_in_the_history(tmp_path):
    from temperature_sensors import DS18B20Reader
    sysfs = tmp_path / "w1"
    _w1(sysfs, "28-aaa", 85.0); _w1(sysfs, "28-bbb", 34.375)
    rec = tl.sample({"sdr": "28-aaa", "lna": "28-bbb"},
                    reader_factory=lambda s, warning: DS18B20Reader(s, sysfs_root=sysfs, warning=warning))
    assert rec["readings"]["sdr"]["c"] is None and "power-on reset" in rec["readings"]["sdr"]["error"]
    assert rec["readings"]["lna"]["c"] == 34.375
    logs = tmp_path / "logs"
    _log(logs, [85.0, 30.0, 30.1], role="sdr")
    rows = tl.load(1.0, logs, now=T0 + timedelta(minutes=5))
    assert [r["readings"]["sdr"]["c"] for r in rows] == [None, 30.0, 30.1]


def test_a_line_padded_with_nul_bytes_after_a_power_cut_is_still_read(tmp_path):
    _log(tmp_path, [20.0])
    path = next(tmp_path.glob("*.jsonl"))
    rec = {"utc": (T0 + timedelta(minutes=1)).isoformat(timespec="seconds"), "readings": {"lna": {"c": 21.0, "id": "28-x"}}}
    with path.open("a") as fh:
        fh.write("\x00" * 40 + json.dumps(rec) + "\n")
    rows = tl.load(1.0, tmp_path, now=T0 + timedelta(minutes=5))
    assert [r["readings"]["lna"]["c"] for r in rows] == [20.0, 21.0]
