"""Reuse a completed qualification campaign without launching another worker.

Live mode reads GitHub through the host gh and the project through the bound installed
interpreter. Local mode reuses the protocol scenario's retained deterministic project;
it does not manufacture a completed campaign or a human decision.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from factory_gate_auth import engine_creates, engine_time, later
from factory_gate_campaign import Campaign, campaign_binding, configuration, sha256
from factory_gate_context import (
    GateHoldError,
    GateOptions,
    evaluator_identity,
    host_identity,
    subject_identity,
)
from factory_gate_evidence import EvidenceBuilder
from factory_gate_project_probe import valid_human_decision

PROBE = Path(__file__).with_name("factory_gate_bootstrap_probe.py").resolve()

SCENARIO = "manual-bootstrap"
REQUIRED = {
    "campaign-evidence-reused": (
        "both harnesses' successful terminal slots match the immutable binding"
    ),
    "current-head-open-pr": "the designated PR is open, unmerged and unchanged",
    "human-action-decision": "a real-terminal human decision binds this run and head",
    "observed-changed-behavior": "independent current-head behavior and calibration pass",
    "raw-and-product-recovery": "raw originals and product artifacts recover byte-exactly",
    "external-operator-recovery": "workflow-free operator commands recover without an agent",
    "installed-wheel-identity": "the installation and platform receipts match the bound wheel",
    "known-defect-calibration": "the frozen owned fault set demonstrates all four outcomes",
    "frozen-bootstrap-index": "journeys, invariants, runner limits and calibration are frozen",
    "zero-worker-containers": "engine events show zero worker containers created by bootstrap",
}


def required_assertions(options: GateOptions) -> dict[str, str]:
    return dict(REQUIRED)


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object: {path}")
    return value


def _head(pr: Mapping[str, Any]) -> Any:
    value = pr.get("head")
    return value.get("sha") if isinstance(value, dict) else pr.get("head_sha")


def pr_problems(current: Mapping[str, Any], recorded: Mapping[str, Any], head: str) -> list[str]:
    problems = []
    if current.get("state", "").lower() != "open" or current.get("merged") is not False:
        problems.append("PR is closed or merged")
    if not head or _head(current) != head:
        problems.append("PR head is stale")
    paths = current.get("changed_paths")
    if not paths or sorted(paths) != sorted(recorded.get("changed_paths", [])):
        problems.append("changed paths are missing or stale")
    checks = current.get("check_runs")
    if not checks or any(
        item.get("head_sha") != head
        or item.get("status") != "completed"
        or item.get("conclusion") not in {"success", "neutral", "skipped"}
        for item in checks
    ):
        problems.append("check-runs are absent, failed or not on the verified head")

    def identity(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("id"),
            item.get("name"),
            item.get("head_sha"),
            item.get("app", {}).get("slug"),
        )

    if {identity(item) for item in checks or []} != {
        identity(item) for item in recorded.get("check_runs", [])
    }:
        problems.append("check_runs snapshot drift")
    recorded_checks = {identity(item): item for item in recorded.get("check_runs", [])}
    for item in checks or []:
        previous = recorded_checks.get(identity(item), {})
        if previous.get("status") == "completed" and (
            previous.get("conclusion") != item.get("conclusion")
        ):
            problems.append("terminal check-run conclusion changed")
        elif previous.get("status") in {"queued", "in_progress"} and (
            item.get("conclusion") != "success"
        ):
            problems.append("nonterminal check-run did not progress to success")
    if sorted(
        current.get("reviews", []), key=lambda item: json.dumps(item, sort_keys=True)
    ) != sorted(recorded.get("reviews", []), key=lambda item: json.dumps(item, sort_keys=True)):
        problems.append("reviews snapshot drift")
    return problems


def human_decisions(snapshot: Mapping[str, Any], run_id: str, head: str) -> list[dict[str, Any]]:
    return [
        item for item in snapshot.get("decisions", []) if valid_human_decision(item, run_id, head)
    ]


def install_receipt_problems(
    paths: tuple[Path, ...], wheel_digest: str, installed_version: str | None = None
) -> tuple[list[str], list[dict[str, Any]]]:
    problems: list[str] = []
    receipts = []
    platforms: set[str] = set()
    for path in paths:
        try:
            value = _document(path)
            artifact = value.get("artifact", {})
            host = value.get("platform", {})
            platform_name = host.get("system")
            if platform_name == "Linux" and host.get("distribution", {}).get("ID") == "ubuntu":
                platform_name = "Ubuntu"
            commands = value.get("commands")
            entry = Path(value.get("entry_point") or "")
            interpreter = Path(value.get("python", {}).get("executable") or "")
            source = Path(__file__).resolve().parents[1]
            separate_install = (
                entry.is_absolute()
                and entry.parent == interpreter.parent
                and entry.parent.name == "bin"
                and not entry.is_relative_to(source)
            )
            if (
                value.get("schema_version") != 1
                or value.get("outcome") != "pass"
                or value.get("exit_code") != 0
                or value.get("not_run")
                or artifact.get("kind") != "wheel"
                or artifact.get("sha256") != wheel_digest
                or not value.get("installed_version")
                or not separate_install
                or (
                    installed_version is not None
                    and value.get("installed_version") != installed_version
                )
                or not value.get("python", {}).get("version")
                or not isinstance(commands, list)
                or not all(
                    isinstance(item.get("exit_code"), int)
                    and isinstance(item.get("argv"), list)
                    and item["argv"]
                    for item in commands
                )
                or not any(
                    item.get("argv") == [str(entry), "--version"] and item.get("exit_code") == 0
                    for item in commands
                )
                or platform_name not in {"Darwin", "Ubuntu"}
            ):
                problems.append(f"invalid or mismatching install receipt: {path}")
            else:
                platforms.add(platform_name)
            receipts.append({"path": str(path.resolve()), "sha256": sha256(path), "receipt": value})
        except (OSError, ValueError, TypeError, AttributeError) as error:
            problems.append(f"missing or malformed install receipt {path}: {error}")
    for platform_name in ("Darwin", "Ubuntu"):
        if platform_name not in platforms:
            problems.append(f"missing {platform_name} wheel install receipt")
    return problems, receipts


class BootstrapGate:
    def __init__(self, options: GateOptions, evidence: EvidenceBuilder | None = None) -> None:
        self.options = options
        self.root = Path(tempfile.mkdtemp(prefix="enginery-bootstrap-gate-")).resolve()
        self.campaign = Campaign(options)
        self.config = configuration(options)
        self.commands: list[str] = []
        self.effects: dict[str, Any] = {"hosted_inference_calls": 0, "worker_sessions": 0}
        self.workflow: dict[str, Any] = {}
        self.binding: dict[str, Any] = {}
        self.reasons: list[str] = []
        self.reuse_recorded = False
        self.window_start: str | None = None
        self.calibration: dict[str, Any] = {}
        self.evidence = evidence
        if evidence is not None:
            evidence.raw_writer = self.raw_write

    def record_reuse(self) -> None:
        """Persist this invocation's final reasons once, after index freezing."""
        if not self.reuse_recorded and self.campaign.path.is_file():
            self.campaign.record_reuse(self.campaign.read().get("evidence", {}), self.reasons)
            self.reuse_recorded = True

    def raw_write(self, name: str, value: Any) -> str:
        path = self.options.raw_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, indent=2, sort_keys=True, default=str)
        )
        path.write_text(text, encoding="utf-8")
        return f"{self.options.raw_dir.name}/{name}"

    def identities(self, required: dict[str, str]) -> dict[str, Any]:
        return {
            "subject": subject_identity(self.options.subject_commit),
            "build": self.binding.get("installed_distribution"),
            "config": {"campaign_id": self.options.campaign_id, "reuse_evidence": True},
            "host": host_identity(),
            "engine": self.binding.get("engine_kind"),
            "image": self.binding.get("runner_image"),
            "profile": None,
            "evaluator": evaluator_identity(required),
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if not self.options.live:
            env.update(self.workflow.get("environment", {}))
        else:
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
        return env

    def python(self) -> list[str]:
        if not self.options.live:
            return [sys.executable]
        from factory_gate_host_reads import bound_interpreter

        return [bound_interpreter(self.binding["executable"]), "-I"]

    def execute(
        self, arguments: list[str], *, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(shlex.join(arguments))
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=self.root if self.root.exists() else self.options.evidence_out.parent,
            env=self.environment(),
        )
        self.raw_write(
            f"commands/{len(self.commands):03}.json",
            {
                "argv": arguments,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
        return result

    def engine_cli(self, *arguments: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        prefix = ["docker"]
        if self.options.docker_context:
            prefix += ["--context", self.options.docker_context]
        return self.execute([*prefix, *arguments], timeout=timeout)

    def project_snapshot(self) -> dict[str, Any]:
        settings = {key: self.workflow[key] for key in ("database", "artifact_root", "run_id")}
        settings["artifact_digests"] = self.workflow.get("artifact_digests", [])
        stores = {}
        for key, record in self.campaign.read()["evidence"].items():
            workflow = record["workflow"]
            if "artifact_root" in workflow:
                stores[key] = {
                    "artifact_root": workflow["artifact_root"],
                    "artifact_digests": workflow.get("artifact_digests", []),
                }
            elif key.endswith(":completed-workflow"):
                raise ValueError("completed workflow has no retained product artifact store")
        settings["campaign_stores"] = stores
        process = self.execute([*self.python(), str(PROBE), "project", json.dumps(settings)])
        if process.returncode:
            raise ValueError(f"product evidence read failed: {process.stderr[-1000:]}")
        return dict(json.loads(process.stdout))

    def current_pr(self) -> dict[str, Any]:
        if not self.options.live:
            # This is the protocol's retained stand-in remote, not a new PR receipt.
            pr = _document(Path(self.workflow["pr_path"]))
            repository = self.workflow["workspace_path"]
            result = self.execute(
                [
                    "git",
                    "-C",
                    repository,
                    "diff",
                    "--name-only",
                    "-z",
                    self.workflow["base_revision"],
                    self.workflow["verified_head"],
                    "--",
                ]
            )
            if result.returncode:
                raise ValueError("retained project cannot resolve the verified diff")
            pr["changed_paths"] = [item for item in result.stdout.split("\0") if item]
            prefix = f"repos/{self.binding['repository']}"
            number = self.workflow["pr_number"]
            head = _head(pr)
            for field, endpoint in (
                ("check_runs", f"{prefix}/commits/{head}/check-runs"),
                ("reviews", f"{prefix}/pulls/{number}/reviews"),
            ):
                result = self.execute(["gh", "api", endpoint])
                if result.returncode:
                    raise ValueError(f"retained stand-in remote read failed: {endpoint}")
                document = json.loads(result.stdout)
                pr[field] = document["check_runs"] if field == "check_runs" else document
            return pr
        from factory_gate_host_reads import pr_snapshot

        def get(arguments: list[str]) -> Any:
            result = self.execute(arguments, timeout=60)
            self.effects["github_reads"] = self.effects.get("github_reads", 0) + 1
            if result.returncode:
                raise ValueError(f"host gh read failed: {result.stderr[-500:]}")
            return json.loads(result.stdout)

        return pr_snapshot(self.binding["repository"], self.workflow["pr_number"], get)

    def operator(self, verb: str, database: Path, *extra: str) -> dict[str, Any]:
        arguments = ["operator", verb, "--database", str(database), *extra]
        self.commands.append(shlex.join(["enginery", *arguments]))
        entry_point = str(self.binding.get("executable", ""))
        process = self.execute(
            [
                *self.python(),
                str(PROBE),
                "operator",
                json.dumps(arguments),
                entry_point,
                "live" if self.options.live else "local",
            ]
        )
        if process.returncode:
            raise ValueError(f"workflow-free operator guard failed: {process.stderr[-1000:]}")
        result = json.loads(process.stdout)
        if result["code"] != 0 or result["workflow_imports"]:
            raise ValueError(f"operator {verb} failed: {result}")
        result["report"] = json.loads(result["stdout"])
        return dict(result)


def prepare(
    options: GateOptions,
    evidence: EvidenceBuilder | None = None,
    scrubber: object = None,
    engine_factory: object = None,
    *,
    need_relay: bool = False,
) -> BootstrapGate:
    return BootstrapGate(options, evidence)


def _record(
    gate: BootstrapGate,
    evidence: EvidenceBuilder,
    assertion_id: str,
    passed: bool,
    observed: Any,
    start: int = 0,
) -> None:
    if not passed:
        gate.reasons.append(assertion_id)
    evidence.assertion(
        assertion_id,
        passed=passed,
        commands=gate.commands[start:] or ["read retained campaign evidence"],
        observed=observed,
        expected=REQUIRED[assertion_id],
        observer={"campaign": str(gate.campaign.path), "mode": gate.options.mode},
    )


def _recover_operator(gate: BootstrapGate) -> dict[str, Any]:
    database = Path(gate.workflow["database"])
    artifacts = Path(gate.workflow["artifact_root"])
    if not database.is_file() or not artifacts.is_dir():
        raise ValueError("campaign project ledger or artifacts are missing")
    initial = gate.operator("status", database, "--artifacts", str(artifacts))
    if initial["report"].get("running_workers", 0):
        raise ValueError("operator recovery requires the campaign's workers to be gone")
    reports = {"initial": initial}
    backup = gate.root / "backup"
    restored = gate.root / "restored" / "ledger.sqlite"
    restored.parent.mkdir()
    if restored.resolve() == database.resolve():
        raise ValueError("restore target must differ from the live ledger")
    reports["restore_target_differs"] = True
    if list(database.parent.glob("*pre-restore-*")):
        raise ValueError("live ledger has a pre-restore sidecar")
    restored_artifacts = gate.root / "restored" / "artifacts"
    paused = initial["report"].get("paused", False)
    try:
        reports["pause"] = gate.operator("pause", database)
        reports["resume"] = gate.operator("resume", database)
        reports["drain"] = gate.operator("drain", database, "--wait-seconds", "0")
        reports["lift"] = gate.operator("resume", database)
        image = gate.binding.get("runner_image") or "sha256:" + "1" * 64
        extra = ["--image", image]
        if gate.options.docker_context:
            extra += ["--docker-context", gate.options.docker_context]
        reports["select_runner"] = gate.operator("select-runner", database, *extra)
        reports["pause_backup"] = gate.operator("pause", database)
        reports["backup"] = gate.operator(
            "backup", database, "--output", str(backup), "--artifacts", str(artifacts)
        )
        reports["restore"] = gate.operator(
            "restore", restored, "--backup", str(backup), "--artifacts", str(restored_artifacts)
        )
        reports["original_verify"] = gate.operator(
            "verify", database, "--artifacts", str(artifacts), "--json"
        )
        reports["restored_verify"] = gate.operator(
            "verify", restored, "--artifacts", str(restored_artifacts), "--json"
        )
        # Restore records its own pause event, so whole database hashes must differ.
        # Compare the retained domain history instead, excluding operator control events.
        result = gate.execute(
            [
                *gate.python(),
                str(PROBE),
                "compare",
                str(database),
                str(artifacts),
                str(restored),
                str(restored_artifacts),
            ]
        )
        if result.returncode:
            raise ValueError(f"restored equivalence read failed: {result.stderr[-1000:]}")
        reports["equivalence"] = json.loads(result.stdout)
        if not reports["equivalence"]["equivalent"]:
            raise ValueError("restored ledger/artifacts differ from the original")
        sidecars = list(database.parent.glob("*pre-restore-*"))
        reports["live_pre_restore_sidecars"] = [str(path) for path in sidecars]
        if sidecars:
            raise ValueError("restore created a live ledger pre-restore sidecar")
    finally:
        reports["restore_pause_state"] = gate.operator("pause" if paused else "resume", database)
    return reports


def _freeze_index(gate: BootstrapGate, calibration: dict[str, Any]) -> dict[str, Any]:
    """Publish an immutable index without rewriting an identical existing file."""
    from check_import_boundaries import LAYER_ALLOWED_IMPORTS, find_violations
    from enginery.domain.runner import RunnerProfile

    manifest = gate.campaign.read()
    journeys = {
        key: {"path": record["path"], "sha256": sha256(Path(record["path"]))}
        for key, record in manifest["protocol_evidence"].items()
    }
    for name in gate.config.get("baseline_evidence", []):
        path = Path(name)
        if _document(path).get("verdict") != "pass":
            raise ValueError(f"baseline journey did not pass: {path}")
        journeys[str(path)] = {"path": str(path.resolve()), "sha256": sha256(path)}
    layers = {name: [str(item) for item in find_violations(name)] for name in LAYER_ALLOWED_IMPORTS}
    if any(layers.values()):
        raise ValueError("architecture boundary-layer violations")
    boundary = None
    boundary_path = gate.config.get("boundary_evidence")
    if boundary_path:
        path = Path(boundary_path)
        document = _document(path)
        image = document.get("identities", {}).get("image", {})
        if (
            document.get("scenario") != "runner-boundary"
            or document.get("verdict") != "pass"
            or image.get("image_id") != gate.binding.get("runner_image")
            or not document.get("assertions")
            or any(
                item.get("status") != "pass" or not item.get("ran")
                for item in document["assertions"]
            )
        ):
            raise ValueError(
                "runner boundary evidence is missing, failed or bound to another image"
            )
        boundary = {"path": str(path.resolve()), "sha256": sha256(path)}
    elif gate.options.live:
        raise ValueError("live bootstrap requires exact-image runner-boundary evidence")
    profile = gate.workflow.get("runner_profile")
    if gate.options.live and not profile:
        raise ValueError("live workflow is missing its observed runner resource profile")
    index = {
        "schema_version": 1,
        "campaign_id": gate.options.campaign_id,
        "qualification_target": gate.binding["qualification_target"],
        "binding": gate.binding,
        "baseline_journeys": journeys,
        "invariants": {
            "runner_boundary": boundary,
            "boundary_layers": layers,
            "live_boundary_qualified": bool(boundary),
        },
        "resource_bounds": {
            "session_deadline_seconds": gate.options.session_deadline_seconds,
            "maximum_worker_sessions": 4,
            "concurrency": 1,
            "profile": profile or RunnerProfile().to_state(),
        },
        "calibration": {"set_sha256": calibration["set_sha256"], "summary": calibration["summary"]},
    }
    path = gate.options.evidence_out.with_suffix(".index.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(index, indent=2, sort_keys=True, default=str) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError("frozen bootstrap index already exists with different contents")
    else:
        descriptor, name = tempfile.mkstemp(prefix="index-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path)
            except FileExistsError:
                if path.read_text(encoding="utf-8") != text:
                    raise ValueError(
                        "frozen bootstrap index already exists with different contents"
                    ) from None
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(name)
    return {"path": str(path.resolve()), "sha256": sha256(path), "index": index}


def run(gate: BootstrapGate, evidence: EvidenceBuilder | None = None) -> None:
    evidence = evidence or gate.evidence
    if evidence is None:
        raise ValueError("bootstrap requires an evidence builder")
    gate.evidence = evidence
    evidence.raw_writer = gate.raw_write
    gate.window_start = engine_time(gate) if gate.options.live else None
    manifest: dict[str, Any] = {}
    try:
        if not gate.options.reuse_evidence:
            raise ValueError("manual-bootstrap requires --reuse-evidence")
        if not gate.campaign.path.is_file():
            raise ValueError("campaign is missing; run both protocol scenarios first")
        gate.binding = campaign_binding(gate.options)
        gate.campaign.bind(gate.binding)
        manifest = gate.campaign.read()
        problems = gate.campaign.verify_evidence()
        for harness, record in manifest.get("protocol_evidence", {}).items():
            if record.get("verdict") != "pass":
                continue
            try:
                protocol = _document(Path(record["path"]))
            except (OSError, ValueError, KeyError, TypeError) as error:
                problems.append(f"{harness}: protocol qualification_target unavailable: {error}")
                continue
            details = protocol.get("details")
            identities = protocol.get("identities")
            if (
                not isinstance(details, dict)
                or not isinstance(identities, dict)
                or details.get("qualification_target") != gate.binding["qualification_target"]
                or identities.get("qualification_target") != gate.binding["qualification_target"]
            ):
                problems.append("protocol qualification_target differs from campaign binding")
        for slot_key, record in manifest.get("evidence", {}).items():
            retained = _document(Path(record["path"]))
            if retained != record.get("workflow"):
                problems.append(
                    f"{slot_key}: retained slot document differs from manifest metadata"
                )
            raw = record.get("raw_originals", {})
            absent_cancelled_output = (
                slot_key.endswith(":cancellation") and not raw and bool(retained.get("raw_absence"))
            )
            if raw != retained.get("raw_originals", {}) or (
                not raw and not absent_cancelled_output
            ):
                problems.append(f"{slot_key}: raw-original references are missing or stale")
        key = manifest.get("designated_workflow")
        if key not in {"omp:completed-workflow", "claude:completed-workflow"}:
            problems.append("designated completed workflow is missing")
        else:
            record = manifest["evidence"].get(key, {})
            gate.workflow = dict(record.get("workflow", {}))
            if not gate.workflow:
                problems.append("designated workflow details are missing")
        _record(
            gate,
            evidence,
            "campaign-evidence-reused",
            not problems,
            {"problems": problems, "slots": manifest.get("slots")},
        )
    except (OSError, ValueError, KeyError, GateHoldError) as error:
        _record(gate, evidence, "campaign-evidence-reused", False, {"error": str(error)})

    snapshot: dict[str, Any] = {}
    head = str(gate.workflow.get("verified_head", ""))
    run_id = str(gate.workflow.get("run_id", ""))
    steps = (
        "current-head-open-pr",
        "human-action-decision",
        "observed-changed-behavior",
        "raw-and-product-recovery",
    )
    for assertion in steps:
        start = len(gate.commands)
        try:
            if not gate.workflow or "campaign-evidence-reused" in gate.reasons:
                raise ValueError("campaign evidence is missing or stale")
            if assertion == "current-head-open-pr":
                current = gate.current_pr()
                problems = pr_problems(current, gate.workflow["pr"], head)
                _record(
                    gate,
                    evidence,
                    assertion,
                    not problems,
                    {
                        "problems": problems,
                        "current": {
                            "state": current.get("state"),
                            "merged": current.get("merged"),
                            "head": {"sha": _head(current)},
                            "changed_paths": current.get("changed_paths"),
                            "check_runs": [
                                {
                                    **{
                                        key: item.get(key)
                                        for key in (
                                            "id",
                                            "name",
                                            "head_sha",
                                            "status",
                                            "conclusion",
                                        )
                                    },
                                    "app": {"slug": item.get("app", {}).get("slug")},
                                }
                                for item in current.get("check_runs", [])
                            ],
                            "reviews": [
                                {key: item.get(key) for key in ("id", "commit_id", "state")}
                                for item in current.get("reviews", [])
                            ],
                        },
                    },
                    start,
                )
            elif assertion == "human-action-decision":
                snapshot = gate.project_snapshot()
                decisions = human_decisions(snapshot, run_id, head)
                _record(gate, evidence, assertion, bool(decisions), {"decisions": decisions}, start)
            elif assertion == "observed-changed-behavior":
                if not snapshot:
                    snapshot = gate.project_snapshot()
                behavior = snapshot.get("behavior", {})
                receipt = snapshot.get("receipts", {}).get(behavior.get("receipt_digest"), {})
                calibration_state = snapshot.get("calibration", {})
                calibration_qualified = (
                    calibration_state.get("head_revision") == head
                    and calibration_state.get("qualified") is True
                    and not calibration_state.get("holds")
                    and bool(calibration_state.get("calibration_receipt_digest"))
                )
                passed = (
                    behavior.get("result") == "pass"
                    and not behavior.get("holds")
                    and behavior.get("head_revision") == head
                    and receipt.get("head_revision") == head
                    and receipt.get("result") == "pass"
                    and bool(receipt.get("assertion_outcomes"))
                    and all(item.get("result") == "pass" for item in receipt["assertion_outcomes"])
                    and bool(behavior.get("observation_receipt_digests"))
                    and calibration_qualified
                )
                _record(
                    gate,
                    evidence,
                    assertion,
                    passed,
                    {
                        "behavior": behavior,
                        "receipt": receipt,
                        "calibration": snapshot.get("calibration"),
                        **(
                            {
                                "repair": shlex.join(
                                    [
                                        "enginery",
                                        "calibrate",
                                        run_id,
                                        "--project",
                                        gate.workflow["project_path"],
                                    ]
                                )
                            }
                            if not calibration_qualified
                            else {}
                        ),
                    },
                    start,
                )
            else:
                if not snapshot:
                    snapshot = gate.project_snapshot()
                problems = gate.campaign.verify_evidence()
                raw = {
                    name: digest
                    for record in manifest.get("evidence", {}).values()
                    for name, digest in record.get("raw_originals", {}).items()
                }
                passed = not problems and bool(raw) and bool(snapshot.get("verified_artifacts"))
                _record(
                    gate,
                    evidence,
                    assertion,
                    passed,
                    {
                        "problems": problems,
                        "raw_originals": raw,
                        "verified_artifacts": snapshot.get("verified_artifacts"),
                        "verified_stores": snapshot.get("verified_stores"),
                    },
                    start,
                )
        except (
            GateHoldError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            _record(gate, evidence, assertion, False, {"error": str(error)}, start)

    start = len(gate.commands)
    try:
        if "campaign-evidence-reused" in gate.reasons:
            raise ValueError("operator recovery refused: campaign evidence is stale or missing")
        reports = _recover_operator(gate)
        _record(gate, evidence, "external-operator-recovery", True, reports, start)
    except (GateHoldError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        _record(gate, evidence, "external-operator-recovery", False, {"error": str(error)}, start)

    identity = gate.binding.get("installed_distribution") or {}
    problems, receipts = install_receipt_problems(
        gate.options.install_receipts,
        str(gate.binding.get("wheel_sha256", "")),
        identity.get("version") if gate.options.live else None,
    )
    if not gate.options.live and not gate.options.install_receipts:
        problems = []
    version_report: dict[str, Any] = {}
    try:
        argv = (
            [gate.binding["executable"]]
            if gate.options.live
            else [sys.executable, "-m", "enginery.cli.main"]
        )
        version = gate.execute([*argv, "--version"], timeout=30)
        version_report = {
            "exit_code": version.returncode,
            "stdout": version.stdout,
            "stderr": version.stderr,
        }
        bound_version = identity.get("version")
        if version.returncode or (
            gate.options.live
            and (not bound_version or version.stdout.strip() != f"enginery {bound_version}")
        ):
            problems.append("installed executable version does not match the bound distribution")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        problems.append(f"installed executable could not run: {error}")
    _record(
        gate,
        evidence,
        "installed-wheel-identity",
        bool(identity) and not problems,
        {
            "identity": identity,
            "version": version_report,
            "receipts": receipts,
            "problems": problems,
            "platform_install_qualification": "required"
            if gate.options.live
            else "not requested (deterministic mode)",
        },
    )
    calibration: dict[str, Any] = {}
    try:
        from enginery.evidence.bootstrap_calibration import (
            CALIBRATION_SET_SHA256,
            load_calibration_set,
        )
        from factory_gate_bootstrap_calibration import run_calibration

        frozen = load_calibration_set()
        calibration = run_calibration(gate.root / "calibration")
        passed = (
            len(frozen["entries"]) >= 4
            and calibration.get("passed") is True
            and calibration.get("set_sha256") == CALIBRATION_SET_SHA256
            and {item["id"] for item in calibration.get("entries", [])}
            == {item["id"] for item in frozen["entries"]}
            and bool(calibration.get("digests"))
            and set(calibration.get("outcomes", []))
            == {"caught", "escaped", "not_injected", "inconclusive"}
            and all(item.get("passed") is True for item in calibration.get("entries", []))
        )
        # Calibration runs in a disposable root; retain its original receipt bytes.
        retained = gate.options.raw_dir / "calibration"
        retained.mkdir(parents=True, exist_ok=True)
        for name, expected in calibration.get("digests", {}).items():
            source = gate.root / "calibration" / name
            if sha256(source) != expected:
                raise ValueError("calibration receipt digest drift")
            target = retained / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for entry in calibration.get("entries", []):
            for receipt in entry.get("receipts", []):
                receipt["path"] = str(retained / Path(receipt["path"]).name)
        _record(
            gate,
            evidence,
            "known-defect-calibration",
            passed,
            {"set": frozen, "execution": calibration},
        )
    except (OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as error:
        _record(gate, evidence, "known-defect-calibration", False, {"error": str(error)})
    gate.calibration = calibration


def finalize(gate: BootstrapGate, evidence: EvidenceBuilder | None = None) -> None:
    evidence = evidence or gate.evidence
    if evidence is None:
        return
    start = len(gate.commands)
    text: str | None = None
    end = None
    try:
        if gate.options.live and gate.window_start is not None:
            end = engine_time(gate)
            # Docker truncates an equal start/end second; include the full ending
            # second rather than permitting an empty interval to prove zero.
            until = later(end, 1) if end else None
            if until:
                text = engine_creates(gate, gate.window_start, until)
        events = (
            [] if text is None else [json.loads(line) for line in text.splitlines() if line.strip()]
        )
        created = [
            event
            for event in events
            if event.get("Type") == "container" and event.get("Action") == "create"
        ]
        # Reject every created container, including an unlabelled worker. Counting
        # only a campaign operation label would miss a regressed launch path.
        passed = not gate.options.live or (text is not None and not created)
        gate.effects["isolated_containers"] = len(created) if text is not None else None
        _record(
            gate,
            evidence,
            "zero-worker-containers",
            passed,
            {
                "start": gate.window_start,
                "end": end,
                "events_readable": text is not None,
                "created": created,
                "local_engine_not_requested": not gate.options.live,
            },
            start,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        _record(gate, evidence, "zero-worker-containers", False, {"error": str(error)}, start)


def freeze(gate: BootstrapGate, evidence: EvidenceBuilder | None = None) -> None:
    """Publish the immutable index only after finalization and the secret scan."""
    evidence = evidence or gate.evidence
    if evidence is None:
        return
    try:
        if gate.reasons:
            raise ValueError("cannot freeze failed bootstrap evidence: " + ", ".join(gate.reasons))
        prerequisites = set(REQUIRED) - {"frozen-bootstrap-index"}
        if any(
            not evidence._assertions.get(key, {}).get("ran")
            or evidence._assertions.get(key, {}).get("status") != "pass"
            for key in prerequisites
        ):
            raise ValueError("cannot freeze before every prerequisite assertion passes")
        index = _freeze_index(gate, gate.calibration)
        evidence.details["bootstrap_index"] = index
        _record(gate, evidence, "frozen-bootstrap-index", True, index)
    except (OSError, ValueError, KeyError) as error:
        _record(gate, evidence, "frozen-bootstrap-index", False, {"error": str(error)})
    gate.record_reuse()
