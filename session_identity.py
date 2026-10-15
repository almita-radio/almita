"""One identity for an OBSERVE session (BL-050).

A run leaves three different identifiers, all still written as before (nothing in the acquisition changed):
  campaign_id       data/mosaic/<campaign_id>/ - the plan (grid_metadata.json session_id is its timestamp part)
  iq_session_dir    data/mosaic/<campaign_id>/data/iq/<iq_session_dir>/ - created when the first capture starts
  capture_session   capture.py's session_id (capture_timing_<id>.csv, HDF5 attr session_id, current/last_session.json)

The canonical identity is the campaign_id: it names the directory that holds everything else. resolve() finds the
campaign from any of the three and returns all of them."""
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
MOSAIC = ROOT / "data" / "mosaic"
_TIMING = re.compile(r"^capture_timing_(\d{8}_\d{6})\.csv$")


def from_session_root(session_root: Optional[str]) -> Dict[str, Optional[str]]:
    """campaign_id / iq_session_dir from capture.py's announced session_root (.../mosaic/<campaign>/data/iq/<dir>)."""
    parts = Path(session_root).parts if session_root else ()
    if len(parts) >= 4 and parts[-2] == "iq" and parts[-3] == "data":
        return {"campaign_id": parts[-4], "iq_session_dir": parts[-1]}
    return {"campaign_id": None, "iq_session_dir": None}


def describe(campaign_dir: Path) -> Dict[str, Any]:
    """All identifiers recorded inside one campaign directory."""
    iq = campaign_dir / "data" / "iq"
    caps: List[str] = sorted(m.group(1) for p in campaign_dir.glob("capture_timing_*.csv") if (m := _TIMING.match(p.name)))
    return {"campaign_id": campaign_dir.name, "campaign_dir": str(campaign_dir.relative_to(ROOT)) if campaign_dir.is_relative_to(ROOT) else str(campaign_dir),
            "iq_session_dirs": sorted(p.name for p in iq.iterdir() if p.is_dir()) if iq.is_dir() else [],
            "capture_session_ids": caps}


def resolve(identifier: str, mosaic: Path = MOSAIC) -> Optional[Dict[str, Any]]:
    """The campaign (and its other identifiers) that any of the three identifiers belongs to; None if unknown."""
    if not identifier or "/" in identifier or "\x00" in identifier:
        return None
    direct = mosaic / identifier
    if direct.is_dir():
        return describe(direct)
    for camp in sorted(mosaic.iterdir()) if mosaic.is_dir() else []:
        if not camp.is_dir():
            continue
        if (camp / f"capture_timing_{identifier}.csv").is_file() or (camp / "data" / "iq" / identifier).is_dir():
            return describe(camp)
    return None
