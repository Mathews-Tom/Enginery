"""Independent issue #232 acceptance check; uses only stdlib and candidate source.

The fixture never launches a worker, talks to a provider, or touches operator state.
Run with python3.12 from the checkout root; all durable state lives under TMPDIR.
"""

# ruff: noqa: E402 -- candidate metadata and import path must precede candidate imports.
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import tomllib
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[2]
# Source-only observer images have no installed Enginery distribution. Materialize
# real project metadata from the candidate's canonical pyproject, without pip/uv,
# network access, monkeypatches, or an installed copy of the product.
_metadata = tempfile.TemporaryDirectory(prefix="enginery-evidence-metadata-")
_project = tomllib.loads((CHECKOUT / "pyproject.toml").read_text())["project"]
_dist_info = Path(_metadata.name) / "enginery.dist-info"
_dist_info.mkdir()
_dist_info.joinpath("METADATA").write_text(
    f"Metadata-Version: 2.1\nName: {_project['name']}\nVersion: {_project['version']}\n"
    + "".join(f"Project-URL: {name}, {url}\n" for name, url in _project["urls"].items()),
    encoding="utf-8",
)
sys.path[:0] = [str(CHECKOUT / "src"), _metadata.name]

from enginery.application.behavioral_checks import checks_from_state
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.cli.main import main as cli_main
from enginery.domain.artifact import ArtifactKind, RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.execution_selection import (
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, OperationId, RunId, WorkItemId
from enginery.domain.run import Run, RunState
from enginery.domain.runner import RunnerSelection
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.engine.runtime import CoordinatorRuntime
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.artifacts import ArtifactMetadataWrite, apply_artifact_metadata
from enginery.ledger.service import LedgerService
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.run_lifecycle import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    Stage1RunService,
)

RUN_ID = "evidence-verify-run"
OWNER = "evidence-verify-observer"
STATUSES = {"intact", "missing", "tampered"}
OTHER_ARTIFACT_ID = "other-run-artifact"
LEGACY_KEYS = {
    "run_id",
    "run_status",
    "request_digest",
    "source_revision",
    "base_revision",
    "nodes",
    "hold",
    "execution",
}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise AssertionError(reason)


