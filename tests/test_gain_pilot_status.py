"""observation_gain_pilot.py status: "no pilot planned for this session" is an answer, not a failed job (BL-048)."""
import argparse
import json

import pytest

import observation_gain_pilot as gp


def test_status_without_a_planned_pilot_answers_not_planned_and_exits_zero(tmp_path, capsys):
    rc = gp.cmd_status(argparse.Namespace(session_dir=str(tmp_path)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["state"] is None and out["planned"] is False and out["session_dir"] == str(tmp_path)
    assert list(tmp_path.iterdir()) == []                      # a status query writes nothing


def test_status_with_a_planned_pilot_returns_its_state(tmp_path, capsys):
    (tmp_path / gp.STATE_FILENAME).write_text(json.dumps({"step": "PLANNED", "observation_name": "T"}))
    rc = gp.cmd_status(argparse.Namespace(session_dir=str(tmp_path)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["state"]["step"] == "PLANNED"


def test_every_other_command_still_refuses_without_a_planned_pilot(tmp_path):
    with pytest.raises(SystemExit, match="plan first"):
        gp._load_state(tmp_path)
