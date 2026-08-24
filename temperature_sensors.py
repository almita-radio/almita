"""Small, non-fatal DS18B20 sysfs reader for capture telemetry."""

from datetime import datetime, timezone
import logging
import math
from pathlib import Path
from typing import Callable, Dict, Optional


class DS18B20Reader:
    """Read explicitly assigned DS18B20 sensors directly from Linux sysfs."""

    def __init__(self, sensors: Dict[str, str], sysfs_root: Path = Path("/sys/bus/w1/devices"),
                 warning: Optional[Callable[[str], None]] = None):
        self.sensors = {str(role).lower(): str(sensor_id) for role, sensor_id in sensors.items()}
        self.sysfs_root = Path(sysfs_root)
        self.warning = warning or (lambda message: logging.warning(message))
        self._consecutive_85 = {role: 0 for role in self.sensors}

    def read(self, role: str) -> Dict:
        role = role.lower()
        sensor_id = self.sensors[role]
        timestamp = datetime.now(timezone.utc).isoformat()
        result = {
            "sensor_id": sensor_id, "temperature_c": None,
            "valid": False, "timestamp_utc": timestamp,
        }
        try:
            lines = (self.sysfs_root / sensor_id / "w1_slave").read_text().splitlines()
            if not lines or not lines[0].rstrip().endswith("YES"):
                raise ValueError("CRC check failed")
            if len(lines) < 2 or "t=" not in lines[1]:
                raise ValueError("temperature field t= is missing")
            raw = lines[1].rsplit("t=", 1)[1].strip()
            temperature = int(raw) / 1000.0
            if temperature == -127.0:
                raise ValueError("invalid DS18B20 sentinel -127.0 C")
            result.update(temperature_c=temperature, valid=True)
            if temperature == 85.0:
                self._consecutive_85[role] = self._consecutive_85.get(role, 0) + 1
                if self._consecutive_85[role] >= 2:
                    self.warning(
                        f"Temperature sensor {role.upper()} ({sensor_id}) repeatedly reports 85.0 C"
                    )
            else:
                self._consecutive_85[role] = 0
        except (OSError, ValueError, IndexError) as exc:
            self._consecutive_85[role] = 0
            self.warning(f"Temperature sensor {role.upper()} ({sensor_id}) invalid: {exc}")
        return result

    def read_all(self) -> Dict[str, Dict]:
        return {role: self.read(role) for role in ("sdr", "lna") if role in self.sensors}


def temperature_metadata(pre: Dict[str, Dict], post: Dict[str, Dict]) -> Dict:
    """Flatten pre/post readings into HDF5-compatible scalar attributes."""
    metadata = {
        "temperature_pre_timestamp_utc": _set_timestamp(pre),
        "temperature_post_timestamp_utc": _set_timestamp(post),
    }
    for role in ("sdr", "lna"):
        before = pre.get(role, {})
        after = post.get(role, {})
        valid_values = [
            item.get("temperature_c") for item in (before, after)
            if item.get("valid") and item.get("temperature_c") is not None
        ]
        metadata.update({
            f"temperature_{role}_pre_c": before.get("temperature_c") if before.get("valid") else math.nan,
            f"temperature_{role}_post_c": after.get("temperature_c") if after.get("valid") else math.nan,
            f"temperature_{role}_mean_c": sum(valid_values) / len(valid_values) if valid_values else math.nan,
            f"temperature_{role}_valid_count": len(valid_values),
            f"temperature_{role}_sensor_id": before.get("sensor_id") or after.get("sensor_id"),
            f"temperature_{role}_valid_pre": bool(before.get("valid")),
            f"temperature_{role}_valid_post": bool(after.get("valid")),
        })
    return metadata


def _set_timestamp(readings: Dict[str, Dict]) -> Optional[str]:
    timestamps = [item.get("timestamp_utc") for item in readings.values() if item.get("timestamp_utc")]
    return max(timestamps) if timestamps else None


def format_temperatures(readings: Dict[str, Dict]) -> str:
    def value(role: str) -> str:
        item = readings.get(role, {})
        return f"{item['temperature_c']:.1f}°C" if item.get("valid") else "ERR"
    return f"TEMP     SDR={value('sdr')}   LNA={value('lna')}"
