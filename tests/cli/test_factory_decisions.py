"""Authenticated terminal ingress over real requests, Git objects and persisted packets."""

from __future__ import annotations

import getpass
import hashlib
import io
import json
import os
import pty
import shlex
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from enginery.application.human_decisions import (
    ActionMismatchError,
    AuthenticationError,
    ExpiredChallengeError,
    PrincipalMismatchError,
    ReplayedChallengeError,
    StaleSubjectError,
    SubjectMismatchError,
    SupersededDecisionError,
)
from enginery.application.layered_config import ConfigError, parse_layer_document
from enginery.application.review_packet import ReviewPacket
from enginery.cli import factory_decisions, factory_review
from enginery.domain.digests import Digest
from enginery.domain.execution_selection import SelectionLayer
from enginery.domain.ids import NodeId
from enginery.domain.workflow.node import ActorType
from enginery.engine.runtime import CoordinatorRuntime, WorkflowNodeDispatch, _request_state
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.human_decisions import list_decisions
from enginery.ledger.service import LedgerService
from enginery.policy.rules import HardRuleError
from enginery.workflows.run_intent import Stage1RunRequest
from enginery.workflows.run_lifecycle import Stage1RunService
from tests.cli.test_engine_runs import _dispatch, _start_request

ACTION = "pull_request.open"
RUN = "run-stage1"


def _preferences(**changes: object) -> bytes:
    values: dict[str, object] = {
        "principal_id": "operator",
        "os_user": getpass.getuser(),
        "os_uid": os.getuid(),
    }
    values.update(changes)
    lines = ["schema_version = 1", "[review_authority]"]
    lines.extend(f"{key} = {json.dumps(value)}" for key, value in values.items())
    return ("\n".join(lines) + "\n").encode()


@dataclass
class Ingress:
    project: Path
    ledger: LedgerService
    request: Stage1RunRequest
    database: Path
    preferences: Path

    @property
    def packet(self) -> ReviewPacket:
        return factory_decisions.build_packet(self.project, self.ledger, self.request)

    def persist(self, aggregate_type: str, aggregate_id: str, state: dict[str, object]) -> None:
        projection = self.ledger.read_projection(
            aggregate_type=aggregate_type, aggregate_id=aggregate_id
        )
        version = 0 if projection is None else projection.aggregate_version
        self.ledger.append(
            AppendCommand(
                correlation_id=f"fixture:{aggregate_type}:{aggregate_id}:{version}",
                events=(
                    EventWrite(
                        aggregate_type=aggregate_type,
                        aggregate_id=aggregate_id,
                        expected_version=version,
                        event_type=f"{aggregate_type}.fixture_persisted",
                        schema_version=1,
                        payload=state,
                    ),
                ),
            )
        )

    def state(self, node_id: str) -> dict[str, object]:
        return dict(factory_decisions._state(self.ledger, RUN, node_id))

    def update_node(self, node_id: str, **changes: object) -> None:
        self.persist("runtime_node", f"{RUN}:{node_id}", {**self.state(node_id), **changes})

    def replace_request(self, **changes: Any) -> None:
        request = replace(self.request, **changes)
        projection = self.ledger.read_projection(aggregate_type="run", aggregate_id=RUN)
        assert projection is not None
        self.persist("run", RUN, {**projection.state, **request.initial_state()})
        self.request = request

    def register(self, node_id: str, *, outcome: str | None = None) -> None:
        runtime = CoordinatorRuntime(self.ledger, owner="stage1-cli")
        node = self.request.manifest.nodes[NodeId(node_id)]
        dispatch = _dispatch(
            self.request,
            node_id=node_id,
            dependencies=tuple(str(dependency) for dependency in node.dependencies),
        )
        projection = self.ledger.read_projection(
            aggregate_type="runtime_node", aggregate_id=f"{RUN}:{node_id}"
        )
        if projection is not None:
            dispatch = replace(
                dispatch,
                attempt_id=f"{node_id}-{projection.aggregate_version}",
                operation_id=f"{node_id}:{RUN}:{projection.aggregate_version}",
            )
        now = datetime.now(UTC)
        epoch = runtime.register_node(
            dispatch=WorkflowNodeDispatch(dispatch, self.request.manifest),
            now=now,
            heartbeat_window=timedelta(seconds=60),
        )
        if outcome == "awaiting_human":
            runtime.await_human_node(
                run_id=RUN,
                node_id=node_id,
                epoch=epoch.epoch,
                now=now,
                reason="operator decision required",
            )
        elif outcome is not None:
            runtime.complete_node(
                run_id=RUN, node_id=node_id, epoch=epoch.epoch, now=now, outcome=outcome
            )

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ("git", *arguments), cwd=self.request.workspace_path, text=True
        ).strip()

    def commit(self, *, text: str | None = None) -> str:
        if text is not None:
            (self.request.workspace_path / "README").write_text(text)
            self.git("add", "README")
        self.git("commit", "--allow-empty", "-m", "persist implementation evidence")
        head = self.git("rev-parse", "HEAD")
        attempt_id = str(self.state("implement")["attempt_id"])
        self.persist("node_attempt", attempt_id, {"result": {"head_revision": head}})
        return head

    def issue(self, **kwargs: Any) -> dict[str, Any]:
        return factory_decisions.issue_for_review(
            self.project,
            RUN,
            {"subject_digest": self.packet.subject_digest, "lines": ["Review"]},
            database=self.database,
            owner="stage1-cli",
            **kwargs,
        )

    def decide(self, issued: dict[str, Any], **changes: Any) -> dict[str, object]:
        arguments = {
            "action": ACTION,
            "subject_digest": issued["subject_digest"],
            "challenge": issued["challenge"],
            **changes,
        }
        return factory_decisions.decide(
            self.project, RUN, database=self.database, owner="stage1-cli", **arguments
        )


