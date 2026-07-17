from __future__ import annotations

from pathlib import Path

import pytest

from enginery.domain.artifact import ArtifactKind, RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId
from enginery.ledger.artifacts import ArtifactMetadataWrite
from enginery.ledger.errors import ArtifactMissingError, ExpectedVersionConflictError
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService


@pytest.fixture
def ledger_with_store(tmp_path: Path) -> LedgerService:
    return LedgerService.open(tmp_path / "ledger.db", artifact_store_root=tmp_path / "artifacts")


def _run_created_event(**overrides: object) -> EventWrite:
    defaults: dict[str, object] = {
        "aggregate_type": "run",
        "aggregate_id": "run-1",
        "expected_version": 0,
        "event_type": "run.created",
        "schema_version": 1,
        "payload": {},
    }
    defaults.update(overrides)
    return EventWrite(**defaults)  # type: ignore[arg-type]


def _artifact_write(
    *, digest: Digest, byte_size: int, artifact_id: str = "art-1"
) -> ArtifactMetadataWrite:
    return ArtifactMetadataWrite(
        artifact_id=ArtifactId(artifact_id),
        digest=digest,
        byte_size=byte_size,
        media_type="text/plain",
        kind=ArtifactKind.LOG,
        run_id=RunId("run-1"),
        node_id=NodeId("normalize"),
        attempt_id=NodeAttemptId("attempt-1"),
        redaction=RedactionClassification.INTERNAL,
    )


def test_append_with_artifact_reference_records_metadata(
    ledger_with_store: LedgerService,
) -> None:
    digest = ledger_with_store.publish_artifact_bytes(b"log output", media_type="text/plain")
    result = ledger_with_store.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_run_created_event(),),
            artifact_references=(_artifact_write(digest=digest, byte_size=len(b"log output")),),
        )
    )
    assert result.artifact_ids == ("art-1",)

    metadata = ledger_with_store.read_artifact_metadata("art-1")
    assert metadata is not None
    assert metadata.digest == str(digest)
    assert metadata.kind == "log"
    assert metadata.redaction == "internal"


def test_artifact_reference_to_unpublished_digest_rolls_back_the_command(
    ledger_with_store: LedgerService,
) -> None:
    fabricated = Digest.of_bytes(b"never actually published")
    with pytest.raises(ArtifactMissingError):
        ledger_with_store.append(
            AppendCommand(
                correlation_id="cmd-1",
                events=(_run_created_event(),),
                artifact_references=(_artifact_write(digest=fabricated, byte_size=10),),
            )
        )
    run_row = ledger_with_store.connection.execute(
        "SELECT COUNT(*) AS n FROM aggregates "
        "WHERE aggregate_type = 'run' AND aggregate_id = 'run-1'"
    ).fetchone()
    assert run_row["n"] == 0
    assert ledger_with_store.read_artifact_metadata("art-1") is None


def test_artifact_references_without_a_store_raise(tmp_path: Path) -> None:
    ledger_without_store = LedgerService.open(tmp_path / "ledger.db")
    try:
        digest = Digest.of_bytes(b"whatever")
        with pytest.raises(InvalidInputError):
            ledger_without_store.append(
                AppendCommand(
                    correlation_id="cmd-1",
                    events=(_run_created_event(),),
                    artifact_references=(_artifact_write(digest=digest, byte_size=8),),
                )
            )
    finally:
        ledger_without_store.close()


def test_reading_unknown_artifact_metadata_returns_none(ledger_with_store: LedgerService) -> None:
    assert ledger_with_store.read_artifact_metadata("does-not-exist") is None


def test_corrupted_published_bytes_block_metadata_recording(
    ledger_with_store: LedgerService,
) -> None:
    digest = ledger_with_store.publish_artifact_bytes(b"trustworthy", media_type="text/plain")
    store = ledger_with_store.artifact_store
    assert store is not None
    store.path_for(digest).write_bytes(b"tampered")

    with pytest.raises(ArtifactMissingError):
        ledger_with_store.append(
            AppendCommand(
                correlation_id="cmd-1",
                events=(_run_created_event(),),
                artifact_references=(
                    _artifact_write(digest=digest, byte_size=len(b"trustworthy")),
                ),
            )
        )


def test_multiple_artifact_references_in_one_command(ledger_with_store: LedgerService) -> None:
    first_digest = ledger_with_store.publish_artifact_bytes(b"first", media_type="text/plain")
    second_digest = ledger_with_store.publish_artifact_bytes(b"second", media_type="text/plain")

    result = ledger_with_store.append(
        AppendCommand(
            correlation_id="cmd-1",
            events=(_run_created_event(),),
            artifact_references=(
                _artifact_write(digest=first_digest, byte_size=5, artifact_id="art-1"),
                _artifact_write(digest=second_digest, byte_size=6, artifact_id="art-2"),
            ),
        )
    )
    assert set(result.artifact_ids) == {"art-1", "art-2"}


def test_artifact_reference_rolls_back_with_an_event_conflict(
    ledger_with_store: LedgerService,
) -> None:
    ledger_with_store.append(AppendCommand(correlation_id="setup", events=(_run_created_event(),)))
    digest = ledger_with_store.publish_artifact_bytes(b"log", media_type="text/plain")

    with pytest.raises(ExpectedVersionConflictError):
        ledger_with_store.append(
            AppendCommand(
                correlation_id="cmd-2",
                events=(_run_created_event(expected_version=0, event_type="run.queued"),),
                artifact_references=(_artifact_write(digest=digest, byte_size=3),),
            )
        )
    assert ledger_with_store.read_artifact_metadata("art-1") is None
