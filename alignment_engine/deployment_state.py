"""Physical deployment-state interlock.

This is a SEPARATE layer from every software-authorization gate already in
this package (preflight, reference trust, Quality V2, sync_policy). None
of those can know whether the mount is actually sitting somewhere it is
safe and meaningful to move - that is a physical fact about the world,
and the only way to learn it honestly is an explicit, timestamped
operator action. This module never infers FIELD from time of day, GPS,
mount reachability, observer coordinates, internet access, or history -
see check_hardware_movement_allowed()'s docstring for the exact,
deliberately short list of things that count.

Real, concrete incident this module exists because of: an authorized,
fully-tested, rehearsed real HI night scan was about to run for real
against a mount that turned out to be sitting on a table inside an
apartment, not pointed at open sky - caught only because the operator
happened to mention it out loud at the last moment, not because any
software gate asked the question. See the project memory entry this
module's design was requested to make unnecessary to rely on.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from runtime_state import atomic_write_json, read_json_safe

DEFAULT_STATE_PATH = "data/deployment_state.json"


class DeploymentState(str, Enum):
    UNKNOWN = "UNKNOWN"
    BENCH = "BENCH"
    INDOOR = "INDOOR"
    FIELD = "FIELD"


# The ONLY state that permits mount movement. Every other value - including
# ones that don't exist yet, should this enum ever grow - fails closed.
MOVEMENT_ALLOWED_STATES = frozenset({DeploymentState.FIELD})


@dataclass
class DeploymentRecord:
    state: DeploymentState
    timestamp_utc: str
    operator_action: str        # e.g. "set-field", "set-indoor", "set-bench", "set-unknown"
    hostname: str
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {"state": self.state.value, "timestamp_utc": self.timestamp_utc,
                "operator_action": self.operator_action, "hostname": self.hostname, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict) -> "DeploymentRecord":
        return cls(state=DeploymentState(data["state"]), timestamp_utc=data["timestamp_utc"],
                    operator_action=data["operator_action"], hostname=data["hostname"], reason=data.get("reason"))


def write_deployment_state(state: DeploymentState, operator_action: str,
                            reason: Optional[str] = None, path: str = DEFAULT_STATE_PATH) -> DeploymentRecord:
    """The ONLY way this file is ever written - always a full, explicit,
    timestamped record, never a bare state value. No caller can silently
    upgrade a state without going through here (and every call here comes
    from an explicit CLI `deployment set-*` invocation - see almita_align.py)."""
    record = DeploymentRecord(state=state, timestamp_utc=datetime.now(timezone.utc).isoformat(),
                               operator_action=operator_action, hostname=socket.gethostname(), reason=reason)
    atomic_write_json(Path(path), record.to_dict())
    return record


def read_current_deployment_state(path: str = DEFAULT_STATE_PATH) -> Optional[DeploymentRecord]:
    """Fail-closed by construction: a missing file, corrupt JSON, or an
    unrecognized state string all return None here - callers must treat
    None exactly like DeploymentState.UNKNOWN (see
    check_hardware_movement_allowed()), never assume FIELD as a default."""
    data = read_json_safe(Path(path))
    if data is None:
        return None
    try:
        return DeploymentRecord.from_dict(data)
    except (KeyError, ValueError):
        return None


@dataclass
class MovementGateResult:
    allowed: bool
    reason: str
    observed_state: str  # the DeploymentState value, or "MISSING"/"CORRUPT"

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "observed_state": self.observed_state}


def check_hardware_movement_allowed(purpose: str, record: Optional[DeploymentRecord],
                                     state_path: str = DEFAULT_STATE_PATH) -> MovementGateResult:
    """`purpose` is a short label for what's being gated (e.g. "real GOTO",
    "real SYNC") - used only in the reason string, not in the decision:
    the decision is exactly `record is not None and record.state == FIELD`.
    Nothing else - not time, not GPS, not mount reachability, not observer
    coordinates, not internet, not history, not "the antenna exists" -
    counts as evidence of field deployment. Read-only inspection and pure
    simulation never call this at all (see hw_hi_night_scan.py: this gate
    sits specifically in front of the code path that would command real
    movement, not in front of every real-mount interaction).

    `state_path` is used ONLY to name the checked file in the message
    below when `record` is None - it plays no role in the decision itself
    (a caller checking a non-default path must still get an honest
    message naming THAT path, not silently hardcoded to
    DEFAULT_STATE_PATH - found and fixed while first exercising this
    function with a non-default path)."""
    if record is None:
        return MovementGateResult(False, f"{purpose} BLOCKED: no deployment state recorded "
                                          f"(missing or corrupt {state_path}) - failing closed, "
                                          f"never assuming FIELD", "MISSING")
    if record.state not in MOVEMENT_ALLOWED_STATES:
        return MovementGateResult(False, f"{purpose} BLOCKED: ALMITA is not confirmed deployed in field "
                                          f"(deployment_state={record.state.value}, set {record.timestamp_utc} "
                                          f"by operator action '{record.operator_action}')", record.state.value)
    return MovementGateResult(True, f"{purpose} allowed: deployment_state=FIELD "
                                     f"(confirmed {record.timestamp_utc})", record.state.value)
