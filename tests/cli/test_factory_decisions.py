"""Ingress tests use real PTYs; fake packets isolate composition, not authentication."""

from __future__ import annotations

import getpass
import hashlib
import io
import json
import os
import pty
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
from enginery.cli import factory_decisions
from enginery.domain.digests import Digest
from enginery.domain.execution_selection import SelectionLayer
from enginery.ledger.human_decisions import list_decisions
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.policy.rules import HardRuleError
from enginery.workflows.stage1 import Stage1RunRequest

ACTION = "pull_request.open"
RUN = "run-decisions"


def _preferences(**changes: object) -> bytes:
    values: dict[str, object] = {
        "principal_id": "operator", "os_user": getpass.getuser(), "os_uid": os.getuid(),
    }
    values.update(changes)
    lines = ["schema_version = 1", "[review_authority]"]
    lines.extend(f"{key} = {json.dumps(value)}" for key, value in values.items())
    return ("\n".join(lines) + "\n").encode()


def _packet() -> ReviewPacket:
    return ReviewPacket(
        run_id=RUN, request_digest=str(Digest.of_bytes(b"request")),
        configuration_snapshot_digest=str(Digest.of_bytes(b"config")),
        base_revision="a" * 40, head_revision="b" * 40, diff="+persist the changed result",
        evidence_receipt_digests=(str(Digest.of_bytes(b"receipt")),),
        ci_evidence_json='{"head":"bbbb","status":"passed"}', findings_json="[]",
        sections=(("Intent", '{"objective":"persist the changed result"}'),),
    )


@dataclass
class Ingress:
    project: Path
    ledger: LedgerService
    request: Stage1RunRequest
    packet: ReviewPacket
    preferences: Path
    states: dict[str, dict[str, object]]

    def issue(self, **kwargs: Any) -> dict[str, Any]:
        return factory_decisions.issue_for_review(
            self.project, RUN, {"subject_digest": self.packet.subject_digest, "lines": ["Review"]},
            **kwargs,
        )

    def decide(self, issued: dict[str, Any], **changes: Any) -> dict[str, object]:
        arguments = {
            "action": ACTION, "subject_digest": issued["subject_digest"],
            "challenge": issued["challenge"], **changes,
        }
        return factory_decisions.decide(self.project, RUN, **arguments)


