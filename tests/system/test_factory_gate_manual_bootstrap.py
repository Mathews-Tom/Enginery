"""Campaign reuse, external recovery and fail-closed bootstrap evidence checks."""

from __future__ import annotations

import copy
import json
import shlex
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import factory_gate_manual_bootstrap as bootstrap
from factory_gate_context import GateOptions
from factory_gate_evidence import EXIT_FAIL, EXIT_PASS, EvidenceBuilder, validate_evidence
from run_factory_gate import main, parse_options


def _argv(root: Path, name: str = "bootstrap", *extra: str) -> list[str]:
    return [
        "--scenario",
        "manual-bootstrap",
        "--campaign-id",
        "reuse-campaign",
        "--reuse-evidence",
        "--evidence-out",
        str(root / f"{name}.json"),
        *extra,
    ]


def _read(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def campaign_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("bootstrap-protocols")
    for harness in ("omp", "claude"):
        output = root / f"protocol-{harness}.json"
        code = main(
            [
                "--scenario",
                "protocol",
                "--harness",
                harness,
                "--campaign-id",
                "reuse-campaign",
                "--evidence-out",
                str(output),
            ]
        )
        assert code == EXIT_PASS, _read(output)
    return root


def _manifest(root: Path) -> Path:
    return root / "campaigns" / "reuse-campaign" / "manifest.json"


def _workflow(root: Path) -> dict[str, Any]:
    document = _read(_manifest(root))
    return dict(document["evidence"][document["designated_workflow"]]["workflow"])


def _assertion(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in document["assertions"] if item["id"] == name)


def test_end_to_end_reuses_both_protocol_harnesses_and_no_fifth_worker(
    campaign_root: Path, tmp_path: Path
) -> None:
    before = _read(_manifest(campaign_root))
    workflow = _workflow(campaign_root)
    launch_log = Path(workflow["environment"]["HOME"]).parent / "harness-launches.jsonl"
    launches_before = launch_log.read_bytes()
    linked = tmp_path / "campaign"
    linked.symlink_to(campaign_root, target_is_directory=True)
    code = main(_argv(linked))
    document = _read(campaign_root / "bootstrap.json")
    assert code == EXIT_PASS, document
    assert validate_evidence(document) == []
    assert {item["id"] for item in document["assertions"]} == set(bootstrap.REQUIRED)
    assert all(item["ran"] and item["status"] == "pass" for item in document["assertions"])
    after = _read(_manifest(campaign_root))
    assert after["slots"] == before["slots"]
    assert after["evidence"] == before["evidence"]
    assert len(after["slots"]) == 4
    assert after["reuse"][-1]["reasons"] == []
    assert launches_before == launch_log.read_bytes()
    effects = document["external_effect_counts"]
    assert effects["worker_sessions"] == effects["hosted_inference_calls"] == 0
    assert effects["isolated_containers"] is None
    assert not _assertion(document, "zero-worker-containers")["observed"]["events_readable"]
    recovery = _assertion(document, "external-operator-recovery")["observed"]
    assert recovery["equivalence"]["equivalent"]
    assert recovery["restore_target_differs"]
    assert recovery["live_pre_restore_sidecars"] == []
    for name, report in recovery.items():
        if name in {"equivalence", "restore_target_differs", "live_pre_restore_sidecars"}:
            continue
        assert report["workflow_imports"] == []
        assert report["code"] == 0
        assert not any(Path(argv[0]).name in {"omp", "claude"} for argv in report["subprocesses"])
    calibration = _assertion(document, "known-defect-calibration")["observed"]["execution"]
    assert set(calibration["outcomes"]) == {"caught", "escaped", "not_injected", "inconclusive"}
    assert len(calibration["entries"]) >= 4
    assert all(
        item["passed"] and item["owner"] and item["fault_sha256"] for item in calibration["entries"]
    )
    for entry in calibration["entries"]:
        for receipt in entry["receipts"]:
            assert Path(receipt["path"]).is_file()
    index = _read(campaign_root / "bootstrap.index.json")
    assert index["qualification_target"] == "synthetic"
    assert index["binding"]["qualification_target"] == "synthetic"
    assert set(index["baseline_journeys"]) == {"omp", "claude"}
    for journey in index["baseline_journeys"].values():
        assert _read(Path(journey["path"]))["verdict"] == "pass"
    assert index["resource_bounds"]["maximum_worker_sessions"] == 4
    assert index["resource_bounds"]["concurrency"] == 1
    assert all(not violations for violations in index["invariants"]["boundary_layers"].values())


@pytest.mark.parametrize(
    "mutation,assertion",
    [
        ("head", "current-head-open-pr"),
        ("closed", "current-head-open-pr"),
        ("decision", "human-action-decision"),
        ("raw", "campaign-evidence-reused"),
        ("evidence", "campaign-evidence-reused"),
        ("binding", "campaign-evidence-reused"),
        ("missing-harness", "campaign-evidence-reused"),
        ("qualification-details", "campaign-evidence-reused"),
        ("qualification-identities", "campaign-evidence-reused"),
    ],
)
def test_missing_or_stale_reuse_fails_closed(
    campaign_root: Path, mutation: str, assertion: str
) -> None:
    manifest_path = _manifest(campaign_root)
    original_manifest = manifest_path.read_bytes()
    workflow = _workflow(campaign_root)
    target: Path | None = None
    original: bytes | None = None
    decision_rows: list[tuple[Any, ...]] = []
    database = Path(workflow["database"])
    try:
        if mutation in {"head", "closed"}:
            target = Path(workflow["pr_path"])
            original = target.read_bytes()
            pr = _read(target)
            if mutation == "head":
                pr["head"]["sha"] = "f" * 40
            else:
                pr["state"] = "closed"
            target.write_text(json.dumps(pr), encoding="utf-8")
        elif mutation == "decision":
            with sqlite3.connect(database) as connection:
                decision_rows = list(connection.execute("SELECT * FROM human_decisions"))
                assert decision_rows, "protocol fixture must retain a genuine product decision"
                connection.execute("DELETE FROM human_decisions")
        elif mutation in {"raw", "evidence"}:
            manifest = _read(manifest_path)
            record = manifest["evidence"]["omp:completed-workflow"]
            target = (
                Path(next(iter(record["raw_originals"])))
                if mutation == "raw"
                else Path(record["path"])
            )
            original = target.read_bytes()
            target.write_bytes(original + b"\ncorruption")
        elif mutation.startswith("qualification-"):
            from factory_gate_campaign import sha256

            manifest = _read(manifest_path)
            record = manifest["protocol_evidence"]["omp"]
            target = Path(record["path"])
            original = target.read_bytes()
            protocol = _read(target)
            field = mutation.removeprefix("qualification-")
            protocol[field]["qualification_target"] = "real"
            target.write_text(json.dumps(protocol), encoding="utf-8")
            record["sha256"] = sha256(target)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        elif mutation == "missing-harness":
            manifest = _read(manifest_path)
            manifest["evidence"].pop("claude:completed-workflow")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        else:
            manifest = _read(manifest_path)
            manifest["binding"]["subject_commit"] = "a" * 40
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        output_name = f"missing-{mutation}"
        code = main(_argv(campaign_root, output_name))
        document = _read(campaign_root / f"{output_name}.json")
        assert code == EXIT_FAIL, document
        assert _assertion(document, assertion)["status"] == "fail"
        assert document["external_effect_counts"]["worker_sessions"] == 0
        assert len(_read(manifest_path)["slots"]) == 4
    finally:
        if target is not None and original is not None:
            target.write_bytes(original)
        if decision_rows:
            with sqlite3.connect(database) as connection:
                placeholders = ",".join("?" for _ in decision_rows[0])
                connection.executemany(
                    f"INSERT INTO human_decisions VALUES ({placeholders})", decision_rows
                )
        manifest_path.write_bytes(original_manifest)


@pytest.mark.parametrize(
    ("verdict", "damage"),
    [
        ("hold", "missing"),
        ("invalid", "non-object"),
        ("pass", "missing"),
        ("pass", "non-object"),
        ("pass", "details"),
        ("pass", "identities"),
    ],
)
def test_invalid_protocol_record_keeps_itemised_problems_and_workflow(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch, verdict: str, damage: str
) -> None:
    from factory_gate_campaign import sha256

    gates: list[bootstrap.BootstrapGate] = []

    class ObservedGate(bootstrap.BootstrapGate):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            gates.append(self)

    monkeypatch.setattr(bootstrap, "BootstrapGate", ObservedGate)
    manifest_path = _manifest(campaign_root)
    original_manifest = manifest_path.read_bytes()
    manifest = _read(manifest_path)
    record = manifest["protocol_evidence"]["omp"]
    target = Path(record["path"])
    original = target.read_bytes()
    try:
        record["verdict"] = verdict
        if damage == "missing":
            record["path"] = str(campaign_root / "absent-protocol.json")
        elif damage == "non-object":
            target.write_text("[]", encoding="utf-8")
            record["sha256"] = sha256(target)
        else:
            protocol = _read(target)
            protocol[damage] = [] if damage == "details" else None
            target.write_text(json.dumps(protocol), encoding="utf-8")
            record["sha256"] = sha256(target)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        output_name = f"invalid-protocol-{verdict}-{damage}"
        assert main(_argv(campaign_root, output_name)) == EXIT_FAIL
        document = _read(campaign_root / f"{output_name}.json")
        observed = _assertion(document, "campaign-evidence-reused")["observed"]
        assert "error" not in observed
        assert observed["problems"]
        if verdict != "pass":
            assert "omp: missing passing protocol evidence" in observed["problems"]
            assert not any("qualification_target" in problem for problem in observed["problems"])
        else:
            assert any("qualification_target" in problem for problem in observed["problems"])
        assert gates[0].workflow == _workflow(campaign_root)
        assert document["external_effect_counts"]["worker_sessions"] == 0
    finally:
        target.write_bytes(original)
        manifest_path.write_bytes(original_manifest)


def test_missing_campaign_does_not_manufacture_successful_evidence(tmp_path: Path) -> None:
    code = main(_argv(tmp_path))
    document = _read(tmp_path / "bootstrap.json")
    assert code == EXIT_FAIL
    assert _assertion(document, "campaign-evidence-reused")["status"] == "fail"
    assert not _manifest(tmp_path).exists()


def test_missing_install_receipt_fails_scenario_without_launching_worker(
    campaign_root: Path,
) -> None:
    output_name = "missing-install"
    code = main(
        _argv(
            campaign_root,
            output_name,
            "--install-receipt",
            str(campaign_root / "absent-install-receipt.json"),
        )
    )
    document = _read(campaign_root / f"{output_name}.json")
    assert code == EXIT_FAIL
    assert _assertion(document, "installed-wheel-identity")["status"] == "fail"
    assert document["external_effect_counts"]["worker_sessions"] == 0
    assert len(_read(_manifest(campaign_root))["slots"]) == 4


def _receipt(path: Path, system: str, digest: str = "a" * 64) -> Path:
    document = {
        "schema_version": 1,
        "platform": {"system": system, "distribution": {"ID": "ubuntu"}},
        "python": {"version": "3.13", "executable": "/fresh/bin/python"},
        "artifact": {"kind": "wheel", "sha256": digest, "name": "enginery.whl"},
        "installed_version": "0.6.0",
        "entry_point": "/fresh/bin/enginery",
        "commands": [
            {"argv": ["/host/python", "-m", "venv", "/fresh"], "exit_code": 0},
            {"argv": ["/fresh/bin/python", "-m", "pip", "install", "enginery.whl"], "exit_code": 0},
            {"argv": ["/fresh/bin/enginery", "--version"], "exit_code": 0},
        ],
        "outcome": "pass",
        "exit_code": 0,
        "not_run": [],
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_install_receipts_require_both_platforms_exact_wheel_and_completed_smoke(
    tmp_path: Path,
) -> None:
    macos = _receipt(tmp_path / "macos.json", "Darwin")
    ubuntu = _receipt(tmp_path / "ubuntu.json", "Linux")
    assert bootstrap.install_receipt_problems((macos, ubuntu), "a" * 64)[0] == []
    assert "missing Ubuntu" in " ".join(bootstrap.install_receipt_problems((macos,), "a" * 64)[0])
    assert bootstrap.install_receipt_problems((macos, ubuntu), "b" * 64)[0]
    document = _read(ubuntu)
    document["outcome"] = "incomplete"
    document["not_run"] = ["lifecycle"]
    ubuntu.write_text(json.dumps(document), encoding="utf-8")
    assert bootstrap.install_receipt_problems((macos, ubuntu), "a" * 64)[0]
    assert bootstrap.install_receipt_problems((tmp_path / "missing.json",), "a" * 64)[0]


@pytest.mark.parametrize("change", ["head", "closed", "merged", "paths", "checks", "review"])
def test_current_pr_snapshot_drift_is_not_current_head_evidence(change: str) -> None:
    head = "a" * 40
    recorded = {
        "state": "open",
        "merged": False,
        "head": {"sha": head},
        "changed_paths": ["result.txt"],
        "check_runs": [{"head_sha": head, "status": "completed", "conclusion": "success"}],
        "reviews": [{"commit_id": head, "state": "APPROVED"}],
    }
    current = copy.deepcopy(recorded)
    if change == "head":
        current["head"]["sha"] = "b" * 40
    elif change == "closed":
        current["state"] = "closed"
    elif change == "merged":
        current["merged"] = True
    elif change == "paths":
        current["changed_paths"] = []
    elif change == "checks":
        current["check_runs"] = []
    else:
        current["reviews"][0]["state"] = "CHANGES_REQUESTED"
    assert bootstrap.pr_problems(current, recorded, head)


def test_human_decision_must_bind_run_head_and_terminal() -> None:
    decision = {
        "id": "decision",
        "run_id": "run",
        "action": "pull_request.open",
        "principal_type": "human",
        "principal_id": "registered-human",
        "os_uid": 501,
        "tty_name": "/dev/ttys001",
        "decided_at": "2026-10-18T00:00:00+00:00",
        "superseded_by": None,
        "subject_fields": {"head_revision": "head"},
    }
    assert bootstrap.human_decisions({"decisions": [decision]}, "run", "head")
    for patch in (
        {"principal_type": "agent"},
        {"tty_name": ""},
        {"run_id": "other"},
        {"subject_fields": {"head_revision": "old"}},
        {"superseded_by": "new"},
    ):
        assert not bootstrap.human_decisions({"decisions": [{**decision, **patch}]}, "run", "head")


@pytest.mark.parametrize(
    "events,passed",
    [
        ("", True),
        (None, False),
        (
            json.dumps(
                {
                    "Type": "container",
                    "Action": "create",
                    "Actor": {"Attributes": {"name": "unlabelled-worker"}},
                }
            ),
            False,
        ),
    ],
)
def test_live_zero_container_guard_reads_real_event_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: str | None,
    passed: bool,
) -> None:
    options = GateOptions(
        scenario="manual-bootstrap",
        evidence_out=tmp_path / "result.json",
        campaign_id="reuse-campaign",
        reuse_evidence=True,
    )
    evidence = EvidenceBuilder(
        scenario="manual-bootstrap", mode="live", required=bootstrap.REQUIRED
    )
    gate = bootstrap.BootstrapGate(options, evidence)
    gate.options = replace(options, live=True)
    gate.window_start = "2026-10-18T00:00:00Z"
    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(bootstrap, "engine_time", lambda gate: "2026-10-18T00:00:01Z")

    def read_events(gate: Any, since: str, until: str) -> str | None:
        observed.append((since, until))
        return events

    monkeypatch.setattr(bootstrap, "engine_creates", read_events)
    try:
        bootstrap.finalize(gate, evidence)
        assert bool(evidence._assertions["zero-worker-containers"]["status"] == "pass") is passed
        assert observed == [("2026-10-18T00:00:00Z", "2026-10-18T00:00:02Z")]
        if not passed:
            bootstrap.freeze(gate, evidence)
            assert not options.evidence_out.with_suffix(".index.json").exists()
    finally:
        gate.cleanup()


def test_operator_guard_rejects_workflow_import_from_installed_entry_point(tmp_path: Path) -> None:
    entry = tmp_path / "enginery"
    entry.write_text("import enginery.workflows.stage1\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(bootstrap.PROBE),
            "operator",
            json.dumps(["operator", "status", "--database", str(tmp_path / "ledger.sqlite")]),
            str(entry),
            "live",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert "operator imported workflow code" in result.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["omp", "-p", "never launch"],
        [sys.executable, "-c", "raise RuntimeError('never launch')"],
        ["docker", "compose", "up"],
    ],
)
def test_operator_guard_rejects_agent_launch_from_installed_entry_point(
    tmp_path: Path, argv: list[str]
) -> None:
    entry = tmp_path / "enginery"
    entry.write_text(f"import subprocess\nsubprocess.run({argv!r})\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(bootstrap.PROBE),
            "operator",
            json.dumps(["operator", "status", "--database", str(tmp_path / "ledger.sqlite")]),
            str(entry),
            "live",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode != 0
    assert "worker/agent launch" in result.stderr


@pytest.mark.parametrize(
    "extra,remove,scenario,message",
    [
        (
            ["--harness", "omp"],
            [],
            "manual-bootstrap",
            "--harness and --model are invalid for manual-bootstrap",
        ),
        (
            ["--model", "provider/model"],
            [],
            "manual-bootstrap",
            "--harness and --model are invalid for manual-bootstrap",
        ),
        (
            [
                "--live",
                "--gateway-url",
                "https://gateway.example/v1",
                "--gateway-token-file",
                "/token",
                "--custody-receipt",
                "/custody.json",
                "--confirm-single-session",
                "--runner-image",
                "sha256:" + "a" * 64,
                "--relay-image",
                "sha256:" + "b" * 64,
                "--docker-context",
                "qualified",
                "--subject-commit",
                "c" * 40,
                "--campaign-config",
                "/campaign.json",
            ],
            ["--reuse-evidence"],
            "manual-bootstrap",
            "live manual-bootstrap requires --reuse-evidence",
        ),
        (
            [],
            ["--campaign-id", "reuse-campaign"],
            "manual-bootstrap",
            "--campaign-id is required and must match [a-z0-9][a-z0-9-]{2,62}",
        ),
        (
            ["--harness", "omp"],
            [],
            "protocol",
            "--reuse-evidence and --install-receipt apply only to manual-bootstrap",
        ),
        (
            ["--harness", "omp", "--install-receipt", "/receipt.json"],
            ["--reuse-evidence"],
            "protocol",
            "--reuse-evidence and --install-receipt apply only to manual-bootstrap",
        ),
    ],
)
def test_bootstrap_option_rejections(
    tmp_path: Path,
    extra: list[str],
    remove: list[str],
    scenario: str,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _argv(tmp_path, "invalid", *extra)
    argv[1] = scenario
    for item in remove:
        argv.remove(item)
    with pytest.raises(SystemExit) as raised:
        parse_options(argv, environ={})
    assert raised.value.code == 2
    assert capsys.readouterr().err.rstrip().endswith("error: " + message)


def test_nonlive_without_reuse_fails_campaign_assertion(campaign_root: Path) -> None:
    argv = _argv(campaign_root, "no-reuse")
    argv.remove("--reuse-evidence")
    assert main(argv) == EXIT_FAIL
    document = _read(campaign_root / "no-reuse.json")
    assert _assertion(document, "campaign-evidence-reused")["status"] == "fail"
    assert not (campaign_root / "no-reuse.index.json").exists()


def test_other_harness_product_store_corruption_is_not_hidden_by_raw_copies(
    campaign_root: Path,
) -> None:
    from enginery.ledger.artifact_store import ArtifactStore

    manifest = _read(_manifest(campaign_root))
    other = manifest["evidence"]["claude:completed-workflow"]["workflow"]
    store = ArtifactStore(Path(other["artifact_root"]), create_directories=False)
    digest = next(store.iter_digests())
    path = store.path_for(digest)
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"corruption")
        code = main(_argv(campaign_root, "other-store-corrupt"))
        document = _read(campaign_root / "other-store-corrupt.json")
        assert code == EXIT_FAIL
        assert _assertion(document, "raw-and-product-recovery")["status"] == "fail"
    finally:
        path.write_bytes(original)


def test_check_run_identity_allows_queued_to_success_but_not_replacement() -> None:
    recorded = {
        "state": "open",
        "merged": False,
        "head": {"sha": "head"},
        "changed_paths": ["result.txt"],
        "reviews": [],
        "check_runs": [
            {
                "id": 1,
                "name": "CI",
                "head_sha": "head",
                "app": {"slug": "actions"},
                "status": "queued",
                "conclusion": None,
            }
        ],
    }
    current = copy.deepcopy(recorded)
    current["check_runs"][0].update(status="completed", conclusion="success")
    assert not bootstrap.pr_problems(current, recorded, "head")
    current["check_runs"][0]["id"] = 2
    assert bootstrap.pr_problems(current, recorded, "head")
    current["check_runs"][0]["id"] = 1
    recorded["check_runs"][0].update(status="completed", conclusion="failure")
    assert bootstrap.pr_problems(current, recorded, "head")


@pytest.fixture
def live_gate(
    campaign_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
):
    recovered = getattr(request, "param", False)
    options = parse_options(_argv(campaign_root, f"live-shaped-{tmp_path.name}-{recovered}"))
    evidence = EvidenceBuilder(
        scenario="manual-bootstrap", mode="live", required=bootstrap.REQUIRED
    )
    gate = bootstrap.BootstrapGate(options, evidence)
    manifest = gate.campaign.read()
    gate.binding = manifest["binding"]
    gate.workflow = dict(manifest["evidence"][manifest["designated_workflow"]]["workflow"])
    try:
        current = gate.current_pr()
        current["user"] = {"login": "private-reviewer", "avatar_url": "https://private/avatar"}
        current["body"] = "private PR prose"
        current["assignees"] = [{"login": "private-assignee"}]
        for review in current.get("reviews", []):
            review["user"] = {"login": "private-reviewer"}
        # Compare the unchanged full objects in memory, but never retain these fields.
        gate.workflow["pr"] = copy.deepcopy(current)
        snapshot = gate.project_snapshot()
        recovery = bootstrap._recover_operator(gate)
        profile = {
            "name": "default",
            "profile_digest": "sha256:" + "a" * 64,
            "effective": {"memory_bytes": 1024},
            "bounds": None,
            "runner_receipt_digest": "sha256:" + "b" * 64,
        }
        gate.workflow["runner_profile"] = profile
        key = manifest["designated_workflow"]
        manifest["evidence"][key]["workflow"] = gate.workflow
        if recovered:
            slot = manifest["slots"][key]
            slot["state"] = "reconciled"
            slot["reconciliation"] = {
                "terminal_status": "completed",
                "container_gone": True,
                "group_gone": True,
                "workflow": gate.workflow,
            }
        original_document = bootstrap._document
        retained_path = Path(manifest["evidence"][key]["path"])

        def document(path: Path) -> dict[str, Any]:
            return gate.workflow if path == retained_path else original_document(path)

        monkeypatch.setattr(bootstrap, "_document", document)
        monkeypatch.setattr(gate.campaign, "read", lambda: copy.deepcopy(manifest))
        monkeypatch.setattr(gate.campaign, "bind", lambda binding: None)
        monkeypatch.setattr(bootstrap, "campaign_binding", lambda options: gate.binding)
        monkeypatch.setattr(gate, "current_pr", lambda: current)
        monkeypatch.setattr(gate, "project_snapshot", lambda: snapshot)
        monkeypatch.setattr(bootstrap, "_recover_operator", lambda gate: recovery)
        monkeypatch.setattr(bootstrap, "engine_time", lambda gate: "2026-10-18T00:00:01Z")
        monkeypatch.setattr(bootstrap, "engine_creates", lambda *args: "")
        version = "0.6.0"
        gate.binding["installed_distribution"]["version"] = version
        gate.binding["executable"] = "/fresh/bin/enginery"
        monkeypatch.setattr(
            gate,
            "execute",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, f"enginery {version}", ""),
        )
        macos = _receipt(tmp_path / "macos.json", "Darwin", gate.binding["wheel_sha256"])
        ubuntu = _receipt(tmp_path / "ubuntu.json", "Linux", gate.binding["wheel_sha256"])
        gate.options = replace(options, install_receipts=(macos, ubuntu))
        boundary = tmp_path / "boundary.json"
        boundary.write_text(
            json.dumps(
                {
                    "scenario": "runner-boundary",
                    "verdict": "pass",
                    "identities": {"image": {"image_id": gate.binding["runner_image"]}},
                    "assertions": [{"status": "pass", "ran": True}],
                }
            ),
            encoding="utf-8",
        )
        gate.config["boundary_evidence"] = str(boundary)
        gate.options = replace(gate.options, live=True)
        bootstrap.run(gate, evidence)
        assert not gate.options.evidence_out.with_suffix(".index.json").exists()
        bootstrap.finalize(gate, evidence)
        assert not gate.options.evidence_out.with_suffix(".index.json").exists()
        assert gate.reasons == []
        yield gate
    finally:
        gate.cleanup()


