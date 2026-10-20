"""Read installed product evidence without claiming an epoch or changing its ledger.

The command-line entry point runs by absolute path under the bound interpreter's ``-I``
mode. Host callers may import the same reader and human-decision predicate.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def valid_human_decision(decision: Mapping[str, Any], run_id: str, head: str) -> bool:
    """Require the durable authenticated terminal receipt for this exact run and head."""
    fields = decision.get("subject_fields")
    tty = decision.get("tty_name")
    uid = decision.get("os_uid")
    return bool(
        head
        and decision.get("run_id") == run_id
        and decision.get("principal_type") == "human"
        and decision.get("principal_id")
        and isinstance(tty, str)
        and tty.startswith("/dev/")
        and len(tty) > len("/dev/")
        and isinstance(uid, int)
        and not isinstance(uid, bool)
        and uid >= 0
        and decision.get("decided_at")
        and decision.get("superseded_by") is None
        and decision.get("action")
        and isinstance(fields, Mapping)
        and fields.get("head_revision", fields.get("head_sha")) == head
    )


def project_state(
    database: Path | str, run_id: str, artifacts: Path | str | None = None
) -> dict[str, Any]:
    """Return one read-only projection/decision contract for protocol and bootstrap."""
    from enginery.domain.immutable import thaw_json_value
    from enginery.ledger.artifact_store import ArtifactStore
    from enginery.ledger.human_decisions import list_decisions
    from enginery.ledger.service import LedgerService

    connection = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    ledger = LedgerService(connection)
    try:
        attempts = [
            thaw_json_value(row.state)
            for row in ledger.list_projections(aggregate_type="node_attempt")
            if row.state.get("run_id") == run_id
        ]
        nodes = [
            dict(thaw_json_value(row.state), node_id=row.aggregate_id.removeprefix(run_id + ":"))
            for row in ledger.list_projections(aggregate_type="runtime_node")
            if row.aggregate_id.startswith(run_id + ":")
        ]
        supervisors = [
            thaw_json_value(row.state)
            for row in ledger.list_process_manager_states(process_manager_name="worker-supervisor")
            if row.state.get("run_id") == run_id
        ]
        decisions = [
            {
                "id": row.id,
                "run_id": row.run_id,
                "action": row.action,
                "principal_type": row.principal.principal_type.value,
                "principal_id": row.principal.id,
                "subject_digest": row.subject_digest,
                "subject_fields": thaw_json_value(row.subject_fields),
                "os_uid": row.os_uid,
                "tty_name": row.tty_name,
                "decided_at": row.decided_at.isoformat(),
                "superseded_by": row.superseded_by,
            }
            for row in list_decisions(connection, run_id)
        ]
        projections = {}
        for key, kind in (
            ("behavior", "behavioral_acceptance"),
            ("calibration", "behavioral_calibration"),
        ):
            record = ledger.read_projection(aggregate_type=kind, aggregate_id=run_id)
            projections[key] = {} if record is None else thaw_json_value(record.state)
    finally:
        ledger.close()
    verified = []
    if artifacts is not None:
        store = ArtifactStore(Path(artifacts), create_directories=False)
        for digest in store.iter_digests():
            # read_bytes validates the content-addressed digest, not just file presence.
            store.read_bytes(digest)
            verified.append(str(digest))
    return {
        "attempts": attempts,
        "nodes": nodes,
        "supervisors": supervisors,
        "decisions": decisions,
        "decision_ids": [row["id"] for row in decisions],
        "execution_receipts": [
            row["result"]["execution_receipt"]
            for row in attempts
            if isinstance(row.get("result"), dict) and "execution_receipt" in row["result"]
        ],
        "artifact_digests": sorted(verified),
        "verified_artifacts": sorted(verified),
        **projections,
    }


def main() -> None:
    if len(sys.argv) not in {3, 4}:
        raise SystemExit("usage: factory_gate_project_probe.py DATABASE RUN_ID [ARTIFACTS]")
    print(json.dumps(project_state(*sys.argv[1:])))


if __name__ == "__main__":
    main()
