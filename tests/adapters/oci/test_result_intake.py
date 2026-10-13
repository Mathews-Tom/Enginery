"""Reading what a worker wrote: bounded, regular files only, bound to the attempt."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from enginery.adapters.runner_launch import (
    bind_result_to_attempt,
    read_bounded_text,
    read_launch_outcome,
    require_exited,
)
from enginery.domain.errors import (
    HumanActionRequiredError,
    InvalidInputError,
    WorkerFailureError,
)
from enginery.domain.runner import LaunchOutcome, OutcomeState, outcome_path_for

_NONCE = "c" * 32


# --- read_bounded_text --------------------------------------------------------------------


def test_a_regular_file_within_the_limit_is_read(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("héllo", encoding="utf-8")

    assert read_bounded_text(path, limit=64, what="result") == "héllo"


def test_a_file_of_exactly_the_limit_is_read_and_one_byte_more_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_bytes(b"x" * 10)
    assert read_bounded_text(path, limit=10, what="result") == "x" * 10

    path.write_bytes(b"x" * 11)
    with pytest.raises(WorkerFailureError, match="exceeds the 10-byte limit"):
        read_bounded_text(path, limit=10, what="result")


def test_a_file_that_grows_after_the_size_check_is_still_bounded(tmp_path: Path) -> None:
    """The read itself stops at limit + 1, whatever fstat said."""
    path = tmp_path / "result.json"
    path.write_bytes(b"x" * 10)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        assert os.fstat(descriptor).st_size == 10
    finally:
        os.close(descriptor)
    path.write_bytes(b"x" * 5000)

    with pytest.raises(WorkerFailureError, match="limit"):
        read_bounded_text(path, limit=10, what="result")


def test_a_symlink_is_refused_even_to_a_readable_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("HOST SECRET", encoding="utf-8")
    link = tmp_path / "result.json"
    link.symlink_to(secret)

    with pytest.raises(WorkerFailureError) as caught:
        read_bounded_text(link, limit=1024, what="result")

    assert "HOST SECRET" not in str(caught.value)
    assert "missing or unreadable" in str(caught.value)


def test_a_dangling_symlink_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "result.json"
    link.symlink_to(tmp_path / "nothing-here")

    with pytest.raises(WorkerFailureError, match="missing or unreadable"):
        read_bounded_text(link, limit=1024, what="result")


def test_a_fifo_is_refused_without_blocking_the_host(tmp_path: Path) -> None:
    fifo = tmp_path / "result.json"
    os.mkfifo(fifo)
    outcome: list[BaseException | str] = []

    def attempt() -> None:
        try:
            outcome.append(read_bounded_text(fifo, limit=1024, what="result"))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "reading a FIFO blocked the host"
    (error,) = outcome
    assert isinstance(error, WorkerFailureError)
    assert "not a regular file" in str(error)


def test_a_directory_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "result.json"
    directory.mkdir()

    with pytest.raises(WorkerFailureError, match="not a regular file"):
        read_bounded_text(directory, limit=1024, what="result")


def test_a_missing_file_and_a_missing_directory_are_refused(tmp_path: Path) -> None:
    with pytest.raises(WorkerFailureError, match="missing or unreadable"):
        read_bounded_text(tmp_path / "absent.json", limit=1024, what="result")
    with pytest.raises(WorkerFailureError, match="missing or unreadable"):
        read_bounded_text(tmp_path / "no-dir" / "result.json", limit=1024, what="result")


def test_bytes_that_are_not_utf8_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(WorkerFailureError, match="missing or unreadable"):
        read_bounded_text(path, limit=1024, what="result")


def test_a_symlinked_parent_directory_of_a_regular_file_is_how_the_host_itself_reaches_it(
    tmp_path: Path,
) -> None:
    """Only the final component is the worker's: the host's own directories may be links."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "result.json").write_text("ok", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    assert read_bounded_text(alias / "result.json", limit=64, what="result") == "ok"


# --- bind_result_to_attempt ---------------------------------------------------------------


def test_a_result_that_echoes_the_nonce_is_returned_without_it() -> None:
    text = json.dumps({"operation_id": "op", "output": "x", "nonce": _NONCE})

    bound = json.loads(bind_result_to_attempt(text, nonce=_NONCE, what="result"))

    assert bound == {"operation_id": "op", "output": "x"}


@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"output": "x"}),
        json.dumps({"output": "x", "nonce": "d" * 32}),
        json.dumps({"output": "x", "nonce": None}),
        json.dumps({"output": "x", "nonce": [_NONCE]}),
        json.dumps({"output": "x", "nonce": _NONCE.upper()}),
    ],
)
def test_a_result_that_does_not_echo_this_nonce_is_refused(text: str) -> None:
    with pytest.raises(WorkerFailureError, match="not bound to this attempt"):
        bind_result_to_attempt(text, nonce=_NONCE, what="result")


