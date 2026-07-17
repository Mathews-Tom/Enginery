from __future__ import annotations

import json
from pathlib import Path

import pytest

from enginery.cli._exit_codes import exit_code_for
from enginery.cli.main import main
from enginery.domain.errors import FailureClass


def test_policy_explain_unknown_action_denies(capsys: pytest.CaptureFixture[str]) -> None:
    fixture = Path("tests/fixtures/policy/unknown-action.json")

    exit_code = main(["policy", "explain", str(fixture)])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == exit_code_for(FailureClass.POLICY_DENIAL)
    assert payload["result"] == "deny"
    assert payload["rule_id"] == "unknown_action"
