from __future__ import annotations

import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from enginery.application.human_decisions import (
    MissingDecisionError,
    SubjectMismatchError,
    decision_commands,
)
from enginery.engine.runtime import CoordinatorRuntime
from enginery.ledger.service import LedgerService
from enginery.workflows import stage1
from enginery.workflows.review import ReviewOutcome
from enginery.workflows.stage1 import Stage1RunService
from tests.cli.test_stage1 import _request


@pytest.fixture
def publication_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reach the publication authority boundary without invoking external adapters."""
    for name in ("_require_executable", "_require_passed_node", "_require_validation_artifact"):
        monkeypatch.setattr(Stage1RunService, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(Stage1RunService, "_review_outcome", lambda *args: ReviewOutcome.APPROVED)
    monkeypatch.setattr(Stage1RunService, "_implement_generation", lambda *args: 0)
    monkeypatch.setattr(Stage1RunService, "_open_pr_binding", lambda *args, **kwargs: "open_pr-0")
    monkeypatch.setattr(Stage1RunService, "_require_branch_publisher", lambda *args: object())
    monkeypatch.setattr(
        CoordinatorRuntime, "register_node",
        lambda *args, **kwargs: SimpleNamespace(epoch=1),
    )


@pytest.mark.usefixtures("publication_boundary")
@pytest.mark.parametrize(
    "decided_digest", [None, "different-protected-digest", "actual-protected-digest"]
)
def test_publication_requires_record_bound_by_decided_protected_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decided_digest: str | None
) -> None:
    request = _request(tmp_path)
    fields = {} if decided_digest is None else {"protected_publication_digest": decided_digest}
    monkeypatch.setattr(
        stage1, "list_decisions",
        lambda *args: [SimpleNamespace(id="authenticated-decision", subject_fields=fields)],
    )
    monkeypatch.setattr(
        Stage1RunService, "_require_protected_publication_decision",
        lambda *args, **kwargs: {"digest": "actual-protected-digest"},
    )
    effects: list[str] = []

    def publish(*args: object, **kwargs: object) -> None:
        effects.append("publish")
        raise RuntimeError("publication boundary reached")

    monkeypatch.setattr(Stage1RunService, "_publish_verified_branch", publish)
    monkeypatch.setattr(
        Stage1RunService, "_require_pull_requests",
        lambda *args: effects.append("pull_request"),
    )
    with LedgerService.open(tmp_path / "ledger.db") as ledger:
        service = Stage1RunService(
            runtime=CoordinatorRuntime(ledger, owner="stage1-cli"), ledger=ledger,
            publication_authorizer=lambda request: "authenticated-decision",
        )
        expected_error = (
            pytest.raises(RuntimeError, match="publication boundary reached")
            if decided_digest == "actual-protected-digest"
            else pytest.raises(SubjectMismatchError, match="protected paths differ")
        )
        with expected_error:
            service.open_pull_request(
                request, now=datetime.now(tz=UTC), heartbeat_window=timedelta(seconds=60)
            )
    assert effects == (["publish"] if decided_digest == "actual-protected-digest" else [])


@pytest.mark.usefixtures("publication_boundary")
def test_stage1_publication_hold_names_quoted_explicit_ledger_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    database = tmp_path / "operator's ledger; not-a-command.db"
    owner = "operator's coordinator; not-a-command"
    held: list[dict[str, object]] = []
    monkeypatch.setattr(Stage1RunService, "_node_status", lambda *args: "queued")
    monkeypatch.setattr(
        CoordinatorRuntime, "await_human_node", lambda *args, **kwargs: held.append(kwargs)
    )

    def refuse(request: object) -> str:
        raise MissingDecisionError("review required")

    with LedgerService.open(database) as ledger:
        service = Stage1RunService(
            runtime=CoordinatorRuntime(ledger, owner=owner), ledger=ledger,
            publication_authorizer=refuse,
        )
        with pytest.raises(stage1.HumanActionRequiredError):
            service.open_pull_request(
                request, now=datetime.now(tz=UTC), heartbeat_window=timedelta(seconds=60)
            )
    review, decide = decision_commands(
        str(request.run.id), str(request.repository_path), "pull_request.open",
        database=str(database), owner=owner,
    )
    assert held[0]["extra"] == {
        "reason": "decision_required", "repair": f"{review}; {decide}",
    }
    for command, verb in ((review, "review"), (decide, "decide")):
        argv = shlex.split(command)
        assert argv[:4] == ["enginery", "factory", verb, str(request.run.id)]
        assert argv[argv.index("--database") + 1] == str(database)
        assert argv[argv.index("--owner") + 1] == owner


@pytest.mark.parametrize("action", ["pull_request.open", "human_wait.plan_approval.requeue"])
def test_shared_decision_commands_preserve_every_interpolated_shell_argument(action: str) -> None:
    run_id = "run ' ; $(touch bad)"
    project = "/project's checkout; $(touch bad)"
    database = "/ledger's state; $(touch bad).db"
    owner = "owner ' ; $(touch bad)"
    review, decide = decision_commands(run_id, project, action, database=database, owner=owner)
    common = ["--project", project, "--database", database, "--owner", owner]
    assert shlex.split(review) == ["enginery", "factory", "review", run_id, *common]
    expected = [
        "enginery", "factory", "decide", run_id, *common,
        "--action", action, "--subject-digest", "DIGEST", "--challenge", "CHALLENGE",
    ]
    if action.startswith("human_wait."):
        expected.extend(("--reason", "REASON"))
    assert shlex.split(decide) == expected
