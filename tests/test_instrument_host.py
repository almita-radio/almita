"""instrument_host: rtl_tcp control and evidence local on the Pi 5, over ssh on the notebook (Pi 4 at the tripod)."""
import json
import os

import pytest

import instrument_host as ih


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "instrument.json"
    monkeypatch.setenv(ih.INSTRUMENT_FILE_ENV, str(path))
    return path


def test_the_test_suite_never_reaches_a_real_instrument_file():
    assert os.environ[ih.INSTRUMENT_FILE_ENV] == os.devnull and ih.ssh_target() is None


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"ssh": ""}', '{"ssh": "  "}', '{"ssh": 5}', "{}"])
def test_missing_or_unusable_config_means_local(config, content):
    if content is not None:
        config.write_text(content)
    assert ih.ssh_target() is None
    assert ih.command(["systemctl", "is-active", "rtl_tcp.service"]) == ["systemctl", "is-active", "rtl_tcp.service"]
    assert ih.where() == "this machine"


def test_ssh_target_wraps_the_command_quoted_as_one_remote_argument(config):
    config.write_text(json.dumps({"ssh": "almita-pi4"}))
    cmd = ih.command(["journalctl", "-u", "rtl_tcp.service", "--since", "Sun 2026-11-01 22:09:42 UTC"])
    assert cmd[0] == "ssh" and "BatchMode=yes" in cmd and cmd[-2] == "almita-pi4"
    assert cmd[-1] == "journalctl -u rtl_tcp.service --since 'Sun 2026-11-01 22:09:42 UTC'"
    assert ih.where() == "almita-pi4 (ssh)"


def test_read_bytes_is_a_plain_read_locally(config, tmp_path):
    f = tmp_path / "cmdline"
    f.write_bytes(b"rtl_tcp\0-T\0")
    assert ih.read_bytes(f) == b"rtl_tcp\0-T\0"


def test_sdr_tuning_sends_its_journal_and_systemctl_reads_to_the_instrument(config, monkeypatch):
    import sdr_tuning
    config.write_text(json.dumps({"ssh": "almita-pi4"}))
    seen = []

    class Out:
        stdout = "/usr/bin/stdbuf -oL /usr/bin/rtl_tcp -d 00000001 -T"

    def run(argv, **kw):
        seen.append(argv)
        return Out()
    assert sdr_tuning.main_service_has_bias_tee_flag(run=run) is True
    assert seen[0][0] == "ssh" and seen[0][-1] == "systemctl show -p ExecStart --value rtl_tcp.service"
