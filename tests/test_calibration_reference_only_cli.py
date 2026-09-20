import json
import sys

from build_calibration_foundation_v1 import parse_args, run
from test_calibration_foundation import make_capture


def test_reference_only_cli_builds_profile_and_defers_antenna_validation(tmp_path, monkeypatch):
    first = make_capture(tmp_path / "reference_a.h5", seed=1)
    second = make_capture(tmp_path / "reference_b.h5", seed=2)
    output = tmp_path / "profile"
    monkeypatch.setattr(sys, "argv", [
        "build_calibration_foundation_v1.py", "--output-dir", str(output),
        "--reference", str(first), str(second),
    ])
    args = parse_args()
    assert args.antenna == []
    run(args)
    validation = json.loads((output / "calibration_indoor_validation.json").read_text())
    assert validation["antenna_validation_status"] == "DEFERRED_NO_ANTENNA_CAPTURE"
    assert validation["captures"] == []
    assert (output / "calibration_profile_v1.json").is_file()
    assert (output / "calibration_profile_v1.npz").is_file()
    assert not (output / "calibration_validation_indoor.png").exists()