@pytest.fixture
def producer_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import release_consumer_smoke as consumer

    wheel = tmp_path / "enginery-0.6.0-py3-none-any.whl"
    wheel.write_bytes(b"receipt producer wheel identity")

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "enginery 0.6.0\n", "")

    def surface(*, executable, root, version, **kwargs):
        consumer._check_version(executable, root=root, version=version)
        return []

    paths = []
    with monkeypatch.context() as patch:
        patch.setattr(consumer, "subprocess", SimpleNamespace(run=process))
        patch.setattr(consumer, "exercise_surface", surface)
        patch.setattr(consumer.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"})
        patch.setattr(consumer.sys, "platform", "linux")
        for system in ("Darwin", "Linux"):
            patch.setattr(consumer.platform, "system", lambda system=system: system)
            path = tmp_path / f"producer-{system}.json"
            assert consumer.main(["--wheel", str(wheel), "--install-receipt", str(path)]) == 0
            paths.append(path)
    return tuple(paths), consumer._sha256(wheel)


def test_real_install_receipt_producer_is_accepted(producer_receipts) -> None:
    paths, digest = producer_receipts
    for path in paths:
        receipt = _read(path)
        assert len(receipt["commands"]) == 3
        assert receipt["commands"][0]["argv"][1:3] == ["-m", "venv"]
        assert receipt["commands"][-1]["argv"] == [receipt["entry_point"], "--version"]
    assert bootstrap.install_receipt_problems(paths, digest, "0.6.0")[0] == []


@pytest.mark.parametrize(
    "field,value",
    [
        (("schema_version",), 2),
        (("outcome",), "incomplete"),
        (("not_run",), ["lifecycle"]),
        (("exit_code",), 1),
        (("artifact", "kind"), "sdist"),
        (("artifact", "sha256"), "b" * 64),
        (("installed_version",), None),
        (("installed_version",), "9.9.9"),
        (("python", "version"), ""),
        (("python", "executable"), "/other/bin/python"),
        (("platform", "system"), "Windows"),
        (("platform", "distribution", "ID"), "debian"),
        (("commands", 0, "argv"), []),
        (("commands", 0, "argv"), "not-an-argv-list"),
        (("commands", 0, "exit_code"), "0"),
        (("commands", 2, "argv", 0), "enginery"),
        (("commands", 2, "argv", 1), "--help"),
        (("commands", 2, "exit_code"), 1),
        (("commands",), []),
        (("commands",), {"argv": ["not-a-command-list"]}),
    ],
)
def test_install_receipt_single_field_mutations_fail(producer_receipts, field, value) -> None:
    paths, digest = producer_receipts
    assert bootstrap.install_receipt_problems(paths, digest, "0.6.0")[0] == []
    receipt = _read(paths[1])
    target = receipt
    for key in field[:-1]:
        target = target[key]
    target[field[-1]] = value
    paths[1].write_text(json.dumps(receipt), encoding="utf-8")
    problems = bootstrap.install_receipt_problems(paths, digest, "0.6.0")[0]
    assert f"invalid or mismatching install receipt: {paths[1]}" in problems


@pytest.mark.parametrize("location", ["relative/bin", "/fresh/tools", "source"])
def test_install_receipt_rejects_nonisolated_install_location(
    producer_receipts,
    location: str,
) -> None:
    paths, digest = producer_receipts
    receipt = _read(paths[1])
    directory = (
        Path(bootstrap.__file__).resolve().parents[1] / ".venv/bin"
        if location == "source"
        else Path(location)
    )
    receipt["entry_point"] = str(directory / "enginery")
    receipt["python"]["executable"] = str(directory / "python")
    receipt["commands"][-1]["argv"][0] = receipt["entry_point"]
    paths[1].write_text(json.dumps(receipt), encoding="utf-8")
    assert bootstrap.install_receipt_problems(paths, digest, "0.6.0")[0]


@pytest.mark.parametrize("live_gate", [False, True], indirect=True, ids=["fresh", "reconciled"])
def test_live_shaped_run_finalize_and_freeze_uses_observed_profile(live_gate) -> None:
    bootstrap.freeze(live_gate, live_gate.evidence)
    assert live_gate.reasons == []
    index = _read(live_gate.options.evidence_out.with_suffix(".index.json"))
    assert index["resource_bounds"]["profile"] == live_gate.workflow["runner_profile"]
    assert set(index["calibration"]) == {"set_sha256", "summary"}


def test_missing_calibration_names_operator_repair(live_gate, monkeypatch) -> None:
    snapshot = copy.deepcopy(live_gate.project_snapshot())
    snapshot.pop("calibration", None)
    monkeypatch.setattr(live_gate, "project_snapshot", lambda: snapshot)
    live_gate.evidence._assertions.clear()
    bootstrap.run(live_gate, live_gate.evidence)
    assertion = live_gate.evidence._assertions["observed-changed-behavior"]
    assert assertion["status"] == "fail"
    assert shlex.split(assertion["observed"]["repair"]) == [
        "enginery",
        "calibrate",
        live_gate.workflow["run_id"],
        "--project",
        live_gate.workflow["project_path"],
    ]


@pytest.mark.parametrize("reason", ["secret-scan", "zero-worker-containers", "hold"])
def test_freeze_never_publishes_failed_or_held_run(live_gate, reason: str) -> None:
    live_gate.reasons.append(reason)
    bootstrap.freeze(live_gate, live_gate.evidence)
    assert live_gate.evidence._assertions["frozen-bootstrap-index"]["status"] == "fail"
    assert not live_gate.options.evidence_out.with_suffix(".index.json").exists()


@pytest.mark.parametrize("assertion", sorted(set(bootstrap.REQUIRED) - {"frozen-bootstrap-index"}))
def test_freeze_never_publishes_before_all_prerequisite_assertions(live_gate, assertion) -> None:
    del live_gate.evidence._assertions[assertion]
    bootstrap.freeze(live_gate, live_gate.evidence)
    assert live_gate.evidence._assertions["frozen-bootstrap-index"]["status"] == "fail"
    assert not live_gate.options.evidence_out.with_suffix(".index.json").exists()


def _execute_prepared_bootstrap(gate, monkeypatch, *, after_run=None):
    import run_factory_gate as runner

    # Use the passing live-shaped run, then exercise the real orchestrator final checks.
    gate.reuse_recorded = False
    assertions = copy.deepcopy(gate.evidence._assertions)
    assertions.pop("zero-worker-containers", None)
    details = copy.deepcopy(gate.evidence.details)

    def prepare(options, evidence, *args, **kwargs):
        gate.options = options
        gate.evidence = evidence
        evidence.raw_writer = gate.raw_write
        evidence._assertions = copy.deepcopy(assertions)
        evidence.details.update(details)
        return gate

    def run(gate):
        if after_run is not None:
            after_run(gate)

    spec = runner.ScenarioSpec(
        required=bootstrap.required_assertions,
        run=run,
        finalize=bootstrap.finalize,
        need_relay=False,
        prepare=prepare,
    )
    monkeypatch.setattr(runner, "_gateway_prerequisites", lambda options: None)
    return runner._execute(
        gate.options,
        engine_factory=lambda context: pytest.fail("unexpected engine"),
        scenarios={"manual-bootstrap": spec},
        prepare=prepare,
    )


def test_execute_secret_hold_does_not_publish_index(live_gate, tmp_path, monkeypatch) -> None:
    token = "registered-bootstrap-secret-0123456789"
    token_file = tmp_path / "gateway-token"
    token_file.write_text(token, encoding="utf-8")
    live_gate.options = replace(live_gate.options, gateway_token_file=token_file)
    reuse_before = len(_read(live_gate.campaign.path)["reuse"])
    document, code = _execute_prepared_bootstrap(
        live_gate,
        monkeypatch,
        after_run=lambda gate: gate.raw_write("leak.txt", token),
    )
    assert code != EXIT_PASS
    assert document["verdict"] == "hold"
    assert "secret-scan" in live_gate.reasons
    assert not live_gate.options.evidence_out.with_suffix(".index.json").exists()
    reuse = _read(live_gate.campaign.path)["reuse"]
    assert len(reuse) == reuse_before + 1
    assert reuse[-1]["reasons"] == live_gate.reasons


@pytest.mark.parametrize("failure", ["hold", "abort"])
def test_execute_scenario_did_not_pass_does_not_publish_index(
    live_gate,
    monkeypatch,
    failure,
) -> None:
    def stop(gate):
        getattr(gate.evidence, failure)("deliberate scenario stop")

    reuse_before = len(_read(live_gate.campaign.path)["reuse"])
    document, code = _execute_prepared_bootstrap(live_gate, monkeypatch, after_run=stop)
    assert code != EXIT_PASS
    assert document["verdict"] != "pass"
    assert "scenario-did-not-pass" in live_gate.reasons
    assert not live_gate.options.evidence_out.with_suffix(".index.json").exists()
    reuse = _read(live_gate.campaign.path)["reuse"]
    assert len(reuse) == reuse_before + 1
    assert reuse[-1]["reasons"] == live_gate.reasons


def test_identical_evidence_out_rerun_keeps_index_bytes(live_gate, monkeypatch) -> None:
    reuse_before = len(_read(live_gate.campaign.path)["reuse"])
    _, code = _execute_prepared_bootstrap(live_gate, monkeypatch)
    assert code == EXIT_PASS
    assert len(_read(live_gate.campaign.path)["reuse"]) == reuse_before + 1
    index = live_gate.options.evidence_out.with_suffix(".index.json")
    original = index.read_bytes()
    # Transient calibration records are intentionally not part of the frozen index.
    live_gate.calibration["digests"] = {"rerun": "changed"}
    live_gate.evidence._assertions.pop("frozen-bootstrap-index")
    _, code = _execute_prepared_bootstrap(live_gate, monkeypatch)
    assert code == EXIT_PASS
    assert index.read_bytes() == original
    assert len(_read(live_gate.campaign.path)["reuse"]) == reuse_before + 2


def test_conflicting_index_is_retained(live_gate, monkeypatch) -> None:
    index = live_gate.options.evidence_out.with_suffix(".index.json")
    original = b'{"existing": "different immutable index"}\n'
    index.write_bytes(original)
    reuse_before = len(_read(live_gate.campaign.path)["reuse"])
    document, code = _execute_prepared_bootstrap(live_gate, monkeypatch)
    assert code == EXIT_FAIL
    assert _assertion(document, "frozen-bootstrap-index")["status"] == "fail"
    assert index.read_bytes() == original
    reuse = _read(live_gate.campaign.path)["reuse"]
    assert len(reuse) == reuse_before + 1
    assert reuse[-1]["reasons"] == live_gate.reasons


def test_bootstrap_observed_pr_retains_only_compared_fields(live_gate) -> None:
    observed = live_gate.evidence._assertions["current-head-open-pr"]["observed"]["current"]
    assert set(observed) == {"state", "merged", "head", "changed_paths", "check_runs", "reviews"}
    assert set(observed["head"]) == {"sha"}
    for check in observed["check_runs"]:
        assert set(check) == {"id", "name", "head_sha", "app", "status", "conclusion"}
        assert set(check["app"]) == {"slug"}
    for review in observed["reviews"]:
        assert set(review) == {"id", "commit_id", "state"}
    assert "private" not in json.dumps(observed)


def test_main_same_evidence_out_rerun_keeps_index_bytes(campaign_root: Path) -> None:
    argv = _argv(campaign_root, "identical-main-rerun")
    index = campaign_root / "identical-main-rerun.index.json"
    assert main(argv) == EXIT_PASS
    original = index.read_bytes()
    assert main(argv) == EXIT_PASS
    assert index.read_bytes() == original


def test_install_receipt_requires_version_without_bound_version(producer_receipts) -> None:
    paths, digest = producer_receipts
    assert bootstrap.install_receipt_problems(paths, digest)[0] == []
    receipt = _read(paths[1])
    receipt["installed_version"] = None
    paths[1].write_text(json.dumps(receipt), encoding="utf-8")
    assert bootstrap.install_receipt_problems(paths, digest)[0]


@pytest.mark.parametrize("failure", ["head-race", "bad-shebang"])
def test_shared_reader_hold_records_failure_and_continues(
    live_gate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    from factory_gate_host_reads import pr_snapshot

    gate = live_gate
    evidence = EvidenceBuilder(
        scenario="manual-bootstrap", mode="live", required=bootstrap.REQUIRED
    )
    gate.reasons = []
    if failure == "head-race":
        reads = 0

        def get(arguments: list[str]) -> Any:
            nonlocal reads
            if "?" in arguments[-1]:
                return []
            reads += 1
            return {"state": "open", "head": {"sha": str(reads)}}

        monkeypatch.setattr(gate, "current_pr", lambda: pr_snapshot("owner/repo", 17, get))
        failed = "current-head-open-pr"
    else:
        entry = tmp_path / "enginery"
        entry.write_text("#!/usr/bin/env python3\n")
        gate.binding["executable"] = str(entry)
        monkeypatch.setattr(gate, "project_snapshot", lambda: gate.python())
        monkeypatch.setattr(
            bootstrap,
            "_recover_operator",
            lambda gate: gate.operator("status", tmp_path / "ledger.sqlite"),
        )
        failed = "human-action-decision"
    bootstrap.run(gate, evidence)
    assert evidence._assertions[failed]["status"] == "fail"
    assert failed in gate.reasons
    if failure == "bad-shebang":
        assert evidence._assertions["external-operator-recovery"]["status"] == "fail"
    bootstrap.finalize(gate, evidence)
    assert set(evidence._assertions) == set(bootstrap.REQUIRED) - {"frozen-bootstrap-index"}
    assert gate.reasons


@pytest.mark.parametrize(
    ("bound_version", "stdout", "returncode", "passed"),
    [
        ("0.6.0", "enginery 0.6.01\n", 0, False),
        ("0.6.0", "0.6.0\n", 0, False),
        ("0.6.0", "enginery 0.6.0 extra\n", 0, False),
        ("", "enginery 0.6.0\n", 0, False),
        (None, "enginery 0.6.0\n", 0, False),
        ("0.6.0", "  enginery 0.6.0\n", 0, True),
        ("0.6.0", "enginery 0.6.0\n", 1, False),
    ],
)
def test_live_installed_version_requires_exact_cli_output(
    live_gate, monkeypatch, bound_version, stdout, returncode, passed
) -> None:
    identity = live_gate.binding["installed_distribution"]
    if bound_version is None:
        identity.pop("version")
    else:
        identity["version"] = bound_version
    monkeypatch.setattr(
        live_gate,
        "execute",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, returncode, stdout, ""),
    )
    evidence = EvidenceBuilder(
        scenario="manual-bootstrap", mode="live", required=bootstrap.REQUIRED
    )
    bootstrap.run(live_gate, evidence)
    assertion = evidence._assertions["installed-wheel-identity"]
    assert assertion["status"] == ("pass" if passed else "fail")
    if not passed:
        assert (
            "installed executable version does not match the bound distribution"
            in assertion["observed"]["problems"]
        )


def test_reuse_fallback_records_final_reasons_when_freeze_raises(live_gate, monkeypatch) -> None:
    def interrupted(*args, **kwargs):
        raise RuntimeError("freezing interrupted")

    monkeypatch.setattr(bootstrap, "freeze", interrupted)
    reuse_before = len(_read(live_gate.campaign.path)["reuse"])
    document, code = _execute_prepared_bootstrap(live_gate, monkeypatch)
    assert code != EXIT_PASS
    assert document["verdict"] != "pass"
    assert "frozen-bootstrap-index" in live_gate.reasons
    reuse = _read(live_gate.campaign.path)["reuse"]
    assert len(reuse) == reuse_before + 1
    assert reuse[-1]["reasons"] == live_gate.reasons


@pytest.fixture
def index_gate(tmp_path: Path):
    gate = bootstrap.BootstrapGate(parse_options(_argv(tmp_path)))
    gate.binding = {"qualification_target": "synthetic"}
    gate.calibration = {"set_sha256": "fixture-calibration", "summary": {}}
    try:
        yield gate
    finally:
        gate.cleanup()


def test_frozen_index_identical_publication_does_not_write(index_gate, monkeypatch) -> None:
    first = bootstrap._freeze_index(index_gate, index_gate.calibration)
    path = Path(first["path"])
    before = path.stat()
    write_text = Path.write_text

    def reject_rewrite(target, *args, **kwargs):
        if target == path:
            pytest.fail("identical frozen index was rewritten")
        return write_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", reject_rewrite)
    assert bootstrap._freeze_index(index_gate, index_gate.calibration) == first
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not list(path.parent.glob("index-*.tmp"))


@pytest.mark.parametrize("identical", [False, True])
def test_frozen_index_concurrent_publication_preserves_winner(
    index_gate, monkeypatch, identical
) -> None:
    first = bootstrap._freeze_index(index_gate, index_gate.calibration)
    path = Path(first["path"])
    winner = path.read_bytes() if identical else b'{"concurrent": "different index"}\n'
    path.unlink()
    exists = Path.exists
    raced = False

    def create_after_absence_check(target):
        nonlocal raced
        result = exists(target)
        if target == path and not result and not raced:
            raced = True
            target.write_bytes(winner)
        return result

    monkeypatch.setattr(Path, "exists", create_after_absence_check)
    if identical:
        assert bootstrap._freeze_index(index_gate, index_gate.calibration) == first
    else:
        with pytest.raises(ValueError, match="already exists with different contents"):
            bootstrap._freeze_index(index_gate, index_gate.calibration)
    assert raced
    assert path.read_bytes() == winner
    assert not list(path.parent.glob("index-*.tmp"))


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_frozen_index_failed_publication_removes_temporary_file(
    index_gate, monkeypatch, error_type
) -> None:
    path = index_gate.options.evidence_out.with_suffix(".index.json")
    temporary = []

    def fail_publication(source, target):
        source = Path(source)
        assert Path(target) == path
        assert not path.exists()
        assert source.parent == path.parent
        assert _read(source)["campaign_id"] == index_gate.options.campaign_id
        temporary.append(source)
        raise error_type("interrupted index publication")

    monkeypatch.setattr(bootstrap.os, "link", fail_publication)
    with pytest.raises(error_type, match="interrupted index publication"):
        bootstrap._freeze_index(index_gate, index_gate.calibration)
    assert len(temporary) == 1
    assert not temporary[0].exists()
    assert not path.exists()
    assert not list(path.parent.glob("index-*.tmp"))
