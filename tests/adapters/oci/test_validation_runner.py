"""The isolated validation runner: launcher contract, redaction and outcome classification."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from enginery.adapters.oci.launcher import write_outcome
from enginery.adapters.oci.validation import OciValidationRunner
from enginery.adapters.runner_launch import LAUNCHER_MODULE, MAX_RESULT_BYTES
from enginery.domain.errors import WorkerFailureError
from enginery.domain.runner import (
    DEFAULT_ENV_ALLOWLIST,
    LaunchOutcome,
    LaunchSpec,
    OutcomeState,
    RunnerContext,
    RunnerProfile,
    RunnerSelection,
    outcome_path_for,
)
from enginery.engine.task_repository import out_path_for

from .support import IMAGE_ID

_SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_CREDENTIAL_WORDS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL", "COOKIE")
_NONCE = "v" * 32


class FakeLauncher:
    """A launcher stand-in: parses the spec like the real one, plays the in-container wrapper
    and records its own outcome the way the real launcher does."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        output: str = "all good\n",
        stderr: str = "",
        write_result: Callable[[Path, LaunchSpec], None] | None = None,
        state: OutcomeState = OutcomeState.EXITED,
        process_returncode: int | None = None,
        record_outcome: bool = True,
        nonce: str = _NONCE,
    ) -> None:
        self.returncode = returncode
        self.output = output
        self.stderr = stderr
        self.write_result = write_result
        self.state = state
        self.process_returncode = returncode if process_returncode is None else process_returncode
        self.record_outcome = record_outcome
        self.nonce = nonce
        self.argv: tuple[str, ...] = ()
        self.spec: LaunchSpec | None = None
        self.raw_spec: dict[str, Any] = {}

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.argv = tuple(argv)
        flag = self.argv.index("--spec")
        self.raw_spec = json.loads(self.argv[flag + 1])
        self.spec = LaunchSpec.from_state(self.raw_spec)
        result = Path(self.spec.out_path) / self.spec.result_name
        if self.write_result is not None:
            self.write_result(result, self.spec)
        else:
            result.write_text(
                json.dumps({"output": self.output, "nonce": self.nonce}), encoding="utf-8"
            )
        if self.record_outcome:
            exited = self.returncode if self.state is OutcomeState.EXITED else None
            write_outcome(
                outcome_path_for(self.spec.receipt_path),
                LaunchOutcome(
                    operation_id=self.spec.operation_id,
                    nonce=self.nonce,
                    state=self.state,
                    exit_code=exited,
                    detail="recorded by the fake launcher",
                ),
            )
        return subprocess.CompletedProcess(self.argv, self.process_returncode, "", self.stderr)


