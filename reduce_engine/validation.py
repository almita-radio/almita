"""VALIDATE / PREFLIGHT: offline-only gates. Never touches hardware, INDI,
rtl_tcp, or the network - every check here is a filesystem/metadata read.
`plan`/`preflight` write nothing; only `run` does.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from reduce_engine.config import ReduceConfig
from reduce_engine.ingest import CampaignManifest


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def run_preflight(manifest: CampaignManifest, config: ReduceConfig, *, output_root: str | Path,
                  calibration_profile_path: Optional[str]) -> list[Check]:
    checks: list[Check] = []

    accepted = manifest.accepted_points()
    checks.append(Check("campaign_readable", True, f"{manifest.campaign_id}: {len(manifest.points)} points discovered"))
    checks.append(Check("has_accepted_points", len(accepted) > 0,
                        f"{len(accepted)}/{len(manifest.points)} points accepted"))

    if calibration_profile_path is not None:
        path = Path(calibration_profile_path)
        ok = path.with_suffix(".json").exists() and path.with_suffix(".npz").exists()
        checks.append(Check("calibration_profile_readable", ok,
                            str(path) if ok else f"missing .json/.npz pair at {path}"))
    else:
        checks.append(Check("calibration_profile_readable", True, "no profile requested - will run UNCALIBRATED"))

    checks.append(Check("fft_size_valid", config.fft_size >= 256 and (config.fft_size & (config.fft_size - 1) == 0),
                        f"fft_size={config.fft_size}"))

    if config.velocity_frame != "topocentric":
        has_observer = bool(manifest.observer.get("observer", {}))
        checks.append(Check("velocity_frame_requirements", has_observer,
                            "observer_config.json present with observer block" if has_observer
                            else f"velocity_frame={config.velocity_frame} requested but no observer location metadata found"))

    output_root = Path(output_root)
    try:
        output_root.mkdir(parents=True, exist_ok=True)
        writable = True
        detail = str(output_root)
    except OSError as error:
        writable, detail = False, str(error)
    checks.append(Check("output_writable", writable, detail))

    return checks


def blocking_reason(checks: list[Check]) -> Optional[str]:
    failed = [c for c in checks if not c.ok]
    if not failed:
        return None
    return "; ".join(f"{c.name}: {c.detail}" for c in failed)
