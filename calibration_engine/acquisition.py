"""Calibration acquisition backends - same Protocol-based real/simulated
split as alignment_engine/hi/acquisition.py, never duplicating SDRCapture
or the simulator: RealCalibrationAcquisitionBackend wraps sdr_capture.SDRCapture
exactly as RealHIAcquisitionBackend does; SimulatedCalibrationAcquisitionBackend
wraps calibration_engine.simulation. RealCalibrationAcquisitionBackend tunes
the receiver explicitly (sdr_tuning.tune_explicitly) before it will capture -
see its own docstring for why the earlier read-only policy was retired.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

import numpy as np

from calibration_foundation import _plain  # noqa: F401 - re-exported for readers of raw captures


def read_capture_iq(path: str) -> "tuple[np.ndarray, Dict[str, Any]]":
    """Same contract as calibration_foundation._read_capture (reused, not
    duplicated): rejects .part files, requires capture_status=="success"."""
    from calibration_foundation import _read_capture
    return _read_capture(path)


class CalibrationAcquisitionBackend(Protocol):
    async def capture(self, *, duration_seconds: float, output_path: str,
                       center_frequency_hz: float, sample_rate_hz: float,
                       gain_db: Optional[float], metadata: Dict[str, Any]) -> str:
        ...


@dataclass
class RealCalibrationAcquisitionBackend:
    """Wraps sdr_capture.SDRCapture - never reimplements the rtl_tcp
    protocol. Constructing this does NOT connect (Fase 50/consistency with
    RealHIAcquisitionBackend's own contract) - connect() is explicit.

    TUNES EXPLICITLY BEFORE CAPTURING. It used to never call configure() so a
    capture stayed read-only at the rtl_tcp protocol level, and it recorded the
    service argv as the frequency. That was wrong: rtl_tcp keeps whatever the
    PREVIOUS client tuned, so the 2026-10-01 wizard 50 ohm captures ran at
    1420405000 Hz (service argv, nobody had retuned yet) while their HDF5 attrs
    declared 1420405752. Now tune() must succeed (rtl_tcp acknowledged exactly
    the requested frequency/rate/gain on this connection) and capture() refuses
    any other frequency/rate/gain than the one tuned. Each capture's attrs
    carry the requested and applied values and the evidence separately
    (sdr_tuning.tuning_attrs)."""
    host: str
    port: int
    _sdr: Any = None
    tuning: Optional[Dict[str, Any]] = None

    async def connect(self) -> None:
        from sdr_capture import SDRCapture
        self._sdr = SDRCapture(mode="network", host=self.host, port=self.port, verbose=False)
        await self._sdr.connect()

    async def tune(self, center_frequency_hz: float, sample_rate_hz: float, gain_db: float,
                   ack_source: Any = None) -> Dict[str, Any]:
        """Explicit tuning on this connection; raises sdr_tuning.TuningIncoherent (no capture) on failure."""
        import sdr_tuning
        if self._sdr is None:
            raise RuntimeError("connect() must be called before tune()")
        self.tuning = None
        self.tuning = await sdr_tuning.tune_explicitly(self._sdr, center_frequency_hz, sample_rate_hz, gain_db,
                                                       ack_source=ack_source, operating=sdr_tuning.operating_config())
        return self.tuning

    async def capture(self, *, duration_seconds: float, output_path: str, center_frequency_hz: float,
                       sample_rate_hz: float, gain_db: Optional[float], metadata: Dict[str, Any]) -> str:
        import sdr_tuning
        if self._sdr is None:
            raise RuntimeError("connect() must be called before capture()")
        if self.tuning is None:
            raise sdr_tuning.TuningIncoherent("capture() before a successful tune() - NO capture taken")
        req = self.tuning["requested"]
        if (int(round(center_frequency_hz)), int(round(sample_rate_hz)), gain_db) != \
                (req["center_frequency_hz"], req["sample_rate_hz"], req["gain_db"]):
            raise sdr_tuning.TuningIncoherent(
                f"capture asks for {center_frequency_hz} Hz / {sample_rate_hz} sps / {gain_db} dB but the receiver "
                f"was tuned to {req} - NO capture taken")
        attrs = {**metadata, **sdr_tuning.tuning_attrs(self.tuning), "gain_requested_db": gain_db}
        await self._sdr.capture(duration_seconds, output_path, int(sample_rate_hz), attrs)
        return output_path

    async def close(self) -> None:
        if self._sdr is not None:
            await self._sdr.close()


@dataclass
class SimulatedCalibrationAcquisitionBackend:
    """Never opens a socket. Writes a real HDF5 file (same iq_data/attrs
    shape a real capture would have) via calibration_engine.simulation so
    replay/analysis code paths are identical for real and simulated
    sessions."""
    simulation_config_factory: Any    # Callable[[int], InstrumentSimulationConfig] - one per capture index

    async def capture(self, *, duration_seconds: float, output_path: str, center_frequency_hz: float,
                       sample_rate_hz: float, gain_db: Optional[float], metadata: Dict[str, Any],
                       index: int = 0) -> str:
        import h5py
        from datetime import datetime, timezone
        from calibration_engine.simulation import simulate_capture_iq
        config = self.simulation_config_factory(index)
        iq = simulate_capture_iq(config)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with h5py.File(path, "w") as handle:
            handle.create_dataset("iq_data", data=iq, compression="gzip", compression_opts=4)
            handle.attrs.update(
                capture_status="success", simulated=True,
                center_frequency_hz=center_frequency_hz, sample_rate_hz=sample_rate_hz,
                gain_requested_db=(gain_db if gain_db is not None else "auto"),
                duration_seconds=duration_seconds, created_at=datetime.now(timezone.utc).isoformat(),
                **{k: v for k, v in metadata.items() if isinstance(v, (str, int, float, bool))},
            )
        return str(path)
