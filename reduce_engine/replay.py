"""REPLAY: reprocess a previous REDUCE session's raw source offline, from
the RAW campaign it points to - never from the session's own derived
output. Always produces a brand-new REDUCE_SESSION_ID; a replay never
overwrites the session it replays. 100% offline: no hardware, no INDI, no
rtl_tcp, no network - it only re-runs the same ingest->...->persist
pipeline `run` already uses.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Optional

from reduce_engine.config import ReduceConfig
from reduce_engine.ingest import discover_campaign
from reduce_engine.pipeline import CampaignReduceReport, reduce_campaign


def replay_session(session_dir: str | Path, *, output_root: str,
                   calibration_profile_path: Optional[str] = None,
                   config_overrides: Optional[dict] = None) -> CampaignReduceReport:
    session_dir = Path(session_dir)
    manifest_path = session_dir / "manifest.json"
    config_path = session_dir / "config.json"
    if not manifest_path.exists() or not config_path.exists():
        raise FileNotFoundError(f"not a REDUCE session directory (missing manifest.json/config.json): {session_dir}")
    old_manifest = json.loads(manifest_path.read_text())
    old_config_dict = json.loads(config_path.read_text())
    source_root = old_manifest["source_campaign_root"]
    if not Path(source_root).is_dir():
        raise FileNotFoundError(f"replay source campaign no longer exists (RAW must never move): {source_root}")

    config = ReduceConfig(**old_config_dict)
    if config_overrides:
        config = replace(config, **config_overrides)

    campaign_manifest = discover_campaign(source_root)
    return reduce_campaign(campaign_manifest, config, output_root=output_root,
                           calibration_profile_path=calibration_profile_path)
