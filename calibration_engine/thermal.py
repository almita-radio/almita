"""Thermal integration (Fase 17-19) and per-sensor calibration architecture
(Fase 53) - kept SEPARATE from RF calibration per the user's explicit
instruction ("DS18B20 sí puede calibrarse relativamente... pero NO
mezclarlo en RF calibration"). Reuses temperature_sensors.DS18B20Reader
as-is; this module only adds correlation analysis and a not-yet-applied
per-ROM offset/slope model on top of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from temperature_sensors import DS18B20Reader, is_plausible_temperature_c  # noqa: F401 - re-exported for callers


@dataclass
class SensorCalibration:
    """Fase 53: architecture only - offset/slope are None (not applied)
    until an independent thermal reference measurement exists. raw is
    always reported; corrected is None until then."""
    rom_id: str
    role: str
    offset_c: Optional[float] = None
    slope: Optional[float] = None
    calibrated: bool = False

    def apply(self, raw_c: Optional[float]) -> Optional[float]:
        if raw_c is None or not self.calibrated or self.offset_c is None or self.slope is None:
            return None
        return self.slope * raw_c + self.offset_c

    def to_dict(self) -> Dict[str, Any]:
        return {"rom_id": self.rom_id, "role": self.role, "offset_c": self.offset_c,
                "slope": self.slope, "calibrated": self.calibrated}


def reading_record(role: str, reading: Dict[str, Any], sensor_cal: Optional[SensorCalibration] = None) -> Dict[str, Any]:
    """Fase 17: temperature_raw / temperature_corrected (None unless a real
    SensorCalibration exists) / sensor_id / role. Invalid reading -> None,
    never a fabricated last-known value (Fase 17's explicit "No fake
    last-known values. Invalid: N/A")."""
    raw = reading.get("temperature_c") if reading.get("valid") else None
    corrected = sensor_cal.apply(raw) if (sensor_cal and raw is not None) else None
    return {
        "role": role, "sensor_id": reading.get("sensor_id"),
        "temperature_raw_c": raw, "temperature_corrected_c": corrected,
        "valid": bool(reading.get("valid")), "timestamp_utc": reading.get("timestamp_utc"),
    }


@dataclass
class CorrelationResult:
    metric_name: str
    pearson_r: Optional[float]
    n_samples: int
    temperature_range_c: Optional[List[float]]
    metric_range: Optional[List[float]]
    confidence_note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_name": self.metric_name, "pearson_r": self.pearson_r, "n_samples": self.n_samples,
            "temperature_range_c": self.temperature_range_c, "metric_range": self.metric_range,
            "confidence_note": self.confidence_note,
        }


# Fase 18: "NO concluir causalidad con pocos datos" - this is a sample-count
# floor for even ATTEMPTING a correlation number, not a claim of
# significance above it. Chosen as the smallest N where a Pearson r has
# any conventional meaning at all (below this, report NONE rather than a
# number that invites over-interpretation).
MIN_SAMPLES_FOR_CORRELATION = 5


def correlate_with_temperature(temperatures_c: Sequence[Optional[float]], metric_values: Sequence[Optional[float]],
                                metric_name: str) -> CorrelationResult:
    pairs = [(t, m) for t, m in zip(temperatures_c, metric_values) if t is not None and m is not None]
    if len(pairs) < MIN_SAMPLES_FOR_CORRELATION:
        return CorrelationResult(metric_name, None, len(pairs), None, None,
                                  f"fewer than {MIN_SAMPLES_FOR_CORRELATION} valid (temperature, metric) pairs - "
                                  f"no correlation attempted, not even a weak one")
    temps = np.asarray([p[0] for p in pairs], dtype=float)
    metrics = np.asarray([p[1] for p in pairs], dtype=float)
    if np.std(temps) < 1e-9 or np.std(metrics) < 1e-9:
        return CorrelationResult(metric_name, None, len(pairs), [float(temps.min()), float(temps.max())],
                                  [float(metrics.min()), float(metrics.max())],
                                  "temperature or metric has ~zero variance over this sample - correlation undefined")
    r = float(np.corrcoef(temps, metrics)[0, 1])
    note = (f"n={len(pairs)} pairs, temperature range "
            f"{temps.max() - temps.min():.2f}C - correlation only, NO causal claim; "
            f"treat as a candidate worth more data, not a conclusion")
    return CorrelationResult(metric_name, r, len(pairs), [float(temps.min()), float(temps.max())],
                              [float(metrics.min()), float(metrics.max())], note)
