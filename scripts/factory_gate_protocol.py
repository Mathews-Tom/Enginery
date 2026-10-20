"""Campaign-bound reference harness lifecycle, controls, and retained raw originals."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.work_ports import HarnessTask
from enginery.domain.errors import EngineryError
from enginery.domain.execution_selection import (
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.service import LedgerService
from factory_gate_campaign import Campaign, campaign_binding, sha256
from factory_gate_context import (
    GateHoldError,
    GateOptions,
    evaluator_identity,
    host_identity,
    subject_identity,
)
from factory_gate_evidence import EvidenceBuilder
from factory_gate_project_probe import project_state

REQUIRED = {
    "campaign-binding": "immutable campaign binding matches before any reservation",
    "unsupported-controls": "unsupported native effort and model requests hold with zero launches",
    "completed-workflow": (
        "one worker reaches a current-head verified open PR through the product CLI"
    ),
    "coordinator-recovery": "one killed client is reconciled without another worker",
    "execution-controls": "selected, submitted, observed and unknown controls are distinct",
    "cancellation-reconcile": (
        "canonical cancellation is reconciled and leaves no process or container"
    ),
    "raw-original-recovery": "retained redacted product artifact bytes recover exactly by digest",
    "campaign-budget": "two durable slots and no duplicate worker or concurrent session",
}


def required_assertions(options: GateOptions) -> dict[str, str]:
    return dict(REQUIRED)


class ProtocolGate:
    def __init__(
        self, options: GateOptions, evidence: EvidenceBuilder, scrubber: Any, engine_factory: Any
    ) -> None:
        self.options = options
        self.evidence = evidence
        self.scrubber = scrubber
        self.engine_factory = engine_factory
        self.campaign = Campaign(options)
        try:
            self.binding = campaign_binding(options)
        except (
            EngineryError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as error:
            raise GateHoldError(f"campaign binding unavailable: {error}") from error
        self.evidence.details["qualification_target"] = self.binding["qualification_target"]
        self.effects: dict[str, Any] = {
            "hosted_inference_calls": 0,
            "isolated_containers": 0,
            "worker_sessions": 0,
        }
        self.commands: list[str] = [
            "python scripts/run_factory_gate.py --scenario protocol --harness "
            + str(options.harness)
            + " --campaign-id "
            + str(options.campaign_id)
            + " --evidence-out "
            + str(options.evidence_out)
        ]
        self.workflows: list[Any] = []
        if options.live and self.campaign.read()["binding"] is None:
            from factory_gate_protocol_live import LiveProtocol

            try:
                LiveProtocol(self).prepare()
            except (
                EngineryError,
                OSError,
                ValueError,
                KeyError,
                subprocess.SubprocessError,
            ) as error:
                raise GateHoldError(f"live prerequisite failed before binding: {error}") from error
        self.campaign.bind(self.binding)

    def record(self, key: str, passed: bool, observed: Any, observer: Any) -> None:
        path = self.options.raw_dir / "assertions" / f"{key}.json"
        _write_json(path, self.scrubber.data({"observed": observed, "observer": observer}))
        self.evidence.assertion(
            key,
            passed=passed,
            commands=self.commands,
            observed=observed,
            expected=REQUIRED[key],
            observer=observer,
            raw_artifacts=[f"{self.options.raw_dir.name}/assertions/{key}.json"],
        )

    def identities(self, required: dict[str, str]) -> dict[str, Any]:
        return {
            "qualification_target": self.binding["qualification_target"],
            "subject": subject_identity(self.options.subject_commit),
            "build": {"campaign_binding": self.binding},
            "config": {
                "campaign_id": self.options.campaign_id,
                "harness": self.options.harness,
                "stand_ins": not self.options.live,
            },
            "host": host_identity(),
            "engine": self.binding.get("engine_kind"),
            "image": self.binding.get("runner_image"),
            "profile": None,
            "evaluator": evaluator_identity(required),
        }

    def cleanup(self) -> None:
        # Retained campaign projects and their artifacts are evidence, not disposable scratch.
        from enginery.engine.operator_control import live_workers

        for workflow in self.workflows:
            for name in ("release-pr", "release-worker"):
                (workflow.bin / name).touch()
            for client in workflow.clients:
                if client.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(client.pid, signal.SIGKILL)
                    client.communicate(timeout=10)
            if workflow.database.exists():
                ledger = LedgerService.open(workflow.database)
                try:
                    for worker in live_workers(ledger):
                        identity = worker.identity
                        if (
                            identity is not None
                            and identity.process_group_id in workflow.worker_groups
                        ):
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(identity.process_group_id, signal.SIGKILL)
                finally:
                    ledger.close()
            if workflow.terminal is not None:
                workflow.terminal.close()


def prepare(
    options: GateOptions,
    evidence: EvidenceBuilder,
    scrubber: Any,
    engine_factory: Any,
    *,
    need_relay: bool,
) -> ProtocolGate:
    return ProtocolGate(options, evidence, scrubber, engine_factory)


def unsupported_probes(harness: str) -> list[dict[str, str]]:
    catalog = load_execution_catalog()
    holds = []
    for control, value in (
        ("effort", "unsupported-campaign-effort"),
        ("model", "unsupported-model-alias" if harness == "omp" else "unsupported/model-form"),
    ):
        try:
            resolve_execution_selection(
                (
                    LayerControls(
                        layer=SelectionLayer.RUN,
                        harness="claude-code" if harness == "claude" else "omp",
                        model=value if control == "model" else None,
                        effort=value if control == "effort" else None,
                        model_pinned=control == "model",
                        effort_pinned=control == "effort",
                        strict_effective=None,
                    ),
                ),
                catalog=catalog,
            )
        except EngineryError as error:
            holds.append({"control": control, "value": value, "reason": str(error)})
    return holds


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="workflow-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def retain_artifacts(artifact_root: Path, directory: Path) -> dict[str, Any]:
    """Copy product-retained (redacted) bytes, verifying every content digest twice."""
    store = ArtifactStore(artifact_root, create_directories=False)
    originals: dict[str, str] = {}
    digests: list[str] = []
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for digest in store.iter_digests():
        content = store.read_bytes(digest)
        target = directory / f"{digest.hex_value}.bin"
        with target.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if target.read_bytes() != store.read_bytes(digest) or sha256(target) != digest.hex_value:
            raise GateHoldError("retained product artifact did not recover byte-exactly")
        originals[str(target.resolve())] = digest.hex_value
        digests.append(str(digest))
    return {
        "raw_originals": originals,
        "artifact_digests": digests,
        "redaction": "product secret redaction applies before storage",
    }


def _local_workflow(gate: ProtocolGate) -> dict[str, Any]:
    import factory_gate_manual_path as manual

    options = replace(gate.options, scenario="manual-path", live=False)
    builder = EvidenceBuilder(scenario="manual-path", mode="local", required=manual.REQUIRED)

    class CampaignJourney(manual.ManualPathGate):
        def __init__(self, opts: GateOptions, observations: EvidenceBuilder) -> None:
            super().__init__(opts, observations)
            previous = self.root
            retained = gate.campaign.path.parent / (str(gate.options.harness) + "-project")
            shutil.move(str(previous), retained)
            for name, value in vars(self).copy().items():
                if isinstance(value, Path) and value.is_relative_to(previous):
                    setattr(self, name, retained / value.relative_to(previous))

        def _write_stand_ins(self) -> None:
            super()._write_stand_ins()
            body = manual._OMP
            if gate.options.harness == "claude":
                body = (
                    body.replace("omp v18.10.3", "2.1.296 (Claude Code)")
                    .replace('{"type": "session"}', '{"type": "system", "subtype": "init"}')
                    .replace(
                        '{"type": "agent_start"}',
                        '{"type": "assistant", "message": {"content": []}}',
                    )
                    .replace(
                        '{"type": "agent_end"}',
                        '{"type": "result", "subtype": "success", "is_error": False}',
                    )
                )
            name = "claude" if gate.options.harness == "claude" else "omp"
            target = self.bin / name
            target.write_text(body.replace("__PYTHON__", sys.executable))
            target.chmod(0o755)

        def preferences(self, **kwargs: Any) -> None:
            super().preferences(**kwargs)
            if gate.options.harness == "claude":
                path = self.user_file()
                path.write_text(
                    path.read_text().replace(str(self.bin / "omp"), str(self.bin / "claude"))
                )
            with self.user_file().open("a") as stream:
                stream.write(
                    "\n[execution]\nmodel = "
                    + json.dumps(gate.binding["model_pins"][gate.options.harness])
                    + '\neffort = "medium"\n'
                )

        def factory(self, command: str, *arguments: str) -> tuple[int, dict[str, Any]]:
            if command == "init" and gate.options.harness == "claude":
                args = list(arguments)
                args[args.index("--harness") + 1] = "claude-code"
                arguments = tuple(args)
            result = super().factory(command, *arguments)
            if command == "init":
                policy = self.project / ".enginery" / "project.toml"
                with policy.open("a") as stream:
                    stream.write(
                        "\n[implementation]\ntime_budget_seconds = "
                        + str(gate.options.session_deadline_seconds)
                        + "\n"
                    )
            return result

    journey = CampaignJourney(options, builder)
    gate.workflows.append(journey)
    for step in (
        manual.step_prepare,
        manual.step_collect,
        manual.step_client_recovery,
        manual.step_inspection_review,
        manual.step_pr_recovery,
    ):
        step(journey)
    failures = [row for row in builder._assertions.values() if row["status"] != "pass"]
    if failures:
        raise GateHoldError(
            "deterministic product journey failed: " + json.dumps(failures, default=str)
        )
    if len(journey.launches()) != 1:
        raise GateHoldError("completed-workflow launched more than one worker")
    gate.commands.extend(journey.commands)
    pr = json.loads((journey.bin / "gh-pr.json").read_text())
    head = pr["head"]["sha"]
    changed = journey.git(
        journey.workspace, "diff", "--name-only", pr["base"]["sha"], head
    ).splitlines()

    def gh(endpoint: str) -> Any:
        result = subprocess.run(
            [str(journey.bin / "gh"), "api", "--method", "GET", endpoint],
            env=journey.environment(),
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    pr.update(
        changed_paths=changed,
        check_runs=gh(f"repos/owner/repo/commits/{head}/check-runs")["check_runs"],
        reviews=gh("repos/owner/repo/pulls/17/reviews"),
    )
    state = project_state(journey.database, journey.run_id)
    request = manual.stage1_request_from_state(json.loads(journey.request_bytes))
    artifacts = retain_artifacts(
        request.execution_configuration.artifact_root,
        gate.options.raw_dir / "completed-workflow" / "product-artifacts",
    )
    return {
        "run_id": journey.run_id,
        "project_path": str(journey.project),
        "database": str(journey.database),
        "artifact_root": str(request.execution_configuration.artifact_root),
        "workspace": str(journey.workspace),
        "workspace_path": str(journey.workspace),
        "base_revision": pr["base"]["sha"],
        "environment": journey.environment(),
        "executable": sys.executable,
        "executable_argv": [sys.executable, "-m", "enginery.cli.main"],
        "pr_path": str(journey.bin / "gh-pr.json"),
        "pr_number": pr["number"],
        "pr_url": pr["html_url"],
        "verified_head": head,
        "decision_receipt_reference": {
            "run_id": journey.run_id,
            "verified_head": head,
            "decision_ids": state["decision_ids"],
        },
        "pr": pr,
        **artifacts,
        "selected_controls": request.execution_selection.to_state(),
        "state": state,
        "assertions": builder._assertions,
        "worker_launches": journey.launches(),
        "selected_harness": gate.options.harness,
        "mode": "deterministic-stand-in",
    }


class ObservedProcess:
    """Observe the first local event without consuming bytes needed by adapter collection."""

    def __init__(self, argv: tuple[str, ...], workspace: Path) -> None:
        self.process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.pid = self.process.pid
        self.first_event = threading.Event()
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        self.threads = []
        for name, pipe in (("stdout", self.process.stdout), ("stderr", self.process.stderr)):

            def capture(name: str = name, pipe: Any = pipe) -> None:
                while chunk := os.read(pipe.fileno(), 65536):
                    self.output[name].extend(chunk)
                    if name == "stdout" and b"\n" in self.output[name]:
                        self.first_event.set()

            thread = threading.Thread(target=capture, daemon=True)
            thread.start()
            self.threads.append(thread)

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def poll(self) -> int | None:
        return self.process.poll()

    def communicate(self) -> tuple[str, None]:
        self.process.wait(timeout=10)
        for thread in self.threads:
            thread.join(timeout=10)
            if thread.is_alive():
                raise GateHoldError("raw stream reader did not close")
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe is not None:
                pipe.close()
        return bytes(self.output["stdout"]).decode(), None


def _local_cancellation(gate: ProtocolGate, operation_id: str) -> dict[str, Any]:
    from factory_gate_auth import _group_alive, _wait_until

    root = gate.campaign.path.parent / (str(gate.options.harness) + "-cancellation")
    root.mkdir(exist_ok=True)
    executable = root / "harness"
    probe = Path(__file__).with_name("factory_gate_cancel_standin.py")
    command = [
        sys.executable,
        str(probe),
        "--harness",
        str(gate.options.harness),
    ]
    executable.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
    executable.chmod(0o755)
    processes: list[ObservedProcess] = []

    def factory(argv: tuple[str, ...], workspace: Path) -> ObservedProcess:
        process = ObservedProcess(argv, workspace)
        processes.append(process)
        return process

    def terminate(pid: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)

    store = ArtifactStore(root / "artifacts")
    if gate.options.harness == "omp":
        harness = OmpHarness(
            OmpAdapterConfig(executable=str(executable), credential_reference="dummy"),
            store,
            process_factory=factory,
            terminator=terminate,
        )
    else:
        harness = ClaudeCodeHarness(
            ClaudeCodeAdapterConfig(executable=str(executable), credential_reference="dummy"),
            store,
            process_factory=factory,
            terminator=terminate,
        )
    selection = resolve_execution_selection(
        (
            LayerControls(
                layer=SelectionLayer.RUN,
                harness="omp" if gate.options.harness == "omp" else "claude-code",
                model=None,
                effort=None,
                model_pinned=False,
                effort_pinned=False,
                strict_effective=None,
            ),
        ),
        catalog=load_execution_catalog(),
    )
    task = HarnessTask(
        run_id=RunId(operation_id),
        node_id=NodeId("cancel"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId(operation_id),
        workspace_path=root,
        objective="Remain busy until cancellation; do not push or create a PR",
        acceptance_criteria=("cancelled",),
        constraints=("no external writes",),
        permitted_capabilities=("none",),
        evidence_requirements=("lifecycle",),
        time_budget_seconds=gate.options.session_deadline_seconds,
        cost_budget=Decimal(0),
        selection=selection,
    )
    session = harness.start(task)
    process = processes[0]
    watchdog = threading.Timer(
        gate.options.session_deadline_seconds, lambda: terminate(process.pid)
    )
    watchdog.start()
    try:
        if not process.first_event.wait(min(10, gate.options.session_deadline_seconds)):
            raise GateHoldError("cancellation worker emitted no first event")
        first_event = json.loads(bytes(process.output["stdout"]).splitlines()[0])
        gate.campaign.annotate(
            str(gate.options.harness) + ":cancellation",
            first_event=first_event,
            cancel_issued=True,
        )
        cancelled = harness.cancel(session, operation_id=task.operation_id)
        events = tuple(harness.events(session))
        result = harness.result(session)
        reconciled = harness.reconcile(operation_id=task.operation_id)
        gone = _wait_until(lambda: not _group_alive(process.pid), 8)
        artifacts = {
            str(output.digest): len(store.read_bytes(output.digest)) for output in result.outputs
        }
        retained = retain_artifacts(root / "artifacts", gate.options.raw_dir / "cancellation")
        return {
            "terminal_status": result.terminal_status,
            "cancel_status": (
                result.terminal_status
                if cancelled is ReconciliationResult.FOUND_MATCHING
                else "unknown"
            ),
            "reconcile_status": (
                result.terminal_status
                if reconciled is ReconciliationResult.FOUND_MATCHING
                else "unknown"
            ),
            "first_event": first_event,
            "cancel_issued": True,
            "event_count": len(events),
            "group_gone": gone,
            "container_gone": None,
            "containers": "not applicable (stand-in, no OCI)",
            "pushes": 0,
            "pull_requests": 0,
            **retained,
            "artifacts": artifacts,
            "artifact_root": str(root / "artifacts"),
        }
    finally:
        watchdog.cancel()
        terminate(process.pid)
        process.process.wait(timeout=10)


def _slot_record(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    _write_json(path, result)
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "raw_originals": result.get("raw_originals", {}),
        "run_id": result.get("run_id"),
        "pr_number": result.get("pr_number"),
        "pr_url": result.get("pr_url"),
        "verified_head": result.get("verified_head"),
        "decision_receipt_reference": result.get("decision_receipt_reference"),
        "workflow": result,
    }


def _slot_result(gate: ProtocolGate, role: str) -> dict[str, Any]:
    harness = str(gate.options.harness)
    key = f"{harness}:{role}"
    document = gate.campaign.read()
    expected = "completed" if role == "completed-workflow" else "cancelled"
    if key in document["slots"]:
        slot = document["slots"][key]
        terminal = slot["state"] == expected or (
            slot["state"] == "reconciled"
            and slot.get("reconciliation", {}).get("terminal_status") == expected
        )
        record = document["evidence"].get(key)
        if not terminal or not record:
            raise GateHoldError(f"{key} was already spent; reconcile it, never relaunch it")
        for name, digest in {
            record["path"]: record["sha256"],
            **record.get("raw_originals", {}),
        }.items():
            if not Path(name).is_file() or sha256(Path(name)) != digest:
                raise GateHoldError(f"{key} has missing or stale retained evidence")
        retained = json.loads(Path(record["path"]).read_text())
        if retained != record["workflow"]:
            raise GateHoldError(f"{key} retained metadata differs from its digest-bound receipt")
        return dict(retained)

    live = None
    if gate.options.live:
        from factory_gate_protocol_live import preflight

        live = preflight(gate, role)
    key = gate.campaign.reserve(harness, role)
    operation_id = gate.campaign.read()["slots"][key]["operation_id"]
    gate.campaign.transition(key, "running")
    gate.effects["worker_sessions"] += 1
    try:
        if live is not None:
            result = (
                live.completed_workflow()
                if role == "completed-workflow"
                else live.cancellation(operation_id)
            )
        else:
            result = (
                _local_workflow(gate)
                if role == "completed-workflow"
                else _local_cancellation(gate, operation_id)
            )
        terminal = expected
        if role == "cancellation" and result.get("terminal_status") != "cancelled":
            terminal = "failed"
        path = gate.options.raw_dir / (key.replace(":", "-") + ".json")
        record = _slot_record(path, result)
        gate.campaign.annotate(
            key,
            workflow_path=record["path"],
            workflow_sha256=record["sha256"],
            workflow_terminal=terminal,
        )
        gate.campaign.finalize(key, terminal, record)
        return result
    except BaseException:
        current = gate.campaign.read()["slots"][key]["state"]
        if current in {"reserved", "running"}:
            gate.campaign.transition(key, "interrupted")
        raise


def _reconcile(gate: ProtocolGate) -> None:
    def callback(key: str, slot: dict[str, Any]) -> dict[str, Any]:
        expected = "completed" if key.endswith(":completed-workflow") else "cancelled"
        path = (
            Path(slot["workflow_path"])
            if slot.get("workflow_path")
            else gate.options.raw_dir / (key.replace(":", "-") + ".json")
        )
        retained = None
        if slot.get("workflow_path") and slot.get("workflow_sha256"):
            try:
                payload = path.read_bytes()
                if hashlib.sha256(payload).hexdigest() == slot["workflow_sha256"]:
                    candidate = json.loads(payload)
                    if isinstance(candidate, dict):
                        retained = candidate
            except (OSError, ValueError):
                pass
        if gate.options.live:
            from factory_gate_protocol_live import reconcile_interrupted

            observation = reconcile_interrupted(gate, key, slot)
        else:
            observation = {
                "container_gone": True,
                "observation": "deterministic campaign uses no OCI containers",
            }
            if retained is not None:
                observation["terminal_status"] = slot.get("workflow_terminal", expected)
        if observation.get("terminal_status") == expected:
            if slot.get("workflow_path"):
                observation.pop("workflow", None)
                if retained is not None and slot.get("workflow_terminal") == expected:
                    observation["workflow"] = retained
            if observation.get("workflow"):
                observation["evidence"] = _slot_record(path, dict(observation["workflow"]))
        return observation

    gate.campaign.reconcile_dead(callback)


def _controls_match(gate: ProtocolGate, workflow: dict[str, Any]) -> bool:
    harness = str(gate.options.harness)
    host = load_execution_catalog().host("omp" if harness == "omp" else "claude-code")
    expected: list[str] = []
    model = gate.binding["model_pins"].get(harness)
    effort = gate.binding.get("effort_pins", {}).get(harness)
    if model is not None:
        expected.extend((host.model.flag, model))
    if effort is not None:
        expected.extend((host.effort.flag, effort))
    receipts = workflow["state"]["execution_receipts"]
    return bool(receipts) and all(
        receipt["harness"] == host.harness
        and receipt["submitted_arguments"] == expected
        and receipt["effective_effort"]["state"] == "unknown"
        and receipt["effective_model"]["state"] in {"observed", "unknown"}
        for receipt in receipts
    )


def run(gate: ProtocolGate) -> None:
    harness = str(gate.options.harness)
    gate.record("campaign-binding", True, gate.binding, {"manifest": str(gate.campaign.path)})
    before = gate.campaign.read()
    holds = unsupported_probes(harness)
    after = gate.campaign.read()
    new_slots = len(after["slots"]) - len(before["slots"])
    gate.record(
        "unsupported-controls",
        len(holds) == 2 and new_slots == 0,
        {"holds": holds, "reserved_slots": len(before["slots"])},
        {
            "container_creates": None,
            "containers": "not applicable: in-process resolver",
            "new_slots": new_slots,
        },
    )
    if len(holds) != 2 or new_slots != 0:
        raise GateHoldError("unsupported controls were not refused before launch")
    _reconcile(gate)
    workflow = _slot_result(gate, "completed-workflow")
    assertions = workflow["assertions"]
    gate.record(
        "completed-workflow",
        assertions["verified-open-pr-outcome"]["status"] == "pass",
        workflow["pr"],
        {"product_assertions": assertions, "mode": workflow["mode"]},
    )
    gate.record(
        "coordinator-recovery",
        assertions["client-resume-same-identity"]["status"] == "pass"
        and len(workflow["worker_launches"]) == 1,
        {"launches": workflow["worker_launches"]},
        assertions["client-kill-worker-survives"],
    )
    receipts = workflow["state"]["execution_receipts"]
    gate.record(
        "execution-controls",
        _controls_match(gate, workflow),
        receipts,
        {
            "source": "product node_attempt result execution_receipt",
            "harness": harness,
            "selected": workflow.get("selected_controls"),
        },
    )
    cancellation = _slot_result(gate, "cancellation")
    cancellation_slot = gate.campaign.read()["slots"][harness + ":cancellation"]
    gate.record(
        "cancellation-reconcile",
        cancellation["terminal_status"] == "cancelled"
        and cancellation["group_gone"]
        and (not gate.options.live or cancellation["container_gone"])
        and cancellation["cancel_status"] == cancellation["reconcile_status"] == "cancelled"
        and bool(cancellation_slot.get("first_event"))
        and cancellation_slot.get("cancel_issued") is True
        and cancellation["pushes"] == cancellation["pull_requests"] == 0,
        cancellation,
        {
            "source": "installed supervised product lifecycle"
            if gate.options.live
            else "canonical HarnessPort with real local stand-in process"
        },
    )
    originals = {**workflow["raw_originals"], **cancellation["raw_originals"]}
    recovered = all(
        Path(name).is_file() and sha256(Path(name)) == digest for name, digest in originals.items()
    )
    gate.record(
        "raw-original-recovery",
        bool(originals) and recovered,
        originals,
        {"reopened": sorted(originals), "capture": "product-retained redacted artifact bytes"},
    )
    document = gate.campaign.read()
    slots = document["slots"]
    terminal_slots = all(
        (slot := slots.get(f"{harness}:{role}", {})).get("state") == expected
        or (
            slot.get("state") == "reconciled"
            and slot.get("reconciliation", {}).get("terminal_status") == expected
        )
        for role, expected in (("completed-workflow", "completed"), ("cancellation", "cancelled"))
    )
    bound_evidence = all(
        document["evidence"].get(f"{harness}:{role}", {}).get("workflow")
        == json.loads(json.dumps(result))
        for role, result in (("completed-workflow", workflow), ("cancellation", cancellation))
    )
    gate.record(
        "campaign-budget",
        terminal_slots
        and bound_evidence
        and (
            not gate.options.live
            or gate.effects["isolated_containers"] == gate.effects["worker_sessions"]
        ),
        slots,
        gate.effects,
    )
    gate.evidence.details["campaign_manifest"] = str(gate.campaign.path)


def finalize(gate: ProtocolGate) -> None:
    """All retained stream and campaign assertions run before cleanup."""
