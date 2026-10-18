"""Public command scopes and workflow-free operator routing."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from enginery.cli.main import _build_parser

_PROJECT_COMMANDS = {
    "init",
    "preflight",
    "collect",
    "run",
    "status",
    "explain",
    "check",
    "calibrate",
    "review",
    "feedback",
    "decide",
    "continue",
    "pause",
    "resume",
    "drain",
    "uninstall",
    "capability",
}
_HOST_COMMANDS = {"doctor", "adapter", "runner"}
_ENGINE_COMMANDS = {"engine", "outcome", "gate", "workspace"}
_ROOT_COMMANDS = _PROJECT_COMMANDS | _HOST_COMMANDS | _ENGINE_COMMANDS | {"operator", "policy"}
_NUMBERED_WORKFLOW_STAGE = re.compile(r"\bstage\s*[- ]?\s*\d+\b", re.IGNORECASE)


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    return next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )


def test_root_command_names_and_scope_labels() -> None:
    commands = _subparsers(_build_parser())
    assert commands is not None
    # Transitional groups may coexist during the cutover; consolidated names are mandatory.
    assert commands.choices.keys() >= _ROOT_COMMANDS
    labels = {
        **dict.fromkeys(_PROJECT_COMMANDS, "Project:"),
        **dict.fromkeys(_HOST_COMMANDS, "Host:"),
        **dict.fromkeys(_ENGINE_COMMANDS, "Engine ledger:"),
        "operator": "Operator ledger:",
        "policy": "Policy:",
    }
    entries = {entry.dest: entry.help for entry in commands._choices_actions}
    assert set(entries) == set(commands.choices)
    for name, help_text in entries.items():
        assert help_text.startswith(
            labels.get(name, ("Project:", "Host:", "Engine ledger:", "Operator ledger:", "Policy:"))
        ), (name, help_text)
        sentence = help_text.split(": ", 1)[1]
        assert sentence[0].isupper() and sentence.endswith("."), (name, help_text)


def test_recursive_help_has_no_numbered_workflow_stages() -> None:
    def visit(parser: argparse.ArgumentParser) -> None:
        # Inspect prose, not transitional parser names printed in usage and choices.
        prose = [parser.description or ""]
        for action in parser._actions:
            if isinstance(action.help, str):
                prose.append(action.help)
            if isinstance(action, argparse._SubParsersAction):
                prose.extend(str(entry.help) for entry in action._choices_actions)
                for entry in action._choices_actions:
                    sentence = str(entry.help).split(": ", 1)[1]
                    assert sentence[0].isupper() and sentence.endswith("."), entry.help
        help_text = "\n".join(prose)
        assert _NUMBERED_WORKFLOW_STAGE.search(help_text) is None, help_text
        children = _subparsers(parser)
        if children is not None:
            for child in children.choices.values():
                visit(child)

    visit(_build_parser())


@pytest.mark.parametrize("text", ["stage1", "Stage2", "stage 1", "stage-1"])
def test_numbered_workflow_stage_pattern(text: str) -> None:
    assert _NUMBERED_WORKFLOW_STAGE.search(text)


def test_importing_root_entry_point_is_workflow_free() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import enginery.cli.main; "
            "bad = [name for name in sys.modules if name.startswith('enginery.workflows')]; "
            "assert not bad, bad",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_every_command_leaf_accepts_help(capsys: pytest.CaptureFixture[str]) -> None:
    root = _build_parser()

    def visit(parser: argparse.ArgumentParser, path: list[str]) -> None:
        children = _subparsers(parser)
        if children is not None:
            for name, child in children.choices.items():
                visit(child, [*path, name])
            return
        with pytest.raises(SystemExit) as exited:
            root.parse_args([*path, "--help"])
        assert exited.value.code == 0, path
        captured = capsys.readouterr()
        assert "usage:" in captured.out, path
        assert not captured.err, (path, captured.err)

    visit(root, [])


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["status"],
        ["pause", "--reason", "maintenance"],
        ["resume"],
        ["drain", "--wait-seconds", "0"],
        ["drain", "--stop"],
        # Invalid image refuses before any OCI-engine invocation.
        ["select-runner", "--image", "not-an-image-digest"],
        ["backup", "--output", "{backup}"],
        ["restore", "--backup", "{backup}"],
        ["verify", "--json"],
        ["rebuild-projections"],
    ],
    ids=[
        "help",
        "status",
        "pause",
        "resume",
        "drain-wait",
        "drain-stop",
        "select-runner",
        "backup",
        "restore",
        "verify",
        "rebuild-projections",
    ],
)
def test_operator_commands_do_not_import_workflows(tmp_path: Path, arguments: list[str]) -> None:
    database = tmp_path / "ledger.sqlite"
    backup = tmp_path / "backup"
    argv = [argument.replace("{backup}", str(backup)) for argument in arguments]
    if arguments != ["--help"]:
        argv += ["--database", str(database)]
    script = """
import contextlib
import io
import json
import sys
from pathlib import Path
from enginery.cli.main import main
assert not any(name.startswith('enginery.workflows') for name in sys.modules)
from enginery.ledger.service import LedgerService
LedgerService.open(Path(sys.argv[2])).close()
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    try:
        code = main(['operator', *json.loads(sys.argv[1])])
    except SystemExit as error:
        assert json.loads(sys.argv[1]) == ['--help'], error
        code = error.code
bad = sorted(name for name in sys.modules if name.startswith('enginery.workflows'))
assert not bad, bad
print(json.dumps({'code': code, 'stdout': out.getvalue(), 'stderr': err.getvalue()}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(argv), str(database)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert isinstance(report["code"], int)
    assert report["stdout"] or report["stderr"]
    assert "invalid choice" not in report["stderr"]
    if arguments == ["--help"]:
        assert report["code"] == 0
        assert "enginery operator" in report["stdout"]
        for verb in (
            "status",
            "pause",
            "resume",
            "drain",
            "select-runner",
            "backup",
            "restore",
            "verify",
            "rebuild-projections",
        ):
            assert verb in report["stdout"]
    elif arguments == ["status"]:
        assert report["code"] == 0
        assert json.loads(report["stdout"])["ledger"]["healthy"] is True
