import pytest

import sdr_capture


def test_help_exits_without_touching_sdr(monkeypatch):
    async def unexpected_connect(self):
        raise AssertionError("--help must not connect to SDR")

    monkeypatch.setattr(sdr_capture.SDRCapture, "connect", unexpected_connect)
    with pytest.raises(SystemExit) as raised:
        sdr_capture.main(["--help"])
    assert raised.value.code == 0


def test_baseline_contract_requires_exact_fixed_configuration(tmp_path):
    parser = sdr_capture.build_cli_parser()
    args = parser.parse_args([
        "baseline-50ohm", "--output", str(tmp_path / "baseline.h5"),
        "--duration", "2", "--frequency", "1420405752", "--sample-rate", "2400000",
        "--gain", "40.2", "--topology", "50ohm", "--bias-t", "on", "--session-id", "e2e",
    ])
    sdr_capture._validate_baseline_request(args)
    args.gain = 0.0
    with pytest.raises(ValueError, match="contract violation"):
        sdr_capture._validate_baseline_request(args)
