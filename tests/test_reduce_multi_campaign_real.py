"""Second-pass acceptance criterion #1/#3/#... : REDUCE must process more
than one real campaign, never modify RAW, and report PARTIAL (not a
falsely-COMPLETED) status when a real campaign has missing points.

Three real, finished, non-active campaigns were selected by auditing
data/mosaic (never the currently-RUNNING ALMITA-OBSERVE-20260917-20:10:26
- confirmed running via `ps` against quicklook_live.py/rtl_tcp during this
pass):

- SMALL  ("A"): ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16 - 9/9 success,
  already validated in the first pass. Kept as the known-good baseline.
- MEDIUM ("B"): ALMITA-OBSERVE-20260914-22:05:06 - 100/100 success, a
  clean finished campaign at a size that actually exercises per-campaign
  overhead amortization (see the performance section of the delivery
  report).
- MESSY  ("C"): ALMITA-OBSERVE-20260902-16:56:58 - 100 planned, only 50
  ever captured (capture_status="planned" for the other 50, not
  "failed" - the campaign was stopped partway through, a real historical
  fact, not injected for this test). This is deliberately NOT one of the
  "campaigns known to pass cleanly" - it is the "less pretty" real
  fixture the spec asked for.

Each campaign is reduced exactly ONCE in this file (via the parametrized
test) - cheap discovery-only checks reuse discover_campaign() again where
that's enough, never a second full reduce_campaign() run of the same data.
"""
import hashlib
import json
from pathlib import Path

import pytest

from reduce_engine.config import ReduceConfig
from reduce_engine.ingest import discover_campaign
from reduce_engine.pipeline import reduce_campaign

SMALL = "data/mosaic/ALMITA-WEB-SMALL-RUN-01-20260902-16:16:16"
MEDIUM = "data/mosaic/ALMITA-OBSERVE-20260914-22:05:06"
MESSY = "data/mosaic/ALMITA-OBSERVE-20260902-16:56:58"
CALIBRATION_PROFILE = "data/calibration/CALIBRATION-FOUNDATION-V1-20260827T005049Z/calibration_profile_v1"


def _skip_if_absent(path):
    if not Path(path).is_dir():
        pytest.skip(f"real fixture campaign not present: {path}")


def _hash_tree(root: Path) -> dict[str, str]:
    """SHA-256 of every real file under a campaign directory (excluding
    REDUCE's own output, which never lives inside the campaign dir
    anyway) - used to prove RAW is byte-identical before and after."""
    digests = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


@pytest.mark.parametrize("campaign_dir,expected_status,expected_completed,expected_discovered", [
    (SMALL, "COMPLETED", 9, 9),
    (MEDIUM, "COMPLETED", 100, 100),
    (MESSY, "PARTIAL", 50, 100),
])
def test_real_campaign_matrix(campaign_dir, expected_status, expected_completed, expected_discovered, tmp_path):
    _skip_if_absent(campaign_dir)
    root = Path(campaign_dir)
    before = _hash_tree(root)

    manifest = discover_campaign(campaign_dir)
    config = ReduceConfig()
    report = reduce_campaign(manifest, config, output_root=str(tmp_path / "reduced"),
                             calibration_profile_path=CALIBRATION_PROFILE)

    after = _hash_tree(root)
    assert before == after, "RAW campaign directory changed after REDUCE - this must never happen"

    assert report.status == expected_status
    assert report.points_completed == expected_completed
    assert report.points_discovered == expected_discovered
    if expected_status == "COMPLETED":
        assert report.points_blocked == 0 and report.points_failed == 0
        assert report.quality_counts.get("GOOD", 0) == expected_completed
        assert report.calibration_level_counts.get("RELATIVE", 0) == expected_completed

    manifest_json = json.loads((Path(report.output_dir) / "manifest.json").read_text())
    assert len(manifest_json["points"]) == expected_discovered  # every planned point listed, none silently dropped
    statuses = {p["status"] for p in manifest_json["points"]}
    assert statuses <= {"COMPLETED", "BLOCKED", "FAILED"}
    if expected_status == "PARTIAL":
        assert "BLOCKED" in statuses


def test_messy_campaign_discovery_shows_explicit_reject_reasons():
    """Cheap (discovery-only, no full reduce run) check of WHY the 50
    missing points were rejected - never a bare "not accepted"."""
    _skip_if_absent(MESSY)
    manifest = discover_campaign(MESSY)
    accepted = manifest.accepted_points()
    rejected = [p for p in manifest.points if not p.accepted]
    assert len(accepted) == 50
    assert len(rejected) == 50
    assert all("planned" in (p.reject_reason or "") for p in rejected)