@pytest.mark.parametrize(("text", "message"), [("{", "not valid JSON"), ("[1]", "invalid shape")])
def test_a_result_that_is_not_a_json_object_is_refused(text: str, message: str) -> None:
    with pytest.raises(WorkerFailureError, match=message):
        bind_result_to_attempt(text, nonce=_NONCE, what="result")


# --- the launcher outcome -----------------------------------------------------------------


def _write_outcome(receipt: Path, outcome: LaunchOutcome) -> None:
    Path(outcome_path_for(str(receipt))).write_text(
        json.dumps(outcome.to_state()), encoding="utf-8"
    )


def test_an_outcome_round_trips_and_is_read_from_beside_the_receipt(tmp_path: Path) -> None:
    receipt = tmp_path / "op.runner-receipt.json"
    written = LaunchOutcome(
        operation_id="op",
        nonce=_NONCE,
        state=OutcomeState.EXITED,
        exit_code=3,
        detail="d",
        violations=("v",),
    )
    _write_outcome(receipt, written)

    read = read_launch_outcome(str(receipt), operation_id="op", what="worker")

    assert read == written
    assert LaunchOutcome.from_state(written.to_state()) == written
    assert outcome_path_for(str(receipt)) == f"{receipt}.outcome.json"


def test_an_outcome_of_another_operation_is_refused(tmp_path: Path) -> None:
    receipt = tmp_path / "op.runner-receipt.json"
    _write_outcome(receipt, LaunchOutcome("other", _NONCE, OutcomeState.EXITED, exit_code=0))

    with pytest.raises(WorkerFailureError, match="another operation"):
        read_launch_outcome(str(receipt), operation_id="op", what="worker")


@pytest.mark.parametrize(
    "content",
    [
        "{",
        "[]",
        json.dumps({"schema_version": 2}),
        json.dumps(
            {"schema_version": 1, "operation_id": "op", "nonce": _NONCE, "state": "mystery"}
        ),
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "op",
                "nonce": _NONCE,
                "state": "exited",
                "exit_code": "0",
                "violations": [],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": "op",
                "nonce": _NONCE,
                "state": "refused",
                "exit_code": 0,
                "violations": [],
            }
        ),
    ],
)
def test_a_malformed_outcome_is_a_worker_failure(tmp_path: Path, content: str) -> None:
    receipt = tmp_path / "op.runner-receipt.json"
    Path(outcome_path_for(str(receipt))).write_text(content, encoding="utf-8")

    with pytest.raises(WorkerFailureError):
        read_launch_outcome(str(receipt), operation_id="op", what="worker")


def test_a_missing_outcome_is_a_worker_failure(tmp_path: Path) -> None:
    with pytest.raises(WorkerFailureError, match="missing or unreadable"):
        read_launch_outcome(str(tmp_path / "op.json"), operation_id="op", what="worker")


def test_an_outcome_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    receipt = tmp_path / "op.json"
    forged = tmp_path / "forged.json"
    forged.write_text(
        json.dumps(LaunchOutcome("op", _NONCE, OutcomeState.EXITED, exit_code=0).to_state()),
        encoding="utf-8",
    )
    Path(outcome_path_for(str(receipt))).symlink_to(forged)

    with pytest.raises(WorkerFailureError, match="missing or unreadable"):
        read_launch_outcome(str(receipt), operation_id="op", what="worker")


@pytest.mark.parametrize(
    "state",
    [
        OutcomeState.REFUSED,
        OutcomeState.DEADLINE,
        OutcomeState.INTERRUPTED,
        OutcomeState.FAULTED,
        OutcomeState.LAUNCHING,
        OutcomeState.RUNNING,
    ],
)
def test_every_outcome_that_is_not_an_exit_is_a_worker_failure(state: OutcomeState) -> None:
    outcome = LaunchOutcome("op", _NONCE, state, detail="why", violations=("a", "b"))

    with pytest.raises(WorkerFailureError) as caught:
        require_exited(outcome, what="worker")

    assert caught.value.details["state"] == state.value
    assert caught.value.details["detail"] == "why"
    assert caught.value.details["violations"] == "a; b"


def test_a_bound_exceeded_outcome_is_held_for_the_operator() -> None:
    outcome = LaunchOutcome("op", _NONCE, OutcomeState.BOUND_EXCEEDED, detail="too big")

    with pytest.raises(HumanActionRequiredError, match="outgrew its disk bound"):
        require_exited(outcome, what="worker")


@pytest.mark.parametrize("code", [0, 1, 124, 125, 137])
def test_an_exit_is_returned_as_the_workers_own_number(code: int) -> None:
    assert (
        require_exited(LaunchOutcome("op", _NONCE, OutcomeState.EXITED, exit_code=code), what="w")
        == code
    )


def test_only_an_exited_outcome_may_carry_an_exit_code() -> None:
    with pytest.raises(InvalidInputError, match="exit code"):
        LaunchOutcome("op", _NONCE, OutcomeState.REFUSED, exit_code=0)
    with pytest.raises(InvalidInputError, match="exit code"):
        LaunchOutcome("op", _NONCE, OutcomeState.EXITED)
