from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enginery.cli.factory_review import DIFF_FLAGS, read_packet
from enginery.domain.workflow.node import ActorType
from enginery.engine.runtime import CoordinatorRuntime, _request_state
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.errors import RawCredentialDetectedError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from tests.cli.test_engine_runs import _dispatch, _request


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(("git", *arguments), cwd=repository, text=True).strip()


def packet_fixture(root: Path, assignment: str) -> tuple[Path, str, str, str, str]:
    """Persist disposable run evidence without dispatching a worker or granting authority."""
    request = _request(root)
    repository = request.repository_path
    source = repository / "candidate.py"
    source.write_text(f"{assignment}\nresult = 'before'\n", encoding="utf-8")
    _git(repository, "add", "candidate.py")
    _git(repository, "commit", "-m", "retain unchanged assignment")
    base = _git(repository, "rev-parse", "HEAD")
    source.write_text(f"{assignment}\nresult = 'after'\n", encoding="utf-8")
    _git(repository, "commit", "-am", "change adjacent behavior")
    head = _git(repository, "rev-parse", "HEAD")
    request = replace(
        request, run=replace(request.run, base_revision=base), workspace_path=repository
    )
    run_id = str(request.run.id)
    database = root / "ledger.sqlite"
    receipt = ArtifactStore(request.execution_configuration.artifact_root).publish_bytes(
        b'{"category":"worker_claim","result":"observed"}', media_type="application/json"
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with LedgerService.open(database) as ledger:
        runtime = CoordinatorRuntime(ledger, owner="factory")
        runtime.register_run(
            run_id=run_id,
            initial_state=request.initial_state(),
            now=now,
            heartbeat_window=timedelta(seconds=60),
        )
        implementation = _request_state(
            _dispatch(request, node_id="implement", dependencies=("qualify",)),
            status="passed",
            actor_type=ActorType.AGENT,
        )
        for kind, identity, state in (
            ("runtime_node", f"{run_id}:implement", implementation),
            (
                "node_attempt",
                request.implementation.attempt_id,
                {"result": {"head_revision": head}},
            ),
            (
                "behavioral_acceptance",
                run_id,
                {
                    "head_revision": head,
                    "configuration_snapshot_digest": str(request.run.configuration_snapshot_digest),
                    "receipt_digest": str(receipt),
                },
            ),
        ):
            ledger.append(
                AppendCommand(
                    correlation_id=f"fixture:{kind}:{identity}",
                    events=(
                        EventWrite(
                            aggregate_type=kind,
                            aggregate_id=identity,
                            expected_version=0,
                            event_type=f"{kind}.recorded",
                            schema_version=1,
                            payload=state,
                        ),
                    ),
                )
            )
    return database, run_id, base, head, str(receipt)


def _snapshot(database: Path) -> tuple[str, ...]:
    with LedgerService.open(database) as ledger:
        return tuple(ledger.connection.iterdump())


def test_real_packet_preserves_unchanged_call_context_and_subject_bindings(tmp_path: Path) -> None:
    assignment = "token = check_token_file('fixture')"
    database, run_id, base, head, receipt = packet_fixture(tmp_path, assignment)
    before = _snapshot(database)
    packet = read_packet(tmp_path / "repository", run_id, database=database)
    assert packet.base_revision == base
    assert packet.head_revision == head
    assert packet.diff == subprocess.check_output(
        ("git", "diff", *DIFF_FLAGS, base, head, "--"), cwd=tmp_path / "repository", text=True
    )
    assert f" {assignment}" in packet.diff
    assert "-result = 'before'" in packet.diff and "+result = 'after'" in packet.diff
    assert packet.evidence_receipt_digests == (receipt,)
    observed = json.loads(dict(packet.sections)["Observed result"])
    assert observed["receipts"][0]["availability"] == "digest_verified"
    assert _snapshot(database) == before  # No approval, challenge, or lifecycle mutation.


@pytest.mark.parametrize(
    "assignment",
    [
        "token = abcdefghijklmnopqrstuvwx",
        "token = 'abcdefghijklmnopqrstuvwx'",
        'token = "abcdefghijklmnopqrstuvwx"',
        "token = 'check_token_file'('fixture')",
        "token = check_token_file('fixture'); password = abcdefghijklmnopqrstuvwx",
        "token = ghp_abcdefghijklmnopqrstuvwxyz0123456789('fixture')",
    ],
)
def test_real_packet_refuses_credential_counterparts(tmp_path: Path, assignment: str) -> None:
    database, run_id, _, _, _ = packet_fixture(tmp_path, assignment)
    before = _snapshot(database)
    with pytest.raises(RawCredentialDetectedError) as refusal:
        read_packet(tmp_path / "repository", run_id, database=database)
    assert "abcdefghijklmnopqrstuvwx" not in str(refusal.value)
    assert "check_token_file" not in str(refusal.value)
    assert _snapshot(database) == before
