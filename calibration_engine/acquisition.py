"""Calibration acquisition backends - same Protocol-based real/simulated
split as alignment_engine/hi/acquisition.py, never duplicating SDRCapture
or the simulator: RealCalibrationAcquisitionBackend wraps sdr_capture.SDRCapture
exactly as RealHIAcquisitionBackend does; SimulatedCalibrationAcquisitionBackend
wraps calibration_engine.simulation. NOT exercised with a real backend in
this pass (Fase 50: hardware policy) - present so the architecture exists
and is unit-testable, never invoked against real hardware here.
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
    RealHIAcquisitionBackend's own contract) - connect() is explicit."""
    host: str
    port: int
    _sdr: Any = None

    async def connect(self) -> None:
        from sdr_capture import SDRCapture
        self._sdr = SDRCapture(mode="network", host=self.host, port=self.port, verbose=False)
        await self._sdr.connect()

    async def capture(self, *, duration_seconds: float, output_path: str, center_frequency_hz: float,
                       sample_rate_hz: float, gain_db: Optional[float], metadata: Dict[str, Any]) -> str:
        if self._sdr is None:
            raise RuntimeError("connect() must be called before capture()")
        await self._sdr.configure(int(center_frequency_hz), int(sample_rate_hz),
                                   gain=("auto" if gain_db is None else gain_db))
        await self._sdr.capture(duration_seconds, output_path, int(sample_rate_hz), metadata)
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
