"""Consumer CLI coverage for unsupported actions and exhausted review."""

from __future__ import annotations

import pytest

from enginery.cli.main import main
from enginery.ledger.human_decisions import list_decisions
from tests.cli.test_factory_decisions import (  # noqa: F401
    RUN,
    Ingress,
    ingress,
    terminal,
    unimplemented_ingress,
)


def scope(value: Ingress) -> list[str]:
    return [
        "--project",
        str(value.project),
        "--database",
        str(value.database),
        "--owner",
        "stage1-cli",
    ]


@pytest.mark.parametrize("command", ["factory", "stage1"])
@pytest.mark.parametrize("registered", [True, False])
def test_requeue_cli_refuses_before_authentication(
    ingress: Ingress,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    command: str,
    registered: bool,
) -> None:
    ingress.register("validate", outcome="awaiting_human")
    if not registered:
        ingress.preferences.write_bytes(b"schema_version = 1\n")
    before = ingress.state("validate")
    args = (
        ["factory", "decide", RUN, *scope(ingress), "--action", "human_wait.validate.requeue"]
        if command == "factory"
        else [
            "stage1",
            "resume",
            "--database",
            str(ingress.database),
            "--owner",
            "stage1-cli",
            "--run-id",
            RUN,
            "--node-id",
            "validate",
            "--attempt-id",
            "validate-1",
            "--operation-id",
            "operation-1",
        ]
    )
    code = main(
        [
            *args,
            "--subject-digest",
            ingress.packet.subject_digest,
            "--challenge",
            "0" * 64,
            "--reason",
            "retry",
        ]
    )
    output = capsys.readouterr()
    assert code == 2
    assert "human_wait_requeue_unsupported" in output.err
    repair = output.err.split("repair:", 1)[1]
    assert "factory review" in repair and "factory decide" not in repair
    assert ingress.state("validate") == before
    assert not list_decisions(ingress.ledger.connection, RUN)


def test_mismatched_action_cli_only_suggests_review(
    ingress: Ingress,  # noqa: F811
    terminal,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    issued = ingress.issue()
    code = main(
        [
            "factory",
            "decide",
            RUN,
            *scope(ingress),
            "--action",
            "human_wait.validate.approve",
            "--subject-digest",
            issued["subject_digest"],
            "--challenge",
            issued["challenge"],
            "--reason",
            "approve",
        ]
    )
    output = capsys.readouterr()
    assert code == 2 and "decision_action_mismatch" in output.err
    repair = output.err.split("repair:", 1)[1]
    assert "factory review" in repair and "factory decide" not in repair


def test_exhausted_review_cli_has_no_actions_or_challenge(
    ingress: Ingress,  # noqa: F811
    terminal,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    ingress.replace_request(repair_limit=0)
    attempt = ingress.state("implement")["attempt_id"]
    ingress.update_node("open_pr", status="passed")
    ingress.register("wait_for_ci", outcome="passed")
    ingress.register("verify", outcome="failed")
    ingress.update_node("verify", attempt_id=attempt, behavioral_findings=["assertion failed"])
    assert main(["factory", "review", RUN, *scope(ingress)]) == 0
    output = capsys.readouterr()
    assert "Subject digest:" in output.out
    assert "Decision actions:" not in output.out
    assert "Decision challenge:" not in output.out
    assert not list_decisions(ingress.ledger.connection, RUN)
    assert (
        main(
            [
                "factory",
                "decide",
                RUN,
                *scope(ingress),
                "--action",
                "pull_request.open",
                "--subject-digest",
                ingress.packet.subject_digest,
                "--challenge",
                "0" * 64,
            ]
        )
        == 2
    )
    refusal = capsys.readouterr().err
    assert "decision_challenge_unknown" in refusal
    repair = refusal.split("repair:", 1)[1]
    assert "factory review" in repair and "factory decide" not in repair


@pytest.mark.parametrize("node", ["wait_for_ci", "verify"])
@pytest.mark.parametrize("reason", ["github_pull_request_merged", "github_evidence_blocked"])
def test_terminal_github_review_offers_no_authority(
    ingress: Ingress,  # noqa: F811
    terminal,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    node: str,
    reason: str,
) -> None:
    ingress.replace_request(repair_limit=0)
    ingress.update_node("open_pr", status="passed")
    if node == "verify":
        ingress.register("wait_for_ci", outcome="passed")
    ingress.register(node, outcome="failed")
    ingress.update_node(node, attempt_id=ingress.state("implement")["attempt_id"], reason=reason)
    assert main(["factory", "review", RUN, *scope(ingress)]) == 0
    output = capsys.readouterr()
    assert "Subject digest:" in output.out
    assert "Decision actions:" not in output.out
    assert "Decision challenge:" not in output.out
    assert (
        main(
            [
                "factory",
                "decide",
                RUN,
                *scope(ingress),
                "--action",
                "pull_request.open",
                "--subject-digest",
                ingress.packet.subject_digest,
                "--challenge",
                "0" * 64,
            ]
        )
        == 2
    )
    repair = capsys.readouterr().err.split("repair:", 1)[1]
    assert "factory review" in repair and "factory decide" not in repair
    assert not list_decisions(ingress.ledger.connection, RUN)