def fixture(root: Path) -> tuple[Path, list[dict], str]:
    """Start a real durable run and publish two different kinds of run artifacts."""
    database = root / "ledger.db"
    store = ArtifactStore(root / "artifacts")
    manifest = stage1_work_manifest()
    snapshot = WorkLedgerSnapshot(
        work_item=WorkItem(
            id=WorkItemId("evidence-verify-work"),
            work_kind=WorkKind.ISSUE,
            source_provider="github",
            external_reference="issue:232",
            source_snapshot_reference="issue:232@fixture",
            title="Verify evidence artifacts",
            objective="Rehash every artifact referenced by this run.",
            acceptance_criteria=("classify intact, missing and tampered bytes",),
            constraints=(),
            risk_class=RiskClass.LOW,
            repository_targets=("evidence-verify-repository",),
            dependencies=(),
            state=WorkItemState.QUALIFYING,
        ),
        source_revision="fixture-revision",
    )
    request = Stage1RunRequest(
        run=Run(
            id=RunId(RUN_ID),
            work_item_id=snapshot.work_item.id,
            work_item_snapshot_digest=snapshot.bound_digest,
            workflow_definition_id=manifest.id,
            workflow_definition_digest=manifest.content_digest,
            repository="evidence-verify-repository",
            base_revision="fixture-base",
            policy_set_version="fixture-policy",
            adapter_versions={},
            adapter_fingerprints={},
            capability_lock_digest=Digest.of_bytes(b"capabilities"),
            environment_manifest_digest=Digest.of_bytes(b"environment"),
            configuration_snapshot_digest=Digest.of_bytes(b"configuration"),
            state=RunState.CREATED,
        ),
        work_snapshot=snapshot,
        manifest=manifest,
        repository_id="evidence-verify-repository",
        repository_path=root,
        workspace_path=root / "workspace",
        base_branch="main",
        head_branch="enginery/evidence-verify",
        validation_commands=(("python3.12", "--version"),),
        applicable_criteria=(True,),
        required_checks=("fixture-check",),
        repair_limit=0,
        implementation=Stage1ImplementationRequest(
            attempt_id="implement-0",
            operation_id=OperationId("implement:evidence-verify"),
            time_budget_seconds=60,
            cost_budget=None,
            permitted_capabilities=("git",),
            evidence_requirements=("transcript",),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository="Mathews-Tom/Enginery",
            github_credential_reference="fixture-github-session",
            github_executable="gh",
            harness_provider="omp",
            harness_credential_reference="fixture-harness-session",
            harness_executable="omp",
            artifact_root=store.root,
        ),
        execution_selection=resolve_execution_selection(
            (LayerControls(layer=SelectionLayer.RUN, harness="omp"),),
            catalog=load_execution_catalog(),
        ),
        runner=RunnerSelection.trusted_local(),
        behavioral_checks=checks_from_state(
            [
                {
                    "name": "fixture-contract",
                    "observer": "command",
                    "argv": ["python3.12", "--version"],
                    "assertions": [{"kind": "exit_status", "equals": 0}],
                    "fault_classes": ["constant_result"],
                }
            ]
        ),
    )
    with LedgerService.open(database, artifact_store_root=store.root) as ledger:
        runtime = CoordinatorRuntime(ledger, owner=OWNER)
        service = Stage1RunService(runtime=runtime, ledger=ledger)
        service.start(
            request,
            now=datetime(2026, 10, 26, tzinfo=UTC),
            heartbeat_window=timedelta(seconds=60),
        )
        with ledger.connection:
            for artifact_id, data, kind, run_id in (
                ("evidence-plan", b"durable plan\n", ArtifactKind.PLAN, RUN_ID),
                ("evidence-log", b"durable log\n", ArtifactKind.LOG, RUN_ID),
                (OTHER_ARTIFACT_ID, b"unrelated run\n", ArtifactKind.LOG, "other-run"),
            ):
                digest = store.publish_bytes(data, media_type="text/plain")
                apply_artifact_metadata(
                    ledger.connection,
                    ArtifactMetadataWrite(
                        artifact_id=ArtifactId(artifact_id),
                        digest=digest,
                        byte_size=len(data),
                        media_type="text/plain",
                        kind=kind,
                        run_id=RunId(run_id),
                        node_id=NodeId("qualify"),
                        attempt_id=NodeAttemptId("qualify-0"),
                        redaction=RedactionClassification.PUBLIC,
                    ),
                    store=store,
                )
    rows = artifact_rows(database)
    other = next(row for row in rows if row["artifact_id"] == OTHER_ARTIFACT_ID)
    Path(other["storage_reference"]).unlink()
    return database, [row for row in rows if row["run_id"] == RUN_ID], str(request.digest)


def artifact_rows(database: Path) -> list[dict]:
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row) for row in connection.execute("SELECT * FROM artifacts ORDER BY artifact_id")
        ]


def store_snapshot(root: Path) -> dict:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def invoke(database: Path, *, verify: bool) -> tuple[int, dict, str]:
    before = database.read_bytes(), artifact_rows(database)
    store = database.parent / "artifacts"
    stored_bytes = store_snapshot(store)
    stdout, stderr = io.StringIO(), io.StringIO()
    argv = ["engine", "evidence", "--database", str(database), "--owner", OWNER, "--run-id", RUN_ID]
    if verify:
        argv.append("--verify")
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = cli_main(argv)
        except SystemExit as error:
            code = error.code
    require(
        before == (database.read_bytes(), artifact_rows(database)),
        "evidence changed ledger file bytes or artifact rows",
    )
    require(store_snapshot(store) == stored_bytes, "evidence changed the artifact store")
    try:
        document = json.loads(stdout.getvalue())
    except json.JSONDecodeError:
        detail = " ".join(stderr.getvalue().split())
        raise AssertionError(
            f"CLI exit {code}; expected JSON; {detail or 'no diagnostic'}"
        ) from None
    require(isinstance(document, dict), "evidence JSON is not an object")
    require(isinstance(code, int), f"CLI returned a non-integer exit status: {code!r}")
    return code, document, stdout.getvalue()


def strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(strings(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(strings(item) for item in value))
    return set()


def entries(value: object, artifact_id: str) -> list[dict]:
    """Allow lists, id-keyed maps, and arbitrary nesting under verification."""
    found = []
    if isinstance(value, dict):
        if artifact_id in value.values():
            found.append(value)
        for key, item in value.items():
            if key == artifact_id and isinstance(item, dict):
                found.append(item)
            found.extend(entries(item, artifact_id))
    elif isinstance(value, list):
        for item in value:
            found.extend(entries(item, artifact_id))
    return found


def mappings(value: object):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from mappings(item)
    elif isinstance(value, list):
        for item in value:
            yield from mappings(item)


def classifications(document: dict, rows: list[dict], changed: dict[str, str]) -> None:
    verification = document.get("verification")
    require(isinstance(verification, dict), "missing verification object")
    store_root = str(Path(rows[0]["storage_reference"]).parents[3])
    require(store_root in strings(verification), "verification omits the artifact store root")
    require(
        not entries(verification, OTHER_ARTIFACT_ID)
        and OTHER_ARTIFACT_ID not in strings(verification),
        "verification includes an artifact belonging to another run",
    )
    summaries = [
        value
        for mapping in mappings(verification)
        for key, value in mapping.items()
        if key in {"summary", "counts", "status_counts", "summary_counts"}
        and isinstance(value, (dict, list))
    ]
    require(bool(summaries), "verification omits the status summary")
    counts = dict.fromkeys(STATUSES, 0)
    for row in rows:
        counts[changed.get(row["artifact_id"], "intact")] += 1
    for status, expected_count in counts.items():
        reported = [
            value
            for mapping in mappings(summaries)
            for key, value in mapping.items()
            if key in {status, f"{status}_count", f"count_{status}"} and type(value) is int
        ]
        reported.extend(
            mapping["count"]
            for mapping in mappings(summaries)
            if mapping.get("status") == status and type(mapping.get("count")) is int
        )
        require(
            (bool(reported) or expected_count == 0)
            and all(count == expected_count for count in reported),
            f"verification summary must count {status} as {expected_count}",
        )
    for row in rows:
        artifact_id, digest = row["artifact_id"], row["digest"]
        matches = entries(verification, artifact_id)
        require(bool(matches), f"verification omits artifact {artifact_id}")
        expected = changed.get(artifact_id, "intact")
        for entry in matches:
            values = strings(entry)
            require(
                values & STATUSES == {expected},
                f"{artifact_id}: expected {expected}, got {sorted(values & STATUSES)}",
            )
            require(
                digest in values or digest.partition(":")[2] in values,
                f"{artifact_id}: verification omits recorded digest {digest}",
            )


def run() -> int:
    case = "fixture"
    try:
        with tempfile.TemporaryDirectory(prefix="enginery-evidence-verify-") as directory:
            database, rows, request_digest = fixture(Path(directory).resolve())
            require(len(rows) >= 2, "fixture did not persist two run artifacts")
            case = "legacy"
            code, original, original_bytes = invoke(database, verify=False)
            require(code == 0, f"legacy evidence exited {code}")
            require("verification" not in original, "legacy output contains verification")
            require(set(original) == LEGACY_KEYS, "legacy output top-level keys changed")
            require(original["run_id"] == RUN_ID, "legacy output has the wrong run id")
            require(
                original["request_digest"] == request_digest,
                "legacy output has the wrong request digest",
            )
            print("PASS legacy")
            case = "intact"
            code, document, _ = invoke(database, verify=True)
            require(code == 0, f"intact evidence exited {code}")
            classifications(document, rows, {})
            code, _, repeated_bytes = invoke(database, verify=False)
            require(
                code == 0 and repeated_bytes == original_bytes,
                "non-verify output bytes changed around verification",
            )
            print("PASS intact")
            first, second = rows[:2]
            first_path = Path(first["storage_reference"])
            first_bytes = first_path.read_bytes()
            first_path.unlink()
            case = "missing"
            code, document, _ = invoke(database, verify=True)
            require(code != 0, "missing bytes incorrectly exited 0")
            classifications(document, rows, {first["artifact_id"]: "missing"})
            print("PASS missing")
            first_path.write_bytes(first_bytes)
            second_path = Path(second["storage_reference"])
            # Same length: size-only verification is insufficient.
            second_bytes = second_path.read_bytes()
            second_path.write_bytes(bytes(byte ^ 1 for byte in second_bytes))
            case = "tampered"
            code, document, _ = invoke(database, verify=True)
            require(code != 0, "tampered bytes incorrectly exited 0")
            classifications(document, rows, {second["artifact_id"]: "tampered"})
            print("PASS tampered")
            case = "read-only"
            code, _, unchanged_bytes = invoke(database, verify=False)
            require(
                code == 0 and unchanged_bytes == original_bytes,
                "non-verify output changed after byte mutations",
            )
            # invoke compares raw ledger bytes, every artifact row and the entire store.
            print("PASS read-only")
        print("evidence-verify: ok")
        return 0
    except Exception as error:
        print(f"FAIL {case}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