@pytest.fixture
def unimplemented_ingress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Ingress]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    preferences = tmp_path / "config" / "enginery" / "user.toml"
    preferences.parent.mkdir(parents=True)
    preferences.write_bytes(_preferences())
    database = tmp_path / "ledger.sqlite"
    original = _start_request(database, tmp_path)
    with LedgerService.open(database) as ledger:
        request = (
            Stage1RunService(runtime=CoordinatorRuntime(ledger, owner="stage1-cli"), ledger=ledger)
            .read(original.run.id)
            .request
        )
        yield Ingress(request.repository_path, ledger, request, database, preferences)


@pytest.fixture
def ingress(unimplemented_ingress: Ingress) -> Ingress:
    value = unimplemented_ingress
    value.register("qualify", outcome="passed")
    subprocess.run(
        (
            "git",
            "clone",
            "--local",
            str(value.request.repository_path),
            str(value.request.workspace_path),
        ),
        check=True,
        capture_output=True,
    )
    value.git("config", "user.name", "Test")
    value.git("config", "user.email", "test@example.invalid")
    dispatch = replace(
        _dispatch(value.request, node_id="implement", dependencies=("qualify",)),
        attempt_id="implement-2",
    )
    # The host-owned completed attempt is persisted, not substituted by a packet double.
    value.persist(
        "runtime_node",
        f"{RUN}:implement",
        _request_state(dispatch, status="passed", actor_type=ActorType.AGENT),
    )
    value.commit(text="fixture\npersist the changed result\n")
    value.register("validate", outcome="passed")
    value.register("review", outcome="passed")
    value.register("open_pr")
    return value


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Actual kernel terminal identity; no isatty/authentication production bypass."""
    master, slave = pty.openpty()
    with (
        os.fdopen(os.dup(slave), "r") as input_stream,
        os.fdopen(os.dup(slave), "w") as output_stream,
    ):
        monkeypatch.setattr(
            factory_decisions,
            "sys",
            SimpleNamespace(stdin=input_stream, stdout=output_stream),
        )
        try:
            yield
        finally:
            os.close(master)
            os.close(slave)


def test_registration_is_user_only_and_not_effective_request_behavior() -> None:
    layer = parse_layer_document(SelectionLayer.USER, _preferences(), source="user.toml")
    assert layer.review_authority is not None
    assert layer.review_authority.ttl_seconds == 1800
    assert layer.review_authority.registered_humans == (layer.review_authority.principal,)
    assert layer.values == {}
    assert layer.digest == Digest.of_bytes(_preferences())
    for selection in (SelectionLayer.PROJECT, SelectionLayer.WORKFLOW, SelectionLayer.FACTORY):
        with pytest.raises(ConfigError, match="user preferences only"):
            parse_layer_document(selection, _preferences(), source="not-user.toml")


@pytest.mark.parametrize(
    "changes",
    [
        {"principal_id": 1},
        {"principal_id": ""},
        {"principal_id": "forged\u202e"},
        {"os_user": True},
        {"os_user": " space"},
        {"os_uid": True},
        {"os_uid": -1},
        {"os_uid": "0"},
        {"ttl_seconds": True},
        {"ttl_seconds": 0},
        {"ttl_seconds": 1801},
        {"ttl_seconds": 1.5},
        {"unknown": "value"},
    ],
)
def test_registration_refuses_unknown_fields_and_wrong_types(changes: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_layer_document(SelectionLayer.USER, _preferences(**changes), source="user.toml")


@pytest.mark.parametrize(
    "document",
    [
        b'schema_version=1\nreview_authority="operator"\n',
        b'schema_version=1\n[review_authority]\nprincipal_id="operator"\n',
    ],
)
def test_registration_requires_a_complete_table(document: bytes) -> None:
    with pytest.raises(ConfigError):
        parse_layer_document(SelectionLayer.USER, document, source="user.toml")


def test_noninteractive_review_preserves_display_without_issuing_authority(
    ingress: Ingress,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory_decisions.sys, "stdin", io.StringIO())
    monkeypatch.setattr(factory_decisions.sys, "stdout", io.StringIO())
    report = ingress.issue()
    assert report["lines"][0] == "Review"
    assert report["challenge_issued"] is False and "challenge" not in report
    count = ingress.ledger.connection.execute(
        "SELECT count(*) FROM human_decision_challenges",
    ).fetchone()[0]
    assert count == 0
    with pytest.raises(AuthenticationError, match="interactive"):
        factory_decisions.decide(
            ingress.project,
            RUN,
            action=ACTION,
            subject_digest=ingress.packet.subject_digest,
            challenge="f" * 64,
            database=ingress.database,
            owner="stage1-cli",
        )


def test_real_terminal_issues_hash_only_and_consumes_once(
    ingress: Ingress,
    terminal: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = ingress.issue()
    row = ingress.ledger.connection.execute("SELECT * FROM human_decision_challenges").fetchone()
    assert row["challenge_hash"] == hashlib.sha256(issued["challenge"].encode()).hexdigest()
    assert issued["challenge"] not in repr(tuple(row))
    result = ingress.decide(issued)
    assert result["action"] == ACTION and result["outcome"] == "approved"
    assert issued["challenge"] not in repr(result)
    with pytest.raises(ReplayedChallengeError):
        ingress.decide(issued)
    decisions = list_decisions(ingress.ledger.connection, RUN)
    assert len(decisions) == 1 and decisions[0].id == result["decision_id"]
    assert decisions[0].normalized_inputs["requesting_principal_id"] == "implement-0"
    producers = decisions[0].normalized_inputs["producer_principal_ids"]
    assert producers == ("implement-0", "implement-2")
    assert "operator" not in producers
    assert issued["challenge"] not in "\n".join(ingress.ledger.connection.iterdump())
    # Durable gate deliberately runs with NO interactive terminal after ingestion.
    monkeypatch.setattr(factory_decisions.sys, "stdin", io.StringIO())
    monkeypatch.setattr(factory_decisions.sys, "stdout", io.StringIO())
    authorized = factory_decisions.require_for_effect(ingress.ledger, ingress.request, ACTION)
    assert authorized.id == result["decision_id"]
    with LedgerService.open(ingress.database) as reopened:
        assert list_decisions(reopened.connection, RUN)[0] == decisions[0]


@pytest.mark.parametrize("action", [ACTION, "human_wait.validate.reject"])
def test_decision_refusal_has_one_context_correct_quoted_repair(
    ingress: Ingress,
    terminal: None,
    action: str,
) -> None:
    if action != ACTION:
        ingress.register("validate", outcome="awaiting_human")
    issued = ingress.issue()
    with pytest.raises(SubjectMismatchError) as refused:
        ingress.decide(issued, action=action, subject_digest="sha256:" + "0" * 64)
    message = str(refused.value)
    assert message.count("repair:") == 1
    review, decide = message.split("; repair: ", 1)[1].split("; ", 1)
    for command, verb in ((review, "review"), (decide, "decide")):
        argv = shlex.split(command)
        assert argv[:3] == ["enginery", verb, RUN]
        assert argv[argv.index("--project") + 1] == str(ingress.project)
        assert argv[argv.index("--database") + 1] == str(ingress.database)
        assert argv[argv.index("--owner") + 1] == "stage1-cli"
    argv = shlex.split(decide)
    assert argv[argv.index("--action") + 1] == action
    assert ("--reason" in argv) == action.startswith("human_wait.")
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_a_durable_publication_decision_cannot_release_another_action(
    ingress: Ingress,
    terminal: None,
) -> None:
    from enginery.application.human_decisions import MissingDecisionError

    result = ingress.decide(ingress.issue())
    with pytest.raises(MissingDecisionError):
        factory_decisions.require_for_effect(
            ingress.ledger, ingress.request, "human_wait.validate.approve", "validate"
        )
    assert (
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, ACTION).id
        == result["decision_id"]
    )


@pytest.mark.parametrize(
    "registration", [{}, {"os_uid": os.getuid() + 1}, {"os_user": "other-user"}]
)
def test_unregistered_operator_still_receives_packet_without_challenge(
    ingress: Ingress,
    terminal: None,
    registration: dict[str, object],
) -> None:
    ingress.preferences.write_bytes(
        _preferences(**registration) if registration else b"schema_version = 1\n"
    )
    report = ingress.issue()
    assert report["subject_digest"] == ingress.packet.subject_digest
    assert report["challenge_issued"] is False
    assert "challenge" not in report
    assert "[review_authority]" in report["notice"]
    assert "user preferences" in report["notice"]
    with pytest.raises(PrincipalMismatchError):
        factory_decisions.decide(
            ingress.project,
            RUN,
            action=ACTION,
            subject_digest=ingress.packet.subject_digest,
            challenge="unissued",
            database=ingress.database,
            owner="stage1-cli",
        )
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_forged_principal_and_wrong_registered_os_identity_are_refused(
    ingress: Ingress,
    terminal: None,
) -> None:
    forged = ingress.issue(principal_id="forged")
    assert forged["challenge_issued"] is False
    assert forged["challenge_reason"] == "decision_principal_mismatch"
    issued = ingress.issue()
    with pytest.raises(PrincipalMismatchError):
        ingress.decide(issued, principal_id="forged")
    ingress.preferences.write_bytes(_preferences(os_uid=os.getuid() + 1))
    wrong_uid = ingress.issue()
    assert wrong_uid["challenge_issued"] is False
    assert wrong_uid["subject_digest"] == ingress.packet.subject_digest
    ingress.preferences.write_bytes(_preferences(os_user="another-local-user"))
    with pytest.raises(PrincipalMismatchError, match="OS user"):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_wrong_action_and_forged_subject_leave_challenge_unconsumed(
    ingress: Ingress,
    terminal: None,
) -> None:
    issued = ingress.issue()
    with pytest.raises(ActionMismatchError):
        ingress.decide(issued, action="release.publish")
    with pytest.raises(SubjectMismatchError):
        ingress.decide(issued, subject_digest=str(Digest.of_bytes(b"forged")))
    assert not list_decisions(ingress.ledger.connection, RUN)
    assert ingress.decide(issued)["outcome"] == "approved"


def _change_bound_input(ingress: Ingress, field: str) -> None:
    packet = ingress.packet
    if field == "head_revision":
        ingress.commit()
    elif field == "diff":
        ingress.commit(text="fixture\na different protected change\n")
    elif field == "protected_publication_digest":
        ingress.replace_request(protected_paths=("README",))
    elif field == "evidence_receipt_digests":
        raw = b'{"outcome":"new persisted evidence"}'
        digest = Digest.of_bytes(raw)
        root = ingress.request.execution_configuration.artifact_root
        path = root / "objects" / digest.algorithm / digest.hex_value[:2] / digest.hex_value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        ingress.persist(
            "behavioral_acceptance",
            RUN,
            {
                "head_revision": packet.head_revision,
                "configuration_snapshot_digest": packet.configuration_snapshot_digest,
                "receipt_digest": str(digest),
            },
        )
    elif field == "ci_evidence_json":
        ingress.update_node(
            "verify",
            head_revision=packet.head_revision,
            configuration_snapshot_digest=packet.configuration_snapshot_digest,
            github_evidence={"status": "failed"},
        )
    elif field == "findings_json":
        ingress.update_node("review", finding_ids=["new finding"])
    else:
        raise AssertionError(f"unknown bound input: {field}")


@pytest.mark.parametrize(
    "field",
    [
        "head_revision",
        "diff",
        "protected_publication_digest",
        "evidence_receipt_digests",
        "ci_evidence_json",
        "findings_json",
    ],
)
@pytest.mark.parametrize("durable", [False, True], ids=["challenge", "approval"])
def test_changed_bound_inputs_refuse_challenge_and_supersede_durable_approval(
    ingress: Ingress,
    terminal: None,
    field: str,
    durable: bool,
) -> None:
    old_packet = ingress.packet
    issued = ingress.issue()
    if durable:
        result = ingress.decide(issued)
        original = list_decisions(ingress.ledger.connection, RUN)[0]
    _change_bound_input(ingress, field)
    current = ingress.packet
    assert getattr(current, field) != getattr(old_packet, field)
    assert current.subject_digest != old_packet.subject_digest
    if not durable:
        with pytest.raises(StaleSubjectError):
            ingress.decide(issued)
        assert not list_decisions(ingress.ledger.connection, RUN)
        return
    with pytest.raises(SupersededDecisionError):
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, ACTION)
    retained = list_decisions(ingress.ledger.connection, RUN)[0]
    assert (
        retained.id == result["decision_id"] and retained.subject_digest == original.subject_digest
    )
    assert retained.normalized_inputs == original.normalized_inputs
    assert retained.superseded_by == current.subject_digest


def test_expired_challenge_cannot_record_authority(
    ingress: Ingress,
    terminal: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_issue = factory_decisions.issue_challenge

    def old_issue(*args: Any, **kwargs: Any) -> Any:
        return real_issue(*args, **kwargs, now=datetime.now(UTC) - timedelta(hours=1))

    monkeypatch.setattr(factory_decisions, "issue_challenge", old_issue)
    issued = ingress.issue()
    with pytest.raises(ExpiredChallengeError):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_current_registration_change_invalidates_effect_authority(
    ingress: Ingress,
    terminal: None,
) -> None:
    ingress.decide(ingress.issue())
    ingress.preferences.write_bytes(_preferences(principal_id="replacement"))
    with pytest.raises(PrincipalMismatchError):
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, ACTION)
    assert len(list_decisions(ingress.ledger.connection, RUN)) == 1


def test_policy_override_is_reasoned_expiring_and_exactly_node_scoped(
    ingress: Ingress,
    terminal: None,
) -> None:
    ingress.register("validate", outcome="awaiting_human")
    action = "human_wait.validate.approve"
    issued = ingress.issue()
    assert action in issued["actions"] and ACTION not in issued["actions"]
    with pytest.raises(HardRuleError):
        ingress.decide(issued, action=action)
    result = ingress.decide(issued, action=action, reason="operator investigated the wait")
    assert result["node_id"] == "validate"
    assert ingress.state("validate")["status"] == "passed"
    event = ingress.ledger.read_projection(
        aggregate_type="runtime_node",
        aggregate_id=f"{RUN}:validate",
    )
    assert event is not None and event.state["decision_id"] == result["decision_id"]
    assert factory_decisions.require_for_effect(
        ingress.ledger,
        ingress.request,
        action,
        "validate",
    ).normalized_inputs["override_scope"] == ("approve", "validate")
    with pytest.raises(ActionMismatchError):
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, action, "review")
    with pytest.raises(ActionMismatchError):
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, action)


def test_self_approval_and_hard_rule_scope_are_never_overridden(
    ingress: Ingress,
    terminal: None,
) -> None:
    ingress.preferences.write_bytes(_preferences(principal_id="implement-2"))
    issued = ingress.issue()
    with pytest.raises(HardRuleError, match="producer separation"):
        ingress.decide(issued)
    ingress.preferences.write_bytes(_preferences())
    node_id = NodeId("evidence.current_subject")
    manifest = replace(
        ingress.request.manifest,
        nodes={
            **ingress.request.manifest.nodes,
            node_id: replace(ingress.request.manifest.nodes[NodeId("review")], node_id=node_id),
        },
    )
    ingress.replace_request(
        manifest=manifest,
        run=replace(ingress.request.run, workflow_definition_digest=manifest.content_digest),
    )
    ingress.register(str(node_id), outcome="awaiting_human")
    issued = ingress.issue()
    with pytest.raises(HardRuleError, match="hard rule"):
        ingress.decide(
            issued,
            action="human_wait.evidence.current_subject.approve",
            reason="cannot weaken subject binding",
        )
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_both_streams_must_be_interactive(
    ingress: Ingress,
    terminal: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = ingress.issue()
    monkeypatch.setattr(factory_decisions.sys, "stdout", io.StringIO())
    assert ingress.issue()["challenge_issued"] is False
    with pytest.raises(AuthenticationError, match="interactive"):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_input_and_output_must_share_the_same_actual_terminal(
    ingress: Ingress,
    terminal: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, slave = pty.openpty()
    try:
        with os.fdopen(os.dup(slave), "w") as other_output:
            monkeypatch.setattr(factory_decisions.sys, "stdout", other_output)
            with pytest.raises(AuthenticationError, match="same local terminal"):
                ingress.issue()
    finally:
        os.close(master)
        os.close(slave)


def test_operator_can_only_shorten_challenge_lifetime(
    ingress: Ingress,
    terminal: None,
) -> None:
    ingress.preferences.write_bytes(_preferences(ttl_seconds=60))
    issued = ingress.issue()
    row = ingress.ledger.connection.execute(
        "SELECT issued_at, expires_at FROM human_decision_challenges",
    ).fetchone()
    assert datetime.fromisoformat(row["expires_at"]) == (
        datetime.fromisoformat(row["issued_at"]) + timedelta(seconds=60)
    )
    assert issued["expires_at"] == row["expires_at"]


def test_review_without_an_available_action_issues_no_challenge(
    ingress: Ingress,
    terminal: None,
) -> None:
    ingress.update_node("review", status="queued")
    report = ingress.issue()
    assert report["challenge_issued"] is False and "challenge" not in report
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_stale_subject_precedes_changed_action_availability(
    ingress: Ingress,
    terminal: None,
) -> None:
    issued = ingress.issue()
    ingress.update_node("review", status="failed", finding_ids=["repair first"])
    with pytest.raises(StaleSubjectError):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_subject_is_checked_again_under_the_nonce_consumption_lock(
    ingress: Ingress,
    terminal: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = ingress.issue()
    packet = ingress.packet
    real_projection = factory_review._projection
    observation_reads = 0

    def changing_observation(
        ledger: LedgerService, aggregate_type: str, aggregate_id: str
    ) -> dict[str, object]:
        nonlocal observation_reads
        value = real_projection(ledger, aggregate_type, aggregate_id)
        if aggregate_type == "behavioral_acceptance" and aggregate_id == RUN:
            observation_reads += 1
            if observation_reads == 2:
                assert ledger.connection.in_transaction
                return {
                    "head_revision": packet.head_revision,
                    "configuration_snapshot_digest": packet.configuration_snapshot_digest,
                    "receipt_digest": str(Digest.of_bytes(b"changed during nonce consumption")),
                }
        return value

    monkeypatch.setattr(factory_review, "_projection", changing_observation)
    with pytest.raises(StaleSubjectError, match="while recording"):
        ingress.decide(issued)
    assert observation_reads == 2
    assert not list_decisions(ingress.ledger.connection, RUN)
    row = ingress.ledger.connection.execute(
        "SELECT consumed_at FROM human_decision_challenges",
    ).fetchone()
    assert row["consumed_at"] is None


@pytest.mark.parametrize("node_id", ["qualify", "no_change_confirmation"])
@pytest.mark.parametrize("outcome", ["approve", "reject"])
def test_preimplementation_wait_has_real_packet_and_authenticated_node_decision(
    unimplemented_ingress: Ingress,
    terminal: None,
    node_id: str,
    outcome: str,
) -> None:
    ingress = unimplemented_ingress
    if node_id != "qualify":
        ingress.register("qualify", outcome="passed")
    ingress.register(node_id, outcome="awaiting_human")
    assert not ingress.request.workspace_path.exists()
    assert ingress.state("implement") == {}
    assert not ingress.ledger.list_projections(aggregate_type="node_attempt")
    packet = ingress.packet
    assert packet.base_revision == ingress.request.run.base_revision
    assert packet.head_revision == ""
    assert packet.diff == ""
    assert packet.preimplementation_waits == (node_id,)
    assert packet.subject_fields()["preimplementation_waits"] == [node_id]
    assert packet.request_digest == str(ingress.request.digest)
    reproduction = json.loads(dict(packet.sections)["Reproduction evidence"])
    assert reproduction["implementation_result"] == {}
    assert reproduction["preimplementation_waits"] == [node_id]

    issued = ingress.issue()
    action = f"human_wait.{node_id}.{outcome}"
    assert set(issued["actions"]) == {
        f"human_wait.{node_id}.{choice}" for choice in ("approve", "reject")
    }
    assert ACTION not in issued["actions"]
    result = ingress.decide(issued, action=action, reason="operator reviewed the source intent")
    decision = list_decisions(ingress.ledger.connection, RUN)[0]
    assert decision.id == result["decision_id"]
    assert decision.action == action
    assert decision.subject_digest == packet.subject_digest
    state = ingress.state(node_id)
    assert (
        state["status"]
        == {
            "approve": "passed",
            "reject": "failed",
        }[outcome]
    )
    assert state["decision_id"] == decision.id
    assert state["decision_action"] == action
    assert state["execution_selection_digest"] == str(ingress.request.execution_selection.digest)
    assert ingress.state("implement") == {}
    row = ingress.ledger.connection.execute(
        "SELECT consumed_at FROM human_decision_challenges"
    ).fetchone()
    assert row["consumed_at"] is not None
    with LedgerService.open(ingress.database) as reopened:
        assert list_decisions(reopened.connection, RUN) == (decision,)


def test_preimplementation_subject_binds_the_actual_waiting_node(
    unimplemented_ingress: Ingress,
    terminal: None,
) -> None:
    ingress = unimplemented_ingress
    ingress.register("qualify", outcome="passed")
    ingress.register("no_change_confirmation", outcome="awaiting_human")
    old_packet = ingress.packet
    issued = ingress.issue()
    ingress.update_node("no_change_confirmation", status="passed")
    ingress.register("plan_approval", outcome="awaiting_human")
    current = ingress.packet
    assert current.head_revision == old_packet.head_revision == ""
    assert current.diff == old_packet.diff == ""
    assert old_packet.preimplementation_waits == ("no_change_confirmation",)
    assert current.preimplementation_waits == ("plan_approval",)
    assert {
        key: value
        for key, value in old_packet.subject_fields().items()
        if key != "preimplementation_waits"
    } == {
        key: value
        for key, value in current.subject_fields().items()
        if key != "preimplementation_waits"
    }
    assert current.subject_digest != old_packet.subject_digest
    with pytest.raises(StaleSubjectError):
        ingress.decide(
            issued,
            action="human_wait.no_change_confirmation.approve",
            reason="the previous wait cannot release a different node",
        )
    assert not list_decisions(ingress.ledger.connection, RUN)
    row = ingress.ledger.connection.execute(
        "SELECT consumed_at FROM human_decision_challenges"
    ).fetchone()
    assert row["consumed_at"] is None


def test_missing_implementation_head_without_an_early_wait_is_not_a_review_packet(
    unimplemented_ingress: Ingress,
) -> None:
    from enginery.domain.errors import MissingPrerequisiteError

    with pytest.raises(MissingPrerequisiteError, match="host-verified implementation head"):
        unimplemented_ingress.issue()
    assert not list_decisions(unimplemented_ingress.ledger.connection, RUN)


@pytest.mark.parametrize(
    "node_id",
    [
        "qualify",
        "plan_approval",
        "no_change_confirmation",
        "implement",
        "validate",
        "review",
        "repair",
        "open_pr",
        "wait_for_ci",
        "verify",
    ],
)
def test_every_stage1_requeue_is_refused_without_changing_the_wait(
    ingress: Ingress,
    terminal: None,
    node_id: str,
) -> None:
    runtime = CoordinatorRuntime(ingress.ledger, owner="stage1-cli")
    now = datetime.now(UTC)
    epoch = runtime.claim_epoch(now=now, heartbeat_window=timedelta(seconds=60))
    if node_id in {"wait_for_ci", "verify"}:
        runtime.complete_node(
            run_id=RUN, node_id="open_pr", epoch=epoch.epoch, now=now, outcome="passed"
        )
        if node_id == "verify":
            ingress.register("wait_for_ci", outcome="passed")
    if node_id == "implement":
        ingress.update_node(node_id, status="awaiting_human")
    elif node_id == "open_pr":
        runtime.await_human_node(
            run_id=RUN,
            node_id=node_id,
            epoch=epoch.epoch,
            now=now,
            reason="operator decision required",
        )
    else:
        ingress.register(node_id, outcome="awaiting_human")
    before = ingress.state(node_id)
    issued = ingress.issue()
    assert not any(action.endswith(".requeue") for action in issued["actions"])
    with pytest.raises(ActionMismatchError) as refused:
        ingress.decide(issued, action=f"human_wait.{node_id}.requeue", reason="retry")
    assert refused.value.code == "decision_action_mismatch"
    assert refused.value.details["reason"] == "human_wait_requeue_unsupported"
    assert ingress.state(node_id) == before
    assert not list_decisions(ingress.ledger.connection, RUN)
    row = ingress.ledger.connection.execute(
        "SELECT consumed_at FROM human_decision_challenges"
    ).fetchone()
    assert row["consumed_at"] is None
