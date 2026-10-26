"""Read-only integrity check of the artifact bytes a durable run's ledger records reference."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from enginery.domain.digests import Digest
from enginery.domain.errors import MissingPrerequisiteError, ValidationFailureError
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.errors import ArtifactDigestMismatchError, ArtifactMissingError
from enginery.ledger.service import LedgerService

STATUSES = ("intact", "missing", "tampered")
_DIGEST_KEY_SUFFIX = "artifact_digest"
_REFERENCES_KEY = "artifact_references"


@dataclass(frozen=True, slots=True)
class ArtifactCheck:
    artifact_id: str
    digest: str
    status: str


def verify_run_artifacts(
    ledger: LedgerService, *, run_id: str, artifact_root: Path
) -> dict[str, object]:
    """Re-hash every artifact the run's records reference; never writes anything."""
    if not artifact_root.is_dir():
        raise MissingPrerequisiteError(
            "the run's artifact store root does not exist",
            details={"artifact_root": str(artifact_root)},
        )
    store = ArtifactStore(artifact_root, create_directories=False)
    checks = [
        ArtifactCheck(artifact_id, digest, _status(store, digest))
        for artifact_id, digest in sorted(_references(ledger, run_id))
    ]
    counts = Counter(check.status for check in checks)
    return {
        "artifact_root": str(store.root),
        "artifacts": [
            {"artifact_id": check.artifact_id, "digest": check.digest, "status": check.status}
            for check in checks
        ],
        "summary": {status: counts.get(status, 0) for status in STATUSES},
    }


def all_intact(verification: dict[str, object]) -> bool:
    summary = verification["summary"]
    assert isinstance(summary, dict)
    return bool(summary["missing"] == 0 and summary["tampered"] == 0)


def _status(store: ArtifactStore, digest_text: str) -> str:
    algorithm, _, hex_value = digest_text.partition(":")
    try:
        digest = Digest(algorithm=algorithm, hex_value=hex_value)
    except Exception as error:
        raise ValidationFailureError(
            "a run record references a malformed artifact digest",
            details={"digest": digest_text},
        ) from error
    try:
        store.read_bytes(digest)
    except ArtifactMissingError:
        return "missing"
    except ArtifactDigestMismatchError:
        return "tampered"
    return "intact"


def _references(ledger: LedgerService, run_id: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    rows = ledger.connection.execute(
        "SELECT artifact_id, digest FROM artifacts WHERE run_id = ?", (run_id,)
    ).fetchall()
    found.update((str(row["artifact_id"]), str(row["digest"])) for row in rows)
    prefix = f"{run_id}:"
    for projection in ledger.list_projections(aggregate_type=RUNTIME_NODE_AGGREGATE_TYPE):
        if not projection.aggregate_id.startswith(prefix):
            continue
        node_id = projection.aggregate_id.removeprefix(prefix)
        for key, value in projection.state.items():
            if key == _REFERENCES_KEY and isinstance(value, list | tuple):
                found.update(
                    (f"{node_id}:{key}[{index}]", str(item))
                    for index, item in enumerate(value)
                    if isinstance(item, str)
                )
            elif key.endswith(_DIGEST_KEY_SUFFIX) and isinstance(value, str):
                found.add((f"{node_id}:{key}", value))
    return found