@pytest.fixture
def ingress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Ingress]:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    preferences = tmp_path / "config" / "enginery" / "user.toml"
    preferences.parent.mkdir(parents=True)
    preferences.write_bytes(_preferences())
    ledger = LedgerService.open(tmp_path / "ledger.sqlite")
    request = cast(Stage1RunRequest, SimpleNamespace(
        run=SimpleNamespace(id=RUN, configuration_snapshot_digest=Digest.of_bytes(b"config")),
        repository_path=tmp_path, repository_id="owner/repository",
        execution_selection=SimpleNamespace(legacy=True),
        implementation=SimpleNamespace(attempt_id="implement-1"),
        manifest=SimpleNamespace(
            nodes={"implement": None, "review": None, "open_pr": None, "validate": None},
            content_digest=Digest.of_bytes(b"manifest"),
        ),
        work_snapshot=SimpleNamespace(bound_digest=Digest.of_bytes(b"source")),
    ))
    value = Ingress(tmp_path, ledger, request, _packet(), preferences, {
        "implement": {"status": "passed", "attempt_id": "implement-2"},
        "review": {"status": "passed"}, "open_pr": {"status": "queued"},
    })
    monkeypatch.setattr(factory_decisions, "build_packet", lambda *args: value.packet)
    monkeypatch.setattr(factory_decisions, "_state", lambda ledger, run, node: value.states.get(node, {}))
    # Keep one real migrated connection open across simulated CLI invocations.
    monkeypatch.setattr(factory_decisions, "_open", lambda *args, **kwargs: (ledger, request))
    monkeypatch.setattr(ledger, "close", lambda: None)
    try:
        yield value
    finally:
        ledger.connection.close()


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Actual kernel terminal identity; no isatty/authentication production bypass."""
    master, slave = pty.openpty()
    with (
        os.fdopen(os.dup(slave), "r") as input_stream,
        os.fdopen(os.dup(slave), "w") as output_stream,
    ):
        monkeypatch.setattr(
            factory_decisions, "sys", SimpleNamespace(stdin=input_stream, stdout=output_stream),
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


@pytest.mark.parametrize("changes", [
    {"principal_id": 1}, {"principal_id": ""}, {"principal_id": "forged\u202e"},
    {"os_user": True}, {"os_user": " space"}, {"os_uid": True}, {"os_uid": -1},
    {"os_uid": "0"}, {"ttl_seconds": True}, {"ttl_seconds": 0}, {"ttl_seconds": 1801},
    {"ttl_seconds": 1.5}, {"unknown": "value"},
])
def test_registration_refuses_unknown_fields_and_wrong_types(changes: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_layer_document(SelectionLayer.USER, _preferences(**changes), source="user.toml")


@pytest.mark.parametrize("document", [
    b'schema_version=1\nreview_authority="operator"\n',
    b'schema_version=1\n[review_authority]\nprincipal_id="operator"\n',
])
def test_registration_requires_a_complete_table(document: bytes) -> None:
    with pytest.raises(ConfigError):
        parse_layer_document(SelectionLayer.USER, document, source="user.toml")


def test_noninteractive_review_preserves_display_without_issuing_authority(
    ingress: Ingress, monkeypatch: pytest.MonkeyPatch,
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
            ingress.project, RUN, action=ACTION,
            subject_digest=ingress.packet.subject_digest, challenge="f" * 64,
        )


def test_real_terminal_issues_hash_only_and_consumes_once(
    ingress: Ingress, terminal: None, monkeypatch: pytest.MonkeyPatch,
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
    assert decisions[0].normalized_inputs["requesting_principal_id"] == "implement-1"
    producers = decisions[0].normalized_inputs["producer_principal_ids"]
    assert producers == ("implement-1", "implement-2")
    assert "operator" not in producers
    assert issued["challenge"] not in "\n".join(ingress.ledger.connection.iterdump())
    # Durable gate deliberately runs with NO interactive terminal after ingestion.
    monkeypatch.setattr(factory_decisions.sys, "stdin", io.StringIO())
    monkeypatch.setattr(factory_decisions.sys, "stdout", io.StringIO())
    authorized = factory_decisions.require_for_effect(ingress.ledger, ingress.request, ACTION)
    assert authorized.id == result["decision_id"]
    with LedgerService.open(ingress.project / "ledger.sqlite") as reopened:
        assert list_decisions(reopened.connection, RUN)[0] == decisions[0]


def test_forged_principal_and_wrong_registered_os_identity_are_refused(
    ingress: Ingress, terminal: None,
) -> None:
    with pytest.raises(PrincipalMismatchError):
        ingress.issue(principal_id="forged")
    issued = ingress.issue()
    with pytest.raises(PrincipalMismatchError):
        ingress.decide(issued, principal_id="forged")
    ingress.preferences.write_bytes(_preferences(os_uid=os.getuid() + 1))
    with pytest.raises(PrincipalMismatchError, match="OS user"):
        ingress.issue()
    ingress.preferences.write_bytes(_preferences(os_user="another-local-user"))
    with pytest.raises(PrincipalMismatchError, match="OS user"):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_wrong_action_and_forged_subject_leave_challenge_unconsumed(
    ingress: Ingress, terminal: None,
) -> None:
    issued = ingress.issue()
    with pytest.raises(ActionMismatchError):
        ingress.decide(issued, action="release.publish")
    with pytest.raises(SubjectMismatchError):
        ingress.decide(issued, subject_digest=str(Digest.of_bytes(b"forged")))
    assert not list_decisions(ingress.ledger.connection, RUN)
    assert ingress.decide(issued)["outcome"] == "approved"


@pytest.mark.parametrize("change", [
    {"head_revision": "c" * 40},
    {"diff": "+different protected change"},
    {"protected_publication_digest": str(Digest.of_bytes(b"new protected matched set"))},
    {"evidence_receipt_digests": (str(Digest.of_bytes(b"new evidence")),)},
    {"ci_evidence_json": '{"status":"failed"}'},
    {"findings_json": '["new finding"]'},
])
def test_changed_bound_inputs_refuse_challenge_and_supersede_durable_approval(
    ingress: Ingress, terminal: None, change: dict[str, Any],
) -> None:
    old_packet = ingress.packet
    issued = ingress.issue()
    ingress.packet = replace(old_packet, **change)
    with pytest.raises(StaleSubjectError):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)
    ingress.packet = old_packet
    current = ingress.issue()
    result = ingress.decide(current)
    original = list_decisions(ingress.ledger.connection, RUN)[0]
    ingress.packet = replace(old_packet, **change)
    with pytest.raises(SupersededDecisionError):
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, ACTION)
    retained = list_decisions(ingress.ledger.connection, RUN)[0]
    assert retained.id == result["decision_id"] and retained.subject_digest == original.subject_digest
    assert retained.normalized_inputs == original.normalized_inputs
    assert retained.superseded_by == ingress.packet.subject_digest


def test_expired_challenge_cannot_record_authority(
    ingress: Ingress, terminal: None, monkeypatch: pytest.MonkeyPatch,
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
    ingress: Ingress, terminal: None,
) -> None:
    ingress.decide(ingress.issue())
    ingress.preferences.write_bytes(_preferences(principal_id="replacement"))
    with pytest.raises(PrincipalMismatchError):
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, ACTION)
    assert len(list_decisions(ingress.ledger.connection, RUN)) == 1


def test_policy_override_is_reasoned_expiring_and_exactly_node_scoped(
    ingress: Ingress, terminal: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enginery.cli import stage1_composition

    ingress.states["validate"] = {"status": "awaiting_human"}
    action = "human_wait.validate.approve"
    issued = ingress.issue()
    assert action in issued["actions"] and ACTION not in issued["actions"]
    with pytest.raises(HardRuleError):
        ingress.decide(issued, action=action)

    class Runtime:
        def claim_epoch(self, **kwargs: Any) -> Any:
            assert ingress.ledger.connection.in_transaction
            return SimpleNamespace(epoch=1)

        def resolve_human_wait(self, **kwargs: Any) -> None:
            assert ingress.ledger.connection.in_transaction
            ingress.ledger.append(AppendCommand(
                correlation_id="resolve-wait",
                events=(EventWrite(
                    aggregate_type="runtime_node", aggregate_id=f"{RUN}:validate",
                    expected_version=0, event_type="runtime_node.human_wait_resolved",
                    schema_version=1, payload=kwargs["extra"],
                ),),
            ))
            ingress.states["validate"]["status"] = kwargs["outcome"]

    monkeypatch.setattr(stage1_composition, "runtime_for_run", lambda *args, **kwargs: Runtime())
    result = ingress.decide(issued, action=action, reason="operator investigated the wait")
    assert result["node_id"] == "validate"
    assert ingress.states["validate"]["status"] == "passed"
    event = ingress.ledger.read_projection(
        aggregate_type="runtime_node", aggregate_id=f"{RUN}:validate",
    )
    assert event is not None and event.state["decision_id"] == result["decision_id"]
    assert factory_decisions.require_for_effect(
        ingress.ledger, ingress.request, action, "validate",
    ).normalized_inputs["override_scope"] == ("approve", "validate")
    with pytest.raises(ActionMismatchError):
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, action, "review")
    with pytest.raises(ActionMismatchError):
        factory_decisions.require_for_effect(ingress.ledger, ingress.request, action)


def test_self_approval_and_hard_rule_scope_are_never_overridden(
    ingress: Ingress, terminal: None,
) -> None:
    ingress.preferences.write_bytes(_preferences(principal_id="implement-2"))
    issued = ingress.issue()
    with pytest.raises(HardRuleError, match="producer separation"):
        ingress.decide(issued)
    ingress.preferences.write_bytes(_preferences())
    nodes = cast(dict[str, object], ingress.request.manifest.nodes)
    nodes["evidence.current_subject"] = None
    ingress.states["evidence.current_subject"] = {"status": "awaiting_human"}
    issued = ingress.issue()
    with pytest.raises(HardRuleError, match="hard rule"):
        ingress.decide(
            issued, action="human_wait.evidence.current_subject.approve",
            reason="cannot weaken subject binding",
        )
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_both_streams_must_be_interactive(
    ingress: Ingress, terminal: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = ingress.issue()
    monkeypatch.setattr(factory_decisions.sys, "stdout", io.StringIO())
    assert ingress.issue()["challenge_issued"] is False
    with pytest.raises(AuthenticationError, match="interactive"):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_input_and_output_must_share_the_same_actual_terminal(
    ingress: Ingress, terminal: None, monkeypatch: pytest.MonkeyPatch,
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
    ingress: Ingress, terminal: None,
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
    ingress: Ingress, terminal: None,
) -> None:
    ingress.states["review"] = {"status": "queued"}
    report = ingress.issue()
    assert report["challenge_issued"] is False and "challenge" not in report
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_stale_subject_precedes_changed_action_availability(
    ingress: Ingress, terminal: None,
) -> None:
    issued = ingress.issue()
    ingress.packet = replace(ingress.packet, findings_json='["repair first"]')
    ingress.states["review"] = {"status": "failed"}
    with pytest.raises(StaleSubjectError):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_subject_is_checked_again_under_the_nonce_consumption_lock(
    ingress: Ingress, terminal: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = ingress.issue()
    builds = 0

    def changing_packet(*args: object) -> ReviewPacket:
        nonlocal builds
        builds += 1
        if builds == 2:
            ingress.packet = replace(ingress.packet, head_revision="c" * 40)
        return ingress.packet

    monkeypatch.setattr(factory_decisions, "build_packet", changing_packet)
    with pytest.raises(StaleSubjectError, match="while recording"):
        ingress.decide(issued)
    assert not list_decisions(ingress.ledger.connection, RUN)
    row = ingress.ledger.connection.execute(
        "SELECT consumed_at FROM human_decision_challenges",
    ).fetchone()
    assert row["consumed_at"] is None
