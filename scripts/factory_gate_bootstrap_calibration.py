"""Execute the frozen bootstrap faults through the existing behavioral CLI driver."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from enginery.evidence.bootstrap_calibration import CALIBRATION_SET_SHA256, load_calibration_set
from enginery.ledger.artifact_store import ArtifactStore
from enginery.workflows.behavioral_acceptance import _FIXTURE
from factory_gate_behavioral import BehavioralGate, _rows
from factory_gate_context import GateOptions
from factory_gate_evidence import EvidenceBuilder


def _retain_artifacts(gate: BehavioralGate, run_id: str, root: Path) -> dict[str, str]:
    """Resolve the actual request store, then retain digest-verified original bytes."""
    request_path = gate.state / "runs" / run_id / "request.json"
    request = json.loads(request_path.read_bytes())
    source = ArtifactStore(
        Path(request["execution_configuration"]["artifact_root"]),
        create_directories=False,
    )
    retained = ArtifactStore(gate.options.raw_dir / "artifacts")
    references: dict[str, str] = {}
    for digest in source.iter_digests():
        content = source.read_bytes(digest)
        copied_digest = retained.publish_bytes(content)
        if copied_digest != digest or retained.read_bytes(digest) != content:
            raise ValueError(f"calibration artifact retention mismatch: {digest}")
        references[str(digest)] = retained.path_for(digest).relative_to(root).as_posix()
    return references


def run_calibration(root: Path) -> dict[str, Any]:
    """Retain product outcomes and a stable per-entry assertion summary.

    The weaker evaluator assertion accepts successful process termination; the
    stronger one independently evaluates stdout. Only the product's caught
    outcomes name a rung. The summary omits run-specific receipts and digests.
    """
    dataset = load_calibration_set()
    fixture_digest = hashlib.sha256(_FIXTURE).hexdigest()
    if any(entry["fault_sha256"] != fixture_digest for entry in dataset["entries"]):
        raise ValueError("bootstrap deliberate-fault executable digest mismatch")
    root.mkdir(parents=True, exist_ok=True)
    gate = BehavioralGate(
        GateOptions(scenario="behavioral-acceptance", evidence_out=root / "calibration.json"),
        EvidenceBuilder(scenario="behavioral-acceptance", mode="local", required={}),
    )
    results: list[dict[str, Any]] = []
    digests: dict[str, str] = {}
    try:
        for entry in dataset["entries"]:
            weak = entry["id"] == "weak-exit-status"
            check: dict[str, Any] = {
                "name": entry["id"],
                "observer": "command",
                "argv": [sys.executable, "install_sample.py"],
                "assertions": (
                    [{"kind": "exit_status", "equals": 0}]
                    if weak
                    else [{"kind": "stdout", "equals": "5\n"}]
                ),
                "fault_classes": [entry["fault_class"]],
            }
            if entry["id"] == "missing-observer":
                check["argv"] = [str(gate.root / "missing-observer-executable")]
            receipts: list[dict[str, Any]] = []
            assertions = ("exit_status", "stdout") if weak else ("stdout",)
            outcomes: list[dict[str, Any]] = []
            for index, assertion in enumerate(assertions):
                if index:
                    check = {**check, "assertions": [{"kind": "stdout", "equals": "5\n"}]}
                run_id = gate.bind([check])
                arguments = (
                    ("--without-fault", "constant_result")
                    if entry["id"] == "withheld-output"
                    else ()
                )
                code, receipt = gate.factory("calibrate", run_id, *arguments)
                rows = [
                    row for row in _rows(receipt) if row.get("fault_class") == entry["fault_class"]
                ]
                if len(rows) != 1:
                    raise ValueError(
                        f"calibration did not execute exactly one fault: {entry['id']}"
                    )
                row = rows[0]
                outcomes.append(
                    {
                        "outcome": row["classification"],
                        "rung": row["rung"],
                        "assertion": check["assertions"][0]["kind"],
                    }
                )
                name = f"{entry['id']}-{index}.json"
                content = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
                (root / name).write_bytes(content)
                digests[name] = hashlib.sha256(content).hexdigest()
                artifacts = _retain_artifacts(gate, run_id, root)
                receipts.append(
                    {
                        "path": str((root / name).resolve()),
                        "sha256": digests[name],
                        "exit_code": code,
                        "assertion": assertion,
                        "result": row,
                        "document": receipt,
                        "artifacts": artifacts,
                    }
                )
            expected = [
                {
                    "outcome": entry["expected_outcome"],
                    "rung": entry["rung"],
                    "assertion": entry["assertion"],
                }
            ]
            if weak:
                expected.append(
                    {
                        "outcome": entry["next_expected_outcome"],
                        "rung": entry["next_rung"],
                        "assertion": entry["next_assertion"],
                    }
                )
            results.append(
                {
                    "id": entry["id"],
                    "owner": entry["owner"],
                    "fault_class": entry["fault_class"],
                    "fault_sha256": entry["fault_sha256"],
                    "expected_outcomes": expected,
                    "classification": outcomes[0]["outcome"],
                    "rung": outcomes[0]["rung"],
                    "assertion": outcomes[0]["assertion"],
                    "receipts": receipts,
                    "outcomes": outcomes,
                    "passed": outcomes == expected,
                }
            )
    finally:
        gate.cleanup()
    for artifact in sorted(gate.options.raw_dir.rglob("*")):
        if artifact.is_file():
            name = artifact.relative_to(root).as_posix()
            digests[name] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    return {
        "schema_version": 1,
        "set_sha256": CALIBRATION_SET_SHA256,
        "entries": results,
        "summary": [
            {"id": entry["id"], "outcomes": entry["outcomes"], "passed": entry["passed"]}
            for entry in results
        ],
        "outcomes": sorted(
            {
                receipt["result"]["classification"]
                for entry in results
                for receipt in entry["receipts"]
            }
        ),
        "digests": digests,
        "passed": all(entry["passed"] for entry in results),
    }
