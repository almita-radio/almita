"""DS18B20Reader retries a failed conversion once (BL-069): sporadic 1-Wire noise no longer costs a capture its
temperature; a sensor that keeps failing is still reported invalid."""
from pathlib import Path

from temperature_sensors import DS18B20Reader

GOOD = "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=35812\n"
BAD = "72 01 4b 46 7f ff 0e 10 57 : crc=57 NO\n72 01 4b 46 7f ff 0e 10 57 t=35812\n"


class _Flaky(DS18B20Reader):
    def __init__(self, root, answers, **kw):
        super().__init__({"sdr": "28-x"}, root, warning=self.warn, **kw)
        self.answers, self.warned = list(answers), []

    def warn(self, msg):
        self.warned.append(msg)

    def _read_once(self, role):
        (self.sysfs_root / "28-x" / "w1_slave").write_text(self.answers.pop(0))
        return super()._read_once(role)


def _root(tmp_path):
    (tmp_path / "28-x").mkdir()
    return tmp_path


def test_one_crc_failure_is_retried_and_recorded(tmp_path):
    r = _Flaky(_root(tmp_path), [BAD, GOOD])
    out = r.read("sdr")
    assert out["valid"] and out["temperature_c"] == 35.812 and out["attempts"] == 2
    assert out["failed_attempts"] == ["CRC check failed"] and r.warned == []


def test_a_sensor_that_keeps_failing_is_invalid_and_warned(tmp_path):
    r = _Flaky(_root(tmp_path), [BAD, BAD])
    out = r.read("sdr")
    assert not out["valid"] and out["attempts"] == 2 and len(r.warned) == 1


def test_a_missing_sensor_is_not_retried(tmp_path):
    r = DS18B20Reader({"sdr": "28-gone"}, tmp_path, warning=lambda m: None)
    out = r.read("sdr")
    assert not out["valid"] and out["attempts"] == 1 and out["error_kind"] == "missing"