def _context(tmp_path: Path) -> RunnerContext:
    profile = RunnerProfile()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    return RunnerContext(
        selection=RunnerSelection.isolated(image=IMAGE_ID, profile=profile),
        profile=profile,
        receipt_root=str(receipts),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def test_the_container_exit_code_and_redacted_output_come_back(
    tmp_path: Path, workspace: Path
) -> None:
    launcher = FakeLauncher(
        returncode=3, output=f"FAILED tests/test_x.py\nGITHUB_TOKEN={_SECRET}\n"
    )
    runner = OciValidationRunner(_context(tmp_path), launcher=launcher)

    completed = runner(("pytest", "-q", "tests"), workspace)

    assert completed.returncode == 3
    assert "FAILED tests/test_x.py" in completed.stdout
    assert _SECRET not in completed.stdout
    assert completed.stderr == ""
    assert completed.args == ("pytest", "-q", "tests")


def test_a_passing_command_reports_exit_zero(tmp_path: Path, workspace: Path) -> None:
    runner = OciValidationRunner(_context(tmp_path), launcher=FakeLauncher(output="2 passed\n"))

    completed = runner(("pytest",), workspace)

    assert completed.returncode == 0
    assert completed.stdout == "2 passed\n"


def test_the_launcher_receives_the_command_after_the_spec_separator(
    tmp_path: Path, workspace: Path
) -> None:
    launcher = FakeLauncher()

    OciValidationRunner(_context(tmp_path), launcher=launcher)(("make", "check", "-j4"), workspace)

    assert launcher.argv[:3] == (sys.executable, "-m", LAUNCHER_MODULE)
    separator = launcher.argv.index("--")
    assert launcher.argv[separator + 1 :] == ("make", "check", "-j4")
    assert launcher.argv.index("--spec") < separator


def test_the_spec_binds_the_workspace_a_per_attempt_out_dir_and_the_pinned_image(
    tmp_path: Path, workspace: Path
) -> None:
    context = _context(tmp_path)
    launcher = FakeLauncher()
    runner = OciValidationRunner(context, time_budget_seconds=77, launcher=launcher)

    runner(("true",), workspace)

    spec = launcher.spec
    assert spec is not None
    assert spec.image == IMAGE_ID
    assert spec.profile.digest == context.profile.digest
    assert spec.harness == "validation"
    assert spec.workspace_path == str(workspace)
    assert spec.time_budget_seconds == 77
    out = Path(spec.out_path)
    assert out.parent == out_path_for(workspace)
    assert out.is_dir()
    assert out == out_path_for(workspace) / out.name
    assert not str(out).startswith(str(workspace) + "/")
    assert spec.result_name == "result.json"
    # The receipt lands in the host-only receipt root, outside every worker-writable mount.
    receipt = Path(spec.receipt_path)
    assert receipt.parent == Path(context.receipt_root)
    assert not receipt.is_relative_to(workspace)
    assert not receipt.is_relative_to(out_path_for(workspace))
    assert spec.bundle_branch is None
    assert spec.permissions.unconditional_bypass is False
    assert spec.permissions.arguments == ()


def test_the_spec_environment_is_inside_the_allowlist_and_carries_no_credential(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(name, _SECRET)
    launcher = FakeLauncher()

    OciValidationRunner(_context(tmp_path), launcher=launcher)(("true",), workspace)

    spec = launcher.spec
    assert spec is not None
    keys = [key for key, _ in spec.env]
    assert keys, "the worker gets an explicit environment"
    assert set(keys) <= set(DEFAULT_ENV_ALLOWLIST)
    assert len(keys) == len(set(keys))
    credential_like = [
        key
        for key in keys
        if any(word in key.upper() for word in _CREDENTIAL_WORDS)
        and not key.startswith("GIT_CONFIG_KEY_")  # git's command-scoped config slot, not a key
    ]
    assert not credential_like
    assert _SECRET not in json.dumps(launcher.raw_spec)
    assert dict(spec.env)["HOME"] == "/home/worker"


def test_every_attempt_uses_a_fresh_out_directory_and_operation(
    tmp_path: Path, workspace: Path
) -> None:
    specs: list[LaunchSpec] = []

    def record(result: Path, spec: LaunchSpec) -> None:
        specs.append(spec)
        result.write_text(json.dumps({"output": "", "nonce": _NONCE}), encoding="utf-8")

    runner = OciValidationRunner(_context(tmp_path), launcher=FakeLauncher(write_result=record))

    runner(("true",), workspace)
    runner(("true",), workspace)

    first, second = specs
    assert first.out_path != second.out_path
    assert first.operation_id != second.operation_id
    assert first.container_name != second.container_name
    assert first.receipt_path != second.receipt_path


# --- classification comes from the launcher's outcome, not its exit status ------------


def test_a_refused_launch_is_a_worker_failure_not_a_validation_failure(
    tmp_path: Path, workspace: Path
) -> None:
    launcher = FakeLauncher(
        state=OutcomeState.REFUSED,
        process_returncode=125,
        stderr="engine refused: profile not enforceable\n",
    )

    with pytest.raises(WorkerFailureError, match="refused to launch") as caught:
        OciValidationRunner(_context(tmp_path), launcher=launcher)(("pytest",), workspace)

    assert "engine refused" in str(caught.value.details["stderr"])
    assert caught.value.details["state"] == "refused"


@pytest.mark.parametrize("exit_code", [124, 125, 126, 137])
def test_a_command_that_exits_124_or_125_is_a_failed_validation_not_a_refused_launch(
    tmp_path: Path, workspace: Path, exit_code: int
) -> None:
    """The exit-125 spoof: a candidate-authored command can exit with the launcher's own
    fault statuses. The launcher recorded that the container exited, so it is just a failure."""
    launcher = FakeLauncher(returncode=exit_code, output="candidate said no\n")

    completed = OciValidationRunner(_context(tmp_path), launcher=launcher)(("pytest",), workspace)

    assert completed.returncode == exit_code
    assert completed.stdout == "candidate said no\n"


@pytest.mark.parametrize(
    "state",
    [OutcomeState.REFUSED, OutcomeState.DEADLINE, OutcomeState.INTERRUPTED, OutcomeState.FAULTED],
)
def test_a_launcher_that_exits_zero_but_recorded_no_exit_is_never_a_passing_validation(
    tmp_path: Path, workspace: Path, state: OutcomeState
) -> None:
    """The reverse spoof: a result file that says success cannot outvote the launcher."""
    launcher = FakeLauncher(state=state, process_returncode=0, output="all passed\n")

    with pytest.raises(WorkerFailureError) as caught:
        OciValidationRunner(_context(tmp_path), launcher=launcher)(("pytest",), workspace)

    assert caught.value.details["state"] == state.value


@pytest.mark.parametrize("state", [OutcomeState.LAUNCHING, OutcomeState.RUNNING])
def test_an_unfinished_launcher_outcome_is_a_worker_failure(
    tmp_path: Path, workspace: Path, state: OutcomeState
) -> None:
    launcher = FakeLauncher(state=state, process_returncode=0)

    with pytest.raises(WorkerFailureError, match="did not finish"):
        OciValidationRunner(_context(tmp_path), launcher=launcher)(("pytest",), workspace)


def test_a_launcher_that_recorded_no_outcome_is_a_worker_failure_with_its_stderr(
    tmp_path: Path, workspace: Path
) -> None:
    launcher = FakeLauncher(record_outcome=False, stderr="launcher: could not start\n")

    with pytest.raises(WorkerFailureError, match="outcome is missing") as caught:
        OciValidationRunner(_context(tmp_path), launcher=launcher)(("pytest",), workspace)

    assert "could not start" in str(caught.value.details["stderr"])


def test_an_outcome_recorded_for_another_operation_is_refused(
    tmp_path: Path, workspace: Path
) -> None:
    def other_operation(result: Path, spec: LaunchSpec) -> None:
        result.write_text(json.dumps({"output": "x", "nonce": _NONCE}), encoding="utf-8")
        write_outcome(
            outcome_path_for(spec.receipt_path),
            LaunchOutcome(
                operation_id="validate:someone-else",
                nonce=_NONCE,
                state=OutcomeState.EXITED,
                exit_code=0,
            ),
        )

    launcher = FakeLauncher(write_result=other_operation, record_outcome=False)

    with pytest.raises(WorkerFailureError, match="another operation"):
        OciValidationRunner(_context(tmp_path), launcher=launcher)(("pytest",), workspace)


# --- the result file is untrusted ------------------------------------------------------


def _write_text(content: str) -> Callable[[Path, LaunchSpec], None]:
    def write(result: Path, _spec: LaunchSpec) -> None:
        result.write_text(content, encoding="utf-8")

    return write


def _write_nothing(_result: Path, _spec: LaunchSpec) -> None:
    return None


def _write_oversized(result: Path, _spec: LaunchSpec) -> None:
    with result.open("wb") as handle:
        handle.truncate(MAX_RESULT_BYTES + 1)


def _write_directory(result: Path, _spec: LaunchSpec) -> None:
    result.mkdir()


def _write_symlink(result: Path, spec: LaunchSpec) -> None:
    """A result that is a link to a host-readable file the worker could not otherwise read."""
    target = Path(spec.workspace_path).parent / "host-secret.json"
    target.write_text(json.dumps({"output": "SECRET", "nonce": _NONCE}), encoding="utf-8")
    result.symlink_to(target)


def _write_fifo(result: Path, _spec: LaunchSpec) -> None:
    os.mkfifo(result)


def _bound(payload: dict[str, object]) -> str:
    return json.dumps({**payload, "nonce": _NONCE})


@pytest.mark.parametrize(
    ("writer", "message"),
    [
        (_write_nothing, "missing or unreadable"),
        (_write_oversized, "limit"),
        (_write_directory, "not a regular file"),
        (_write_symlink, "missing or unreadable"),
        (_write_fifo, "not a regular file"),
        (_write_text("{not json"), "not valid JSON"),
        (_write_text(json.dumps(["output"])), "invalid shape"),
        (_write_text(_bound({"output": 7})), "no output"),
        (_write_text(_bound({"stdout": "x"})), "no output"),
        (_write_text(json.dumps({"output": "x"})), "not bound to this attempt"),
        (
            _write_text(json.dumps({"output": "x", "nonce": "z" * 32})),
            "not bound to this attempt",
        ),
        (_write_text(json.dumps({"output": "x", "nonce": 7})), "not bound to this attempt"),
    ],
    ids=[
        "missing",
        "oversized",
        "directory",
        "symlink",
        "fifo",
        "invalid-json",
        "list",
        "non-string",
        "wrong-key",
        "unbound",
        "stale-nonce-replay",
        "non-text-nonce",
    ],
)
def test_an_untrustworthy_result_is_a_worker_failure_even_when_the_container_exited_zero(
    tmp_path: Path, workspace: Path, writer: Callable[[Path, LaunchSpec], None], message: str
) -> None:
    launcher = FakeLauncher(returncode=0, write_result=writer)

    with pytest.raises(WorkerFailureError, match=message):
        OciValidationRunner(_context(tmp_path), launcher=launcher)(("pytest",), workspace)


def test_the_real_launcher_seam_is_a_subprocess_with_a_closed_stdin(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default launcher runs the argv as a real subprocess and hands back its stderr."""
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        spec = LaunchSpec.from_state(json.loads(argv[argv.index("--spec") + 1]))
        (Path(spec.out_path) / spec.result_name).write_text(
            json.dumps({"output": "ok", "nonce": _NONCE}), encoding="utf-8"
        )
        write_outcome(
            outcome_path_for(spec.receipt_path),
            LaunchOutcome(
                operation_id=spec.operation_id,
                nonce=_NONCE,
                state=OutcomeState.EXITED,
                exit_code=0,
            ),
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("enginery.adapters.oci.validation.subprocess.run", fake_run)

    completed = OciValidationRunner(_context(tmp_path))(("true",), workspace)

    assert completed.stdout == "ok"
    assert seen["argv"][:3] == [sys.executable, "-m", LAUNCHER_MODULE]
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
    assert seen["kwargs"]["capture_output"] is True
