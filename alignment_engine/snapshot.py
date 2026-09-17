"""JSON-serializable progress snapshot (Fase 13).

Not an API - just the shape a future web UI at :8090 would poll. Every
engine method that changes state updates one of these and the CLI/future
API both read the same object, so nothing needs reimplementing later.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AlignmentSnapshot:
    session_id: str
    mode: str  # "SOLAR" | "HI"
    state: str
    point_index: int = 0
    point_total: int = 0
    target: Optional[Dict[str, Any]] = None
    current_mount: Optional[Dict[str, Any]] = None
    tracking_mode: Optional[str] = None
    latest_metric: Optional[float] = None
    elapsed_s: float = 0.0
    eta_s: Optional[float] = None
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
