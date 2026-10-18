"""Deterministic CLI/engine stand-ins for the live orchestration boundary: no live calls."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import factory_gate_protocol_live as live_module
from enginery.adapters.oci.engine import ObjectNotFoundError
from enginery.adapters.runner_launch import publish_receipt
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.execution_controls import build_receipt, expected_native_arguments
from enginery.domain.digests import Digest
from enginery.domain.errors import MissingPrerequisiteError, OperationTimeoutError
from enginery.domain.execution_selection import (
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)
from enginery.domain.runner import (
    EffectiveContainer,
    EngineIdentity,
    ImageIdentity,
    PermissionMapping,
    RunnerProfile,
    RunnerReceipt,
    RunnerSelection,
    TrustMode,
)
from enginery.ledger.artifact_store import ArtifactStore
from factory_gate_context import GateHoldError
from factory_gate_support import Scrubber


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Client:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver
        self.pid = 10000 + len(driver.clients)
        self.returncode: int | None = None
        self.kills = 0

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        assert self.driver.engine.alive, "the deliberate coordinator kill must precede worker exit"
        self.kills += 1
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        assert timeout <= 10
        assert self.returncode is not None
        return self.returncode


class Engine:
    def __init__(self, driver: Driver) -> None:
        self.driver = driver
        self.alive = False
        self.kind = "docker-desktop"
        self.creates = 1
        self.event_history: list[dict[str, Any]] | None = None
        self.calls: list[tuple[str, ...]] = []

    def qualify(self) -> Any:
        return SimpleNamespace(
            kind=self.kind, to_state=lambda: {"kind": self.kind, "observed": True}
        )

    def container_exists(self, name: str) -> bool:
        assert name == live_module.container_name_for(self.driver.operation_id)
        return self.alive

    def container_inspect(self, name: str) -> dict[str, Any]:
        return {
            "Id": "engine-observed-worker-id",
            "Config": {
                "Labels": {
                    live_module.OPERATION_LABEL: self.driver.operation_id,
                    live_module.RUNNER_LABEL: "1",
                }
            },
            "State": {"Running": self.alive},
        }

    def run_cli(self, argv: Any, *, timeout: float, **kwargs: Any) -> Any:
        args = tuple(argv)
        self.calls.append(args)
        assert 0 < timeout <= 90
        if args[0] == "info":
            text = "2026-10-18T00:00:00Z\n"
        elif args[0] == "events":
            event = {
                "Type": "container",
                "Action": "create",
                "time": 1792281600,
                "timeNano": 1792281600000000000,
                "Actor": {
                    "ID": "engine-observed-worker-id",
                    "Attributes": {
                        "name": live_module.container_name_for(self.driver.operation_id),
                        live_module.OPERATION_LABEL: self.driver.operation_id,
                    },
                },
            }
            count = self.creates if self.driver.worker_started else 0
            text = (json.dumps(event) + "\n") * count
            if self.event_history is not None:
                events = self.event_history
                if "event=create" in args:
                    events = [row for row in events if row["Action"] == "create"]
                text = "\n".join(map(json.dumps, events))
        elif args[0] == "ps":
            assert f"label={live_module.OPERATION_LABEL}={self.driver.operation_id}" in args
            text = (
                live_module.container_name_for(self.driver.operation_id) + "\n"
                if self.alive
                else ""
            )
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, text, "")

    def kill(self, name: str) -> None:
        self.alive = False

    def remove(self, name: str, *, force: bool) -> None:
        assert force
        self.alive = False


class Driver:
    """Explicit external effects; production orchestration and artifact copying remain real."""

    def __init__(self, root: Path, harness: str) -> None:
        self.root = root
        self.project = root / "project"
        self.project.mkdir()
        (self.project / ".enginery").mkdir()
        (self.project / ".enginery" / "project.toml").write_text("# stand-in configuration\n")
        self.harness = harness
        self.provider = "claude-code" if harness == "claude" else "omp"
        self.image = "sha256:" + "a" * 64
        self.run_id = "fr-frozen-issue"
        self.operation_id = "implement:" + self.run_id
        self.database = root / "project-ledger.sqlite"
        self.request_path = root / "request.json"
        self.artifact_root = root / "artifacts"
        self.output_digest = ArtifactStore(self.artifact_root).publish_bytes(
            (json.dumps({"type": "message_end", "usage": {"input_tokens": 7}}) + "\n").encode()
        )
        self.initial_issue = (
            '{"number":1,"title":"Useful change","body":"Acceptance","state":"open","labels":[]}\n'
        )
        self.calls: list[list[str]] = []
        self.clients: list[Client] = []
        self.annotations: dict[str, dict[str, Any]] = {}
        self.engine = Engine(self)
        self.clock = Clock()
        self.status = "not_started"
        self.resumed = False
        self.advance_count = 0
        self.worker_started = False
        self.reservations: list[str] = []
        self.project_runs: list[dict[str, Any]] = []
        self.qualification_holds: list[dict[str, Any]] = []
        self.cancel_qualification = "implement"
        self.decisions = True
        self.snapshot_head = "b" * 40
        self.remote_prs: list[dict[str, Any]] = []
        self.bound_request_digest: str | None = None
        self.effective_repair_limit = 0
        self.deadline_jump = False
        self.observed_image_version = (
            load_execution_catalog().host(self.provider).tested_executable_version
        )
        self.request = self.make_request()

    def annotate(self, key: str, **values: Any) -> None:
        self.annotations.setdefault(key, {}).update(values)

    def reserve(self, harness: str, role: str) -> str:
        assert not self.clients and not self.engine.alive
        assert self.status == "qualified"
        key = harness + ":" + role
        self.reservations.append(key)
        return key

    def write_request(self) -> None:
        state = {
            name: value
            for name, value in self.request.items()
            if name not in {"status", "request_digest", "effective_config"}
        }
        self.bound_request_digest = str(Digest.of_json(state))
        self.request["request_digest"] = self.bound_request_digest
        self.request_path.write_text(json.dumps(self.request))

    def make_request(self, **overrides: Any) -> dict[str, Any]:
        catalog = load_execution_catalog()
        selection = resolve_execution_selection(
            (
                LayerControls(
                    layer=SelectionLayer.RUN,
                    harness=self.provider,
                    model=None,
                    effort=None,
                    model_pinned=False,
                    effort_pinned=False,
                    strict_effective=None,
                ),
            ),
            catalog=catalog,
        )
        runner = RunnerSelection.isolated(
            image=self.image,
            profile=RunnerProfile(),
            docker_context="desktop-linux",
            model_access="relay",
        )
        request = {
            "run_id": self.run_id,
            "run": {"data": {"base_revision": "c" * 40}},
            "repository_path": str(self.project),
            "base_branch": "main",
            "validation_commands": [["python3", "-m", "unittest"]],
            "work_item": {
                "data": {
                    "external_reference": "owner/repo#1",
                    "title": "Useful change",
                    "objective": "Acceptance",
                }
            },
            "head_branch": "enginery/" + self.run_id,
            "workspace_path": str(self.root / "workspace"),
            "repair_limit": 0,
            "implementation": {"operation_id": self.operation_id, "time_budget_seconds": 780},
            "execution_configuration": {
                "artifact_root": str(self.artifact_root),
                "harness_provider": self.provider,
                "github_repository": "owner/repo",
            },
            "runner": runner.to_state(),
            "execution_selection": selection.to_state(),
        }
        request.update(overrides)
        return request

    def receipt(self) -> dict[str, Any]:
        catalog = load_execution_catalog()
        host = catalog.host(self.provider)
        selection = live_module.ExecutionSelection.from_state(self.request["execution_selection"])
        profile = RunnerProfile()
        limits = profile.resources
        proof = RunnerReceipt(
            operation_id=self.operation_id,
            trust_mode=TrustMode.ISOLATED_OCI,
            profile_name=profile.name,
            profile_digest=profile.digest,
            engine=EngineIdentity(
                "docker-desktop",
                "29.8.2",
                "Docker Desktop",
                "linuxkit-test",
                "2",
                ("seccomp",),
                False,
                "desktop-linux",
            ),
            image=ImageIdentity(
                self.image, "sha256:" + "e" * 64, ((self.provider, host.tested_executable_version),)
            ),
            container_name=live_module.container_name_for(self.operation_id),
            host={"hostname": "stand-in"},
            effective=EffectiveContainer(
                user=f"{profile.uid}:{profile.gid}",
                cap_drop=profile.cap_drop,
                cap_add=(),
                security_options=profile.security_options,
                privileged=False,
                read_only_rootfs=True,
                memory_bytes=limits.memory_bytes,
                memory_swap_bytes=limits.memory_bytes,
                nano_cpus=limits.nano_cpus,
                pids_limit=limits.pids,
                network_mode="none",
                networks=(),
                mounts=(),
                devices=(),
                env_keys=(),
            ),
            permissions=PermissionMapping(
                self.provider, "isolated-test", (), False, "deterministic fixture"
            ),
            harness_version=self.observed_image_version,
            harness_protocol=host.protocol_fingerprint,
            started_at="2026-10-18T00:00:00Z",
        )
        reference = publish_receipt(proof, ArtifactStore(self.artifact_root))
        return build_receipt(
            selection,
            harness=self.provider,
            catalog=catalog,
            launched_command=("harness", *expected_native_arguments(selection, catalog=catalog)),
            models=(),
            observed_at_launch=self.observed_image_version,
            runner=reference,
        ).to_state()

    def read_state(self, database: Path, run_id: str) -> dict[str, Any]:
        assert str(database) == str(self.database)
        assert run_id == self.run_id
        supervisor = {
            "run_id": run_id,
            "operation_id": self.operation_id,
            "status": "running" if self.engine.alive else "exit_reconciled",
            "pid": 123456789,
            "process_group_id": 123456789,
            "start_identity": "fake",
            "attempt_id": "attempt-0",
        }
        attempts: list[dict[str, Any]] = []
        receipts: list[dict[str, Any]] = []
        if self.status == "passed":
            receipts = [self.receipt()]
            attempts = [
                {
                    "run_id": run_id,
                    "node_id": "implement",
                    "terminal_result": "passed",
                    "artifact_references": [str(self.output_digest)],
                    "result": {"harness_status": "succeeded", "execution_receipt": receipts[0]},
                }
            ]
        elif self.status == "cancelled":
            attempts = [
                {
                    "run_id": run_id,
                    "node_id": "implement",
                    "terminal_result": "cancelled",
                    "artifact_references": [],
                    "result": {"cancellation": "operator_requested"},
                }
            ]
        return {
            "supervisors": [supervisor] if self.worker_started else [],
            "nodes": (
                [{"node_id": "qualify", "status": "passed"}]
                if self.status == "qualified"
                else [{"node_id": "implement", "status": self.status}]
                if self.status != "not_started"
                else []
            ),
            "attempts": attempts,
            "execution_receipts": receipts,
            "decisions": [
                {
                    "id": "real-product-decision-shaped",
                    "run_id": run_id,
                    "subject_fields": {"head_revision": self.snapshot_head},
                    "principal_type": "human",
                    "principal_id": "terminal-owner",
                    "os_uid": 501,
                    "decided_at": "2026-10-18T00:00:00+00:00",
                    "action": "approve",
                    "tty_name": "/dev/ttys001",
                    "superseded_by": None,
                }
            ]
            if self.decisions
            else [],
        }

    def popen(self, argv: list[str], **kwargs: Any) -> Client:
        assert argv[1] == "continue"
        assert argv[2] == self.run_id
        assert self.harness + ":completed-workflow" in self.reservations
        assert (
            self.annotations[self.harness + ":completed-workflow"]["operation_id"]
            == self.operation_id
        )
        assert kwargs["start_new_session"] is True
        assert "PYTHONPATH" not in kwargs["env"]
        client = Client(self)
        self.clients.append(client)
        if len(self.clients) == 1:
            self.engine.alive = True
            self.worker_started = True
            self.status = "running"
        else:
            self.resumed = True
        if self.deadline_jump:
            self.clock.now += 901
        return client

    def run(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(argv)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert 0 < kwargs["timeout"] <= 30
        assert kwargs["cwd"] == self.project
        result: Any
        if argv[0] == "git":
            if argv[1:4] == ["remote", "get-url", "origin"]:
                result = "https://github.com/owner/repo.git\n"
            elif argv[1] == "ls-remote":
                result = ""
            elif argv[1] == "symbolic-ref":
                result = "main\n"
            else:
                result = "c" * 40 + "\n"
        elif argv[0] == "gh":
            assert argv[1:4] == ["api", "--method", "GET"]
            endpoint = next(arg for arg in argv if arg.startswith("repos/"))
            if "/issues/" in endpoint:
                result = self.initial_issue
            elif "/files?" in endpoint:
                result = [[{"filename": "feature.py"}]]
            elif "/check-runs?" in endpoint:
                result = [
                    {
                        "check_runs": [
                            {
                                "head_sha": self.snapshot_head,
                                "status": "completed",
                                "conclusion": "success",
                            }
                        ]
                    }
                ]
            elif "/reviews?" in endpoint:
                result = [[{"state": "APPROVED", "commit_id": self.snapshot_head}]]
            elif endpoint.endswith("/pulls"):
                result = self.remote_prs
            else:
                result = {
                    "number": 17,
                    "state": "open",
                    "merged": False,
                    "head": {"sha": self.snapshot_head},
                    "base": {"sha": "c" * 40},
                    "html_url": "https://github.com/owner/repo/pull/17",
                }
        elif argv[1] == "explain":
            fields = {
                "harness": self.provider,
                "runner_mode": "isolated-oci",
                "runner_image": self.image,
                "model_access": "relay",
                "docker_context": "desktop-linux",
                "repair_limit": self.effective_repair_limit,
                "validation_commands": [["python3", "-m", "unittest"]],
            }
            result = {
                "fields": [{"name": name, "value": value} for name, value in fields.items()],
                "holds": [],
            }
        elif argv[1] == "collect":
            result = {
                "candidates": [{"external_reference": "owner/repo#1", "eligible": True}],
                "holds": [],
            }
        elif argv[1] == "run":
            assert "--once" in argv and "--time-budget-seconds" in argv
            self.database.touch()
            self.status = "qualified"
            self.project_runs = [{"issue": "owner/repo#1", "run_id": self.run_id}]
            self.write_request()
            result = {
                "run_id": self.run_id,
                "request_path": str(self.request_path),
                "database": str(self.database),
                "holds": self.qualification_holds,
                "next_action": "implement",
            }
        elif argv[1] == "status":
            if argv[2] == "--project":
                result = {"runs": self.project_runs}
            else:
                if self.resumed:
                    self.status = "passed"
                    self.engine.alive = False
                    if self.clients:
                        self.clients[-1].returncode = 0
                result = {
                    "run_id": self.run_id,
                    "request_path": str(self.request_path),
                    "database": str(self.database),
                    "issue": "owner/repo#1",
                    "next_action": (
                        "implement" if self.status == "qualified" else "await_human_review"
                    ),
                    "workflow_complete": self.resumed,
                    "terminal_decision": "merge_ready" if self.resumed else None,
                    "pull_request": {"number": 17, "head_revision": "b" * 40},
                    "terminal_evidence": {"verify_status": "passed"},
                    "nodes": [{"node_id": "implement", "status": self.status}],
                    "holds": [],
                }
        elif argv[1:3] == ["engine", "build-request"]:

            def value(flag: str) -> str:
                return argv[argv.index(flag) + 1]

            assert value("--external-reference") == "owner/repo#1"
            self.run_id = value("--run-id")
            self.operation_id = value("--implementation-operation-id")
            self.database = Path(value("--output")).parent / "ledger.sqlite"
            self.request_path = Path(value("--output"))
            self.artifact_root = Path(value("--artifact-root"))
            self.request = self.make_request(
                execution_configuration={
                    "artifact_root": str(self.artifact_root),
                    "harness_provider": self.provider,
                    "github_repository": "owner/repo",
                }
            )
            self.request["implementation"]["time_budget_seconds"] = int(
                value("--implementation-time-budget-seconds")
            )
            self.write_request()
            result = {"run_id": self.run_id}
        elif argv[1:3] == ["engine", "start"]:
            assert not self.reservations and not self.annotations
            self.database.touch()
            result = {"run_id": self.run_id, "status": "created"}
        elif argv[1:3] == ["engine", "next"]:
            if "--advance" in argv:
                self.advance_count += 1
                if self.advance_count == 1:
                    assert not self.reservations
                    self.status = "qualified"
                assert self.advance_count <= 2, "no publication or replacement-worker advance"
                if self.advance_count == 2:
                    self.status = "running"
                    assert self.reservations == [self.harness + ":cancellation"]
                    self.worker_started = True
                    self.engine.alive = True
                result = {
                    "next_action": self.cancel_qualification if self.advance_count == 1 else "wait"
                }
            else:
                result = {
                    "next_action": "await_human_review",
                    "nodes": [{"node_id": "implement", "status": self.status}],
                }
        elif argv[1:3] == ["engine", "cancel"]:
            self.status = "cancelled"
            self.engine.alive = False
            result = {"status": "cancelled"}
        elif argv[1:3] == ["engine", "evidence"]:
            result = {
                "request_digest": self.bound_request_digest,
                "nodes": [{"node_id": "implement", "status": self.status, "artifacts": []}],
            }
        else:
            raise AssertionError(argv)
        stdout = result if isinstance(result, str) else json.dumps(result)
        return subprocess.CompletedProcess(argv, 0, stdout, "")


@pytest.fixture
def setup_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    def create(harness: str = "omp") -> tuple[Any, Driver]:
        driver = Driver(tmp_path, harness)
        token = tmp_path / "token"
        token.write_text("secret-gateway-token")
        config = tmp_path / "config.json"
        config.write_text(
            json.dumps(
                {
                    "qualification_target": "synthetic",
                    "validation_commands": ["python3 -m unittest"],
                }
            )
        )
        options = SimpleNamespace(
            harness=harness,
            campaign_config=config,
            docker_context="desktop-linux",
            raw_dir=tmp_path / "evidence.raw",
            session_deadline_seconds=900,
            gateway_url="http://host.docker.internal:47400",
            gateway_token_file=token,
            custody_receipt=tmp_path / "custody.json",
            runner_image=driver.image,
            relay_image="sha256:" + "d" * 64,
        )
        binding = {
            "project_path": str(driver.project),
            "executable": "/installed/bin/enginery",
            "repository": "owner/repo",
            "qualification_target": "synthetic",
            "issues": {harness: live_module.issue_binding(driver.initial_issue.encode())},
            "model_pins": {harness: None},
            "effort_pins": {harness: None},
            "engine_kind": "docker-desktop",
            "validation_commands": ["python3 -m unittest"],
        }
        gate = SimpleNamespace(
            options=options,
            binding=binding,
            commands=[],
            scrubber=Scrubber(),
            effects={"isolated_containers": 0},
            engine_factory=lambda **kwargs: driver.engine,
            campaign=SimpleNamespace(
                path=tmp_path / "campaign" / "manifest.json",
                campaign_id="frozen",
                annotate=driver.annotate,
                reserve=driver.reserve,
                read=lambda: {
                    "slots": {"claude:completed-workflow": {"operation_id": "implement:other"}}
                },
            ),
        )
        relay = SimpleNamespace(
            container_name="enginery-model-relay",
            up=lambda **kwargs: SimpleNamespace(healthy=True, to_state=lambda: {"healthy": True}),
        )
        monkeypatch.setattr(live_module, "load_live_custody", lambda *args, **kwargs: None)
        import enginery.engine.supervisor as supervisor

        monkeypatch.setattr(supervisor, "_terminate_exact_group", lambda identity: None)
        live = live_module.LiveProtocol(
            gate,
            run_command=driver.run,
            popen=driver.popen,
            clock=driver.clock,
            sleep=driver.clock.sleep,
            group_alive=lambda pgid: driver.engine.alive,
            read_state=driver.read_state,
            relay_factory=lambda engine: relay,
        )
        return live, driver

    return create


def completed(live: Any) -> dict[str, Any]:
    live.preflight("completed-workflow")
    live.gate.campaign.reserve(live.harness, "completed-workflow")
    return live.completed_workflow()


def cancelled(live: Any) -> dict[str, Any]:
    live.preflight("cancellation")
    live.gate.campaign.reserve(live.harness, "cancellation")
    return live.cancellation("campaign-frozen-" + live.harness + "-cancellation")


@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_installed_completion_kills_one_client_and_continues_same_worker(
    setup_live: Any, harness: str
) -> None:
    live, driver = setup_live(harness)
    result = completed(live)
    live.close()
    assert len(driver.clients) == 2
    assert [client.kills for client in driver.clients] == [1, 0]
    assert result["lifecycle"]["coordinator_kills"] == 1
    assert result["verified_head"] == "b" * 40
    assert result["decision_receipt_reference"]["decisions"][0]["tty_name"]
    assert result["usage"] == {"input_tokens": 7}
    assert result["state"]["execution_receipts"][0]["effective_effort"]["state"] == "unknown"
    assert result["group_gone"] and result["container_gone"]
    assert live.gate.effects["isolated_containers"] == 1
    assert live.gate.effects["hosted_inference_calls"] is None
    assert result["worker_launches"] == ["engine-observed-worker-id"]
    observed_runner = live_module.ExecutionReceipt.from_state(
        result["execution_receipts"][0]
    ).runner
    assert observed_runner is not None
    proof = RunnerReceipt.from_state(
        json.loads(ArtifactStore(driver.artifact_root).read_bytes(observed_runner.receipt_digest))
    )
    assert result["runner_profile"]["profile_digest"] == str(proof.profile_digest)
    assert result["runner_profile"]["effective"] == proof.effective.to_state()
    assert driver.annotations[harness + ":completed-workflow"]["coordinator_kills"] == 1
    assert "redaction_count" not in result
    for path, digest in result["raw_originals"].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    assert not any("decide" in argv or "approve" in argv for argv in driver.calls)
    assert sum(argv[1] == "run" for argv in driver.calls if len(argv) > 1) == 1
    for command in live.commands:
        argv = shlex.split(command)
        if argv[0] == live.executable:
            from enginery.cli.main import _build_parser

            _build_parser().parse_args(argv[1:])


@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_cancellation_reuses_issue_in_private_ledger_and_never_publishes(
    setup_live: Any, harness: str
) -> None:
    live, driver = setup_live(harness)
    result = cancelled(live)
    live.close()
    assert result["terminal_status"] == "cancelled"
    assert result["cancel_status"] == result["reconcile_status"] == "cancelled"
    assert result["cancel_issued"] is True
    assert result["event_count"] == 1
    assert result["first_event"]["kind"] == "worker.started"
    assert result["first_event"]["provider_json_before_cancel"] == "not exposed by pinned worker"
    assert result["pushes"] == result["pull_requests"] == 0
    assert result["container_gone"] and result["group_gone"]
    assert result["raw_originals"] == {}
    assert result["raw_absence"]["artifact_references"] == []
    assert result["raw_absence"]["terminal_status"] == "cancelled"
    assert len(driver.clients) == 0
    assert driver.advance_count == 2
    assert result["worker_launches"] == ["engine-observed-worker-id"]
    from enginery.cli.main import _build_parser

    for command in live.commands:
        argv = shlex.split(command)
        if argv[0] == live.executable:
            _build_parser().parse_args(argv[1:])
    assert "cancel" in str(live.database)
    assert not any(
        argv[1] in {"run", "continue", "decide"}
        for argv in driver.calls
        if argv[0] == live.executable
    )


def test_snapshot_drift_holds_before_any_product_request_or_worker(setup_live: Any) -> None:
    live, driver = setup_live()
    snapshot = json.loads(driver.initial_issue)
    snapshot["title"] = "Changed task"
    driver.initial_issue = json.dumps(snapshot)
    with pytest.raises(GateHoldError, match="snapshot changed"):
        live.preflight("completed-workflow")
    assert not driver.clients and not driver.annotations
    assert not any(argv[1] == "run" for argv in driver.calls)


@pytest.mark.parametrize("field,value", [("engine", "rootless-docker"), ("repair_limit", 1)])
def test_configuration_mismatch_holds_before_worker(
    setup_live: Any, field: str, value: Any
) -> None:
    live, driver = setup_live()
    if field == "engine":
        driver.engine.kind = value
    else:
        driver.effective_repair_limit = value
    with pytest.raises(GateHoldError):
        live.preflight("completed-workflow")
    assert not driver.clients and not driver.annotations


def test_same_name_second_engine_create_is_not_deduplicated(setup_live: Any) -> None:
    live, driver = setup_live()
    driver.engine.creates = 2
    with pytest.raises(GateHoldError, match="exactly one worker create"):
        completed(live)
    live.close()
    assert live.gate.effects["isolated_containers"] == 2


def test_no_human_terminal_decision_is_not_fabricated(setup_live: Any) -> None:
    live, driver = setup_live()
    driver.decisions = False
    with pytest.raises(GateHoldError, match="human terminal action decision"):
        completed(live)
    live.close()
    assert not any("decide" in argv for argv in driver.calls)


def test_gateway_leak_holds_and_scrubs_before_retention(setup_live: Any) -> None:
    live, _driver = setup_live()
    live.gate.scrubber.register("gateway-token", "secret-gateway-token")
    with pytest.raises(GateHoldError, match="secret"):
        live._save("leak.json", {"accident": "secret-gateway-token"})
    assert "secret-gateway-token" not in (live.directory / "leak.json").read_text()


def test_worker_deadline_is_not_reset_after_start(setup_live: Any) -> None:
    live, driver = setup_live()
    driver.deadline_jump = True
    with pytest.raises(GateHoldError, match="deadline reached"):
        completed(live)
    assert len(driver.clients) == 1
    assert live.deadline == 900
    assert driver.clock.now > live.deadline


def test_interrupted_reserved_slot_only_inspects_stable_label(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, driver = setup_live()
    driver.operation_id = "campaign-frozen-omp-cancellation"
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: live)
    result = live_module.reconcile_interrupted(
        live.gate, "omp:cancellation", {"operation_id": driver.operation_id}
    )
    assert result["container_gone"]
    assert result["terminal_status"] == "failed"
    assert not driver.calls and not driver.clients and not driver.annotations
    assert driver.engine.calls[0][0] == "ps"


def test_interrupted_completed_run_continues_without_rebinding_or_second_worker(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, driver = setup_live()
    first = completed(live)
    live.close()
    slot = dict(driver.annotations["omp:completed-workflow"])
    driver.clients.clear()
    driver.annotations.clear()
    driver.calls.clear()
    driver.resumed = True
    recovered = live
    # A new injected coordinator boundary, preserving only product state and immutable slot.
    recovered.files = []
    recovered.clients = []
    recovered.popen = lambda argv, **kwargs: SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: recovered)
    result = live_module.reconcile_interrupted(recovered.gate, "omp:completed-workflow", slot)
    assert result["terminal_status"] == "completed"
    assert result["workflow"]["run_id"] == first["run_id"]
    assert result["no_launch"] is True
    assert not driver.annotations
    assert not any(argv[1] == "run" for argv in driver.calls)


def test_deadline_failure_cancels_worker_and_reaps_client(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, driver = setup_live()
    driver.deadline_jump = True
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate: live)
    qualified = live_module.preflight(live.gate, "completed-workflow")
    live.gate.campaign.reserve("omp", "completed-workflow")
    with pytest.raises(GateHoldError, match="deadline reached"):
        qualified.completed_workflow()
    assert not driver.engine.alive
    assert driver.status == "cancelled"
    assert len(driver.clients) == 1
    assert driver.clients[0].returncode == -9
    assert any(argv[1:3] == ["engine", "cancel"] for argv in driver.calls)


@pytest.mark.parametrize("deadline_passed", [True, False])
def test_an_engine_command_cut_short_by_the_deadline_is_the_deadline_hold(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, deadline_passed: bool
) -> None:
    live, driver = setup_live()
    live.deadline = live.clock() + 5

    def timed_out(argv: Any, *, timeout: float, **kwargs: Any) -> Any:
        if deadline_passed:
            driver.clock.now += timeout
        raise OperationTimeoutError("container engine command timed out: events --since")

    monkeypatch.setattr(driver.engine, "run_cli", timed_out)
    expected: type[Exception] = GateHoldError if deadline_passed else OperationTimeoutError
    with pytest.raises(expected) as raised:
        live.engine_cli("events", "--since", "1", timeout=90)
    if deadline_passed:
        assert "deadline reached" in str(raised.value)


def test_a_worker_removed_before_the_survival_check_holds_instead_of_crashing(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, driver = setup_live()
    inspect = driver.engine.container_inspect

    def removed_after_kill(name: str) -> dict[str, Any]:
        if driver.clients and driver.clients[0].kills:
            raise ObjectNotFoundError("container engine command failed: container inspect")
        return inspect(name)

    monkeypatch.setattr(driver.engine, "container_inspect", removed_after_kill)
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate: live)
    qualified = live_module.preflight(live.gate, "completed-workflow")
    live.gate.campaign.reserve("omp", "completed-workflow")
    with pytest.raises(GateHoldError, match="did not survive the single coordinator kill"):
        qualified.completed_workflow()


def test_image_executable_receipt_cannot_substitute_a_workstation_version(setup_live: Any) -> None:
    live, driver = setup_live()
    catalog = load_execution_catalog()
    selection = resolve_execution_selection(
        (
            LayerControls(
                layer=SelectionLayer.RUN,
                harness=driver.provider,
                model=None,
                effort="medium",
                model_pinned=False,
                effort_pinned=True,
                strict_effective=None,
            ),
        ),
        catalog=catalog,
    )
    driver.request["execution_selection"] = selection.to_state()
    raw = driver.receipt()
    assert raw["host"] is not None
    raw["host"]["observed_executable_version_at_launch"] = "999.0.0"
    with pytest.raises(GateHoldError, match="execution receipt"):
        live.check_receipt(driver.request, raw)


@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_host_default_keeps_native_host_unknown_and_uses_product_image_probe(
    setup_live: Any, harness: str
) -> None:
    live, driver = setup_live(harness)
    raw = driver.receipt()
    assert raw["host"] is None
    observation = live.check_receipt(driver.request, raw)
    assert raw["host"] is None
    assert observation["native_host_observation"] is None
    assert observation["observed_executable_version"] == driver.observed_image_version
    assert observation["runner_receipt_digest"] == raw["runner"]["receipt_digest"]


def test_host_default_still_rejects_an_incompatible_observed_image_probe(setup_live: Any) -> None:
    live, driver = setup_live()
    driver.observed_image_version = "999.0.0"
    raw = driver.receipt()
    assert raw["host"] is None
    with pytest.raises(GateHoldError, match="runner receipt"):
        live.check_receipt(driver.request, raw)


def test_retained_uses_shared_byte_verified_artifact_copy(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import factory_gate_protocol

    live, driver = setup_live()
    original = factory_gate_protocol.retain_artifacts
    copied = []

    def retain(root: Path, directory: Path) -> dict[str, Any]:
        result = original(root, directory)
        copied.append((root, directory, result))
        return result

    monkeypatch.setattr(factory_gate_protocol, "retain_artifacts", retain)
    result = live.retained(
        str(driver.artifact_root), "completed-workflow", [str(driver.output_digest)]
    )
    assert len(copied) == 1
    assert result["raw_originals"] == copied[0][2]["raw_originals"]
    for path, digest in result["raw_originals"].items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest


def test_interrupted_cancellation_reconciles_without_any_launch(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, driver = setup_live()
    cancelled(live)
    live.close()
    slot = dict(driver.annotations["omp:cancellation"])
    driver.annotations.clear()
    driver.calls.clear()
    driver.advance_count = 0
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: live)
    result = live_module.reconcile_interrupted(live.gate, "omp:cancellation", slot)
    assert result["terminal_status"] == "cancelled"
    assert result["no_launch"] is True
    assert result["workflow"]["raw_absence"]["terminal_status"] == "cancelled"
    assert not driver.annotations and not driver.clients
    assert driver.advance_count == 0
    assert not any(
        argv[1:3] in (["engine", "start"], ["engine", "build-request"]) for argv in driver.calls
    )


@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_cancellation_persists_first_event_and_intent_before_cancel(
    setup_live: Any, harness: str
) -> None:
    live, driver = setup_live(harness)
    command = live.run_command
    observed = []

    def check_cancel(argv: list[str], **kwargs: Any) -> Any:
        if argv[1:3] == ["engine", "cancel"]:
            annotation = driver.annotations[harness + ":cancellation"]
            assert annotation["first_event"]["kind"] == "worker.started"
            assert annotation["cancel_issued"] is True
            observation = annotation["worker_observation"]
            assert observation["container_id"] == "engine-observed-worker-id"
            assert observation["create_event"]["Actor"]["ID"] == observation["container_id"]
            assert observation["engine_since"] == "1792281600.000000000"
            observed.append(dict(annotation))
        return command(argv, **kwargs)

    live.run_command = check_cancel
    result = cancelled(live)
    assert len(observed) == 1
    assert result["cancel_status"] == result["reconcile_status"] == "cancelled"
    assert driver.annotations[harness + ":cancellation"]["cancel_status"] == "cancelled"


@pytest.mark.parametrize(
    "failure",
    [
        "custody",
        "token",
        "engine",
        "relay",
        "missing-policy",
        "policy",
        "origin",
        "snapshot",
        "existing-run",
        "run-hold",
        "product-hold",
        "cancel-qualification",
        "event-clock",
    ],
)
def test_preflight_failures_do_not_reserve_a_worker_slot(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    live, driver = setup_live()
    role = "cancellation" if failure == "cancel-qualification" else "completed-workflow"

    def hold(*args: Any, **kwargs: Any) -> None:
        raise GateHoldError("unavailable qualification prerequisite")

    if failure == "custody":
        monkeypatch.setattr(live_module, "load_live_custody", hold)
    elif failure == "token":
        live.options.gateway_token_file.write_text("")
    elif failure == "engine":
        driver.engine.kind = "wrong-engine"
    elif failure == "relay":
        live.relay_factory = lambda engine: SimpleNamespace(
            container_name="relay",
            up=lambda **kwargs: SimpleNamespace(healthy=False, to_state=lambda: {"healthy": False}),
        )
    elif failure == "missing-policy":
        (driver.project / ".enginery" / "project.toml").unlink()
    elif failure == "policy":
        driver.effective_repair_limit = 1
    elif failure == "origin":
        command = live.command

        def wrong_origin(argv: Any, **kwargs: Any) -> Any:
            if list(argv[:4]) == ["git", "remote", "get-url", "origin"]:
                return "https://github.com/other/repository"
            return command(argv, **kwargs)

        live.command = wrong_origin
    elif failure == "snapshot":
        snapshot = json.loads(driver.initial_issue)
        snapshot["title"] = "Changed task"
        driver.initial_issue = json.dumps(snapshot)
    elif failure == "existing-run":
        driver.project_runs = [{"issue": "owner/repo#1", "run_id": "unbound-run"}]
    elif failure == "run-hold":
        driver.qualification_holds = [{"reason": "owner action required"}]
    elif failure == "product-hold":
        command = live.run_command

        def held_product(argv: list[str], **kwargs: Any) -> Any:
            result = command(argv, **kwargs)
            if argv[1:3] == ["engine", "evidence"]:
                evidence = json.loads(result.stdout)
                evidence["hold"] = {"reason": "owner action required"}
                result.stdout = json.dumps(evidence)
            return result

        live.run_command = held_product
    elif failure == "cancel-qualification":
        driver.cancel_qualification = "await_human_review"
    elif failure == "event-clock":
        monkeypatch.setattr(live_module, "engine_time", lambda gate: None)
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate: live)
    with pytest.raises(GateHoldError):
        live_module.preflight(live.gate, role)
    assert driver.reservations == []
    assert driver.annotations == {}
    assert not driver.clients and not driver.worker_started
    assert not any(argv[1] == "init" for argv in driver.calls)


def fresh_boundary(live: Any) -> Any:
    return live_module.LiveProtocol(
        live.gate,
        run_command=live.run_command,
        popen=live.popen,
        clock=live.clock,
        sleep=live.sleep,
        group_alive=live.group_alive,
        read_state=live.read_state,
        relay_factory=live.relay_factory,
    )


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
def test_qualified_crash_before_reservation_reuses_exact_request(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    live, driver = setup_live()
    live.preflight(role)
    assert driver.reservations == [] and driver.annotations == {}
    path = Path(live.qualified["request_path"])
    original = path.read_bytes()
    run_id, operation_id = live.run_id, live.operation_id
    qualification_calls = [
        argv
        for argv in driver.calls
        if argv[1] == "run" or argv[1:3] in (["engine", "build-request"], ["engine", "start"])
    ]
    recovered = fresh_boundary(live)
    driver.clock.now += 10
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate: recovered)
    assert live_module.preflight(live.gate, role) is recovered
    assert (recovered.run_id, recovered.operation_id) == (run_id, operation_id)
    assert path.read_bytes() == original
    assert driver.reservations == [] and not driver.worker_started
    assert [
        argv
        for argv in driver.calls
        if argv[1] == "run" or argv[1:3] in (["engine", "build-request"], ["engine", "start"])
    ] == qualification_calls
    assert recovered.deadline == driver.clock.now + recovered.budget
    recovered.gate.campaign.reserve("omp", role)
    result = (
        recovered.completed_workflow()
        if role == "completed-workflow"
        else recovered.cancellation(str(operation_id))
    )
    assert result["run_id"] == run_id
    assert result["worker_launches"] == ["engine-observed-worker-id"]
    assert driver.advance_count == (2 if role == "cancellation" else 0)


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
@pytest.mark.parametrize("drift", ["request", "binding", "supervisor", "create", "container"])
def test_qualified_crash_refuses_changed_or_previously_started_run_before_reservation(
    setup_live: Any, role: str, drift: str
) -> None:
    live, driver = setup_live()
    live.preflight(role)
    if drift == "request":
        path = Path(live.qualified["request_path"])
        path.write_bytes(path.read_bytes() + b" ")
    elif drift == "binding":
        live.binding["engine_kind"] = "another-engine"
        driver.engine.kind = "another-engine"
    elif drift == "supervisor":
        read_state = live.read_state

        def supervised(database: Path, run_id: str) -> dict[str, Any]:
            state = read_state(database, run_id)
            state["supervisors"] = [{"operation_id": live.operation_id}]
            return state

        live.read_state = supervised
    elif drift == "create":
        driver.worker_started = True
        read_state = live.read_state

        def created_without_supervisor(database: Path, run_id: str) -> dict[str, Any]:
            state = read_state(database, run_id)
            state["supervisors"] = []
            return state

        live.read_state = created_without_supervisor
    elif drift == "container":
        driver.engine.alive = True
    recovered = fresh_boundary(live)
    with pytest.raises(GateHoldError):
        recovered.preflight(role)
    assert driver.reservations == [] and driver.annotations == {}
    assert not driver.clients


def dispatched_cancellation(live: Any, driver: Driver) -> dict[str, Any]:
    live.preflight("cancellation")
    live.gate.campaign.reserve(live.harness, "cancellation")
    live._launch_qualified("cancellation")
    driver.annotate(
        live.harness + ":cancellation",
        remote_before=live.qualified["remote_before"],
        head_branch=live.qualified["head_branch"],
    )
    live.engine_action("next", "--advance")
    live.await_worker()
    return dict(driver.annotations[live.harness + ":cancellation"])


@pytest.mark.parametrize("markers", ["neither", "first-event-only", "cancel-only"])
def test_recovery_cleanup_cancel_never_qualifies_without_both_markers(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, markers: str
) -> None:
    live, driver = setup_live()
    slot = dispatched_cancellation(live, driver)
    if markers == "first-event-only":
        slot["first_event"] = live.await_worker()
    elif markers == "cancel-only":
        slot["cancel_issued"] = True
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: live)
    result = live_module.reconcile_interrupted(live.gate, "omp:cancellation", slot)
    assert driver.status == "cancelled"
    assert result["terminal_status"] == "failed"
    assert result["cancel_status"] is None
    assert result["event_count"] == (1 if markers == "first-event-only" else 0)
    assert "workflow" not in result
    assert sum(argv[1:3] == ["engine", "cancel"] for argv in driver.calls) == 1
    assert not driver.engine.alive
    assert driver.advance_count == 2


@pytest.mark.parametrize("cancel_committed", [False, True])
def test_recovered_cancellation_has_explicit_statuses_and_counts_events_not_supervisors(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, cancel_committed: bool
) -> None:
    live, driver = setup_live()
    slot = dispatched_cancellation(live, driver)
    slot.update(first_event=live.await_worker(), cancel_issued=True)
    if cancel_committed:
        live.engine_action("cancel", "--node-id", "implement")
    read_state = live.read_state

    def extra_supervisor(database: Path, run_id: str) -> dict[str, Any]:
        state = read_state(database, run_id)
        state["supervisors"] *= 2
        return state

    live.read_state = extra_supervisor
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: live)
    result = live_module.reconcile_interrupted(live.gate, "omp:cancellation", slot)
    assert result["terminal_status"] == "cancelled"
    assert result["cancel_status"] == result["reconcile_status"] == "cancelled"
    assert result["workflow"]["cancel_status"] == "cancelled"
    assert result["first_event"] == slot["first_event"]
    assert result["cancel_issued"] is True
    assert result["event_count"] == 1
    assert result["worker_launches"] == ["engine-observed-worker-id"]
    assert driver.advance_count == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "another-run"),
        ("principal_type", "agent"),
        ("principal_id", ""),
        ("os_uid", True),
        ("os_uid", None),
        ("tty_name", "not-a-terminal"),
        ("decided_at", ""),
        ("action", ""),
        ("superseded_by", "new-decision"),
        ("subject_fields", {"head_revision": "different-head"}),
    ],
)
def test_shared_human_predicate_rejects_unbound_terminal_decision(
    setup_live: Any, field: str, value: Any
) -> None:
    _live, driver = setup_live()
    decision = driver.read_state(driver.database, driver.run_id)["decisions"][0]
    assert live_module.valid_human_decision(decision, driver.run_id, driver.snapshot_head)
    decision[field] = value
    assert not live_module.valid_human_decision(decision, driver.run_id, driver.snapshot_head)


def test_live_launch_requires_preflight(setup_live: Any) -> None:
    live, driver = setup_live()
    with pytest.raises(GateHoldError, match="requires preflight"):
        live.completed_workflow()
    assert not driver.clients and not driver.reservations


def test_recovery_does_not_credit_exception_cleanup_even_with_cancel_intent(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, driver = setup_live()
    slot = dispatched_cancellation(live, driver)
    slot.update(first_event=live.await_worker(), cancel_issued=True)
    # A failed canonical cancel can leave intent durable before exception cleanup cancels.
    live.abort_worker()
    assert driver.status == "cancelled"
    assert live.cleanup_marker().is_file()
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: live)
    result = live_module.reconcile_interrupted(live.gate, "omp:cancellation", slot)
    assert result["terminal_status"] == "failed"
    assert result["cleanup_abort"] is True
    assert result["cancel_status"] is None
    assert result["event_count"] == 1
    assert "workflow" not in result


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
def test_crash_after_product_qualification_before_marker_recovers_from_durable_intent(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    live, driver = setup_live()

    class QualificationCrash(BaseException):
        pass

    def commit_then_crash(argv: list[str], **kwargs: Any) -> Any:
        result = driver.run(argv, **kwargs)
        qualified = (
            argv[1] == "run"
            if role == "completed-workflow"
            else argv[1:3] == ["engine", "next"] and "--advance" in argv
        )
        if qualified:
            raise QualificationCrash
        return result

    live.run_command = commit_then_crash
    with pytest.raises(QualificationCrash):
        live.preflight(role)
    root = live.gate.campaign.path.parent / "qualified"
    assert (root / f"omp-{role}.intent.json").is_file()
    assert not (root / f"omp-{role}.json").exists()
    assert driver.status == "qualified"
    assert not driver.reservations and not driver.annotations and not driver.worker_started
    original_request = driver.request_path.read_bytes()
    run_id, operation_id = driver.run_id, driver.operation_id
    calls = list(driver.calls)
    live.run_command = driver.run
    recovered = fresh_boundary(live)
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate: recovered)
    assert live_module.preflight(live.gate, role) is recovered
    assert (root / f"omp-{role}.json").is_file()
    assert (recovered.run_id, recovered.operation_id) == (run_id, operation_id)
    assert driver.request_path.read_bytes() == original_request
    assert not driver.reservations and not driver.annotations and not driver.worker_started
    assert not any(
        argv[1] == "run"
        or argv[1:3] in (["engine", "build-request"], ["engine", "start"])
        or (argv[1:3] == ["engine", "next"] and "--advance" in argv)
        for argv in driver.calls[len(calls) :]
    )
    recovered.gate.campaign.reserve("omp", role)
    result = (
        recovered.completed_workflow()
        if role == "completed-workflow"
        else recovered.cancellation(operation_id)
    )
    assert result["run_id"] == run_id
    assert result["worker_launches"] == ["engine-observed-worker-id"]
    assert driver.advance_count == (2 if role == "cancellation" else 0)


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
@pytest.mark.parametrize(
    "drift",
    ["issue", "base", "operation", "repository", "bound-content", "supervisor", "create"],
)
def test_intent_recovery_refuses_mismatched_or_dispatched_product_request_without_reserving(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, role: str, drift: str
) -> None:
    live, driver = setup_live()
    publish = live._write_qualification

    class QualificationCrash(BaseException):
        pass

    def intent_only(path: Path, record: dict[str, Any]) -> None:
        if not path.name.endswith(".intent.json"):
            raise QualificationCrash
        publish(path, record)

    monkeypatch.setattr(live, "_write_qualification", intent_only)
    with pytest.raises(QualificationCrash):
        live.preflight(role)
    request = json.loads(driver.request_path.read_text())
    if drift == "issue":
        request["work_item"]["data"]["external_reference"] = "owner/repo#2"
    elif drift == "base":
        request["run"]["data"]["base_revision"] = "e" * 40
    elif drift == "operation":
        request["implementation"]["operation_id"] = "different-operation"
    elif drift == "repository":
        request["repository_path"] = str(driver.root / "another-project")
    elif drift == "bound-content":
        request["required_checks"] = ["not-the-qualified-request"]
        request["request_digest"] = str(
            Digest.of_json(
                {
                    name: value
                    for name, value in request.items()
                    if name not in {"status", "request_digest", "effective_config"}
                }
            )
        )
    elif drift == "supervisor":
        read_state = live.read_state

        def supervised(database: Path, run_id: str) -> dict[str, Any]:
            state = read_state(database, run_id)
            state["supervisors"] = [{"operation_id": driver.operation_id}]
            return state

        live.read_state = supervised
    elif drift == "create":
        driver.worker_started = True
        read_state = live.read_state

        def created_without_supervisor(database: Path, run_id: str) -> dict[str, Any]:
            state = read_state(database, run_id)
            state["supervisors"] = []
            return state

        live.read_state = created_without_supervisor
    driver.request_path.write_text(json.dumps(request))
    recovered = fresh_boundary(live)
    with pytest.raises(GateHoldError):
        recovered.preflight(role)
    assert driver.reservations == [] and driver.annotations == {}
    assert not driver.clients


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
@pytest.mark.parametrize("unrelated_creates", [False, True])
def test_uncommitted_qualification_failure_requalifies_without_spending_slot(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, role: str, unrelated_creates: bool
) -> None:
    live, driver = setup_live()
    attempts = []

    def temporary_failure(argv: list[str], **kwargs: Any) -> Any:
        qualifies = (
            argv[1] == "run"
            if role == "completed-workflow"
            else argv[1:3] == ["engine", "build-request"]
        )
        if qualifies:
            attempts.append(argv)
            if len(attempts) == 1:
                return subprocess.CompletedProcess(argv, 1, "", "temporary network failure")
        return driver.run(argv, **kwargs)

    live.run_command = temporary_failure
    with pytest.raises(GateHoldError, match="held or failed"):
        live.preflight(role)
    intent_path = live.gate.campaign.path.parent / "qualified" / f"omp-{role}.intent.json"
    original = json.loads(intent_path.read_text())
    assert not driver.project_runs
    assert not driver.database.exists() and not driver.request_path.exists()
    assert not driver.reservations and not driver.worker_started
    if unrelated_creates:
        engine_creates = live_module.engine_creates

        def observations(*args: Any) -> str:
            events = [
                {"Type": "container", "Action": "create", "Actor": {"Attributes": labels}}
                for labels in (
                    {"name": "unrelated-db"},
                    {
                        "name": "other-worker",
                        live_module.OPERATION_LABEL: "implement:other",
                        live_module.RUNNER_LABEL: "1",
                    },
                    {
                        "name": "unrecorded-store",
                        live_module.OPERATION_LABEL: "implement:unknown",
                        live_module.RUNNER_LABEL: "1",
                    },
                    {
                        "name": live_module.container_name_for("validate:0123456789abcdef"),
                        live_module.OPERATION_LABEL: "validate:0123456789abcdef",
                        live_module.RUNNER_LABEL: "1",
                    },
                    {
                        "name": "other-operation",
                        live_module.OPERATION_LABEL: "other:0123456789abcdef",
                        live_module.RUNNER_LABEL: "1",
                    },
                )
            ]
            return engine_creates(*args) + "\n" + "\n".join(map(json.dumps, events))

        monkeypatch.setattr(live_module, "engine_creates", observations)
    recovered = fresh_boundary(live)
    recovered.preflight(role)
    assert len(attempts) == 2
    assert recovered.qualified["role"] == role
    assert json.loads(intent_path.read_text()) == original
    assert not driver.reservations and not driver.worker_started
    recovered.gate.campaign.reserve("omp", role)
    result = (
        recovered.completed_workflow()
        if role == "completed-workflow"
        else recovered.cancellation(str(recovered.operation_id))
    )
    assert result["worker_launches"] == ["engine-observed-worker-id"]


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
@pytest.mark.parametrize("observations", ["create", "unavailable"])
def test_uncommitted_intent_cannot_requalify_without_proving_no_engine_create(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, role: str, observations: str
) -> None:
    live, driver = setup_live()

    def failure(argv: list[str], **kwargs: Any) -> Any:
        if argv[1] == "run" or argv[1:3] == ["engine", "build-request"]:
            return subprocess.CompletedProcess(argv, 1, "", "temporary failure")
        return driver.run(argv, **kwargs)

    live.run_command = failure
    with pytest.raises(GateHoldError):
        live.preflight(role)
    intent_path = live.gate.campaign.path.parent / "qualified" / f"omp-{role}.intent.json"
    original = intent_path.read_bytes()
    live.run_command = driver.run
    if observations == "create":
        operation = json.loads(original).get("operation_id", "implement:unrecorded")
        event = {
            "Type": "container",
            "Action": "create",
            "Actor": {
                "ID": "unrecorded-worker",
                "Attributes": {
                    "name": live_module.container_name_for(operation),
                    live_module.OPERATION_LABEL: operation,
                    live_module.RUNNER_LABEL: "1",
                },
            },
        }
        monkeypatch.setattr(live_module, "engine_creates", lambda *args: json.dumps(event))
    else:
        monkeypatch.setattr(live_module, "engine_creates", lambda *args: None)
    recovered = fresh_boundary(live)
    with pytest.raises(GateHoldError, match=r"engine create|engine observations"):
        recovered.preflight(role)
    assert intent_path.read_bytes() == original
    assert not driver.reservations and not driver.clients
    assert not any(
        argv[1] == "run" or argv[1:3] == ["engine", "build-request"] for argv in driver.calls
    )


@pytest.mark.parametrize("committed", ["request", "database"])
def test_partial_cancellation_qualification_never_discards_intent(
    setup_live: Any, committed: str
) -> None:
    live, driver = setup_live()

    def failure(argv: list[str], **kwargs: Any) -> Any:
        if argv[1:3] == ["engine", "build-request"]:
            return subprocess.CompletedProcess(argv, 1, "", "temporary failure")
        return driver.run(argv, **kwargs)

    live.run_command = failure
    with pytest.raises(GateHoldError):
        live.preflight("cancellation")
    intent_path = live.gate.campaign.path.parent / "qualified" / "omp-cancellation.intent.json"
    intent = json.loads(intent_path.read_text())
    Path(intent["request_path" if committed == "request" else "database"]).touch()
    live.run_command = driver.run
    with pytest.raises(GateHoldError, match="missing its durable product"):
        fresh_boundary(live).preflight("cancellation")
    assert json.loads(intent_path.read_text()) == intent
    assert not driver.reservations and not driver.worker_started
    assert not any(argv[1:3] == ["engine", "build-request"] for argv in driver.calls)


@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_post_cancel_read_failure_preserves_canonical_credit_and_status(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    live, driver = setup_live(harness)
    failed = []

    def remote_failure(argv: list[str], **kwargs: Any) -> Any:
        if argv[:2] == ["git", "ls-remote"] and driver.status == "cancelled" and not failed:
            failed.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "temporary network failure")
        return driver.run(argv, **kwargs)

    live.run_command = remote_failure
    with pytest.raises(GateHoldError, match="held or failed"):
        cancelled(live)
    assert len(failed) == 1
    assert not live.cleanup_marker().exists()
    slot = dict(driver.annotations[harness + ":cancellation"])
    assert slot["cancel_status"] == driver.status == "cancelled"
    driver.annotations.clear()
    driver.calls.clear()
    live.run_command = driver.run
    recovered = fresh_boundary(live)
    recovered.gate.effects["isolated_containers"] = 0
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: recovered)
    result = live_module.reconcile_interrupted(live.gate, harness + ":cancellation", slot)
    assert result["terminal_status"] == "cancelled"
    assert result["cancel_status"] == result["reconcile_status"] == "cancelled"
    assert result["workflow"]["cancel_status"] == "cancelled"
    assert result["cleanup_abort"] is False
    assert not driver.annotations
    assert recovered.gate.effects["isolated_containers"] == 0
    assert not any(argv[1:3] == ["engine", "cancel"] for argv in driver.calls)


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
@pytest.mark.parametrize("history", ["intact", "lost", "replaced", "unavailable", "unrecorded"])
def test_recovery_counts_durable_create_or_finishes_truthfully_unprovable(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, role: str, history: str
) -> None:
    live, driver = setup_live()
    result = completed(live) if role == "completed-workflow" else cancelled(live)
    assert result["worker_launches"] == ["engine-observed-worker-id"]
    assert live.gate.effects["isolated_containers"] == 1
    slot = dict(driver.annotations["omp:" + role])
    observation = slot["worker_observation"]
    assert observation["container_id"] == observation["create_event"]["Actor"]["ID"]
    if history == "unrecorded":
        slot.pop("worker_observation")
    elif history == "lost":
        driver.engine.creates = 0
    elif history == "replaced":
        engine_cli = driver.engine.run_cli

        def replaced(argv: Any, **kwargs: Any) -> Any:
            result = engine_cli(argv, **kwargs)
            if argv[0] == "events":
                result.stdout = result.stdout.replace(
                    "engine-observed-worker-id", "later-worker-id"
                )
            return result

        monkeypatch.setattr(driver.engine, "run_cli", replaced)
    elif history == "unavailable":
        engine_cli = driver.engine.run_cli

        def unavailable(argv: Any, **kwargs: Any) -> Any:
            if argv[0] == "events":
                return subprocess.CompletedProcess(argv, 1, "", "engine history unavailable")
            return engine_cli(argv, **kwargs)

        monkeypatch.setattr(driver.engine, "run_cli", unavailable)
    driver.calls.clear()
    driver.engine.calls.clear()
    driver.annotations.clear()
    driver.clients.clear()
    recovered = fresh_boundary(live)
    recovered.gate.effects["isolated_containers"] = 0
    recovered.popen = lambda argv, **kwargs: SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: recovered)
    result = live_module.reconcile_interrupted(live.gate, "omp:" + role, slot)
    assert result["no_launch"] is True
    assert result["container_gone"] and result["group_gone"]
    assert recovered.gate.effects["isolated_containers"] == 0
    assert not driver.annotations and not driver.clients
    assert not recovered.cleanup_marker().exists()
    assert not any(
        argv[1] == "run"
        or argv[1:3] in (["engine", "start"], ["engine", "build-request"], ["engine", "cancel"])
        or (argv[1:3] == ["engine", "next"] and "--advance" in argv)
        for argv in driver.calls
    )
    if history == "intact":
        expected = "completed" if role == "completed-workflow" else "cancelled"
        assert result["terminal_status"] == expected
        assert result["workflow"]["worker_launches"] == ["engine-observed-worker-id"]
        windows = [
            args[args.index("--since") + 1] for args in driver.engine.calls if args[0] == "events"
        ]
        assert windows == [observation["engine_since"]] * (2 if role == "completed-workflow" else 1)
    else:
        assert result["terminal_status"] == "failed"
        assert result["worker_launches"] is None
        assert result["worker_count_status"] == "unprovable"
        assert result["worker_count_reason"]
        assert result["product_terminal_status"] == driver.status
        assert "workflow" not in result
        if role == "cancellation":
            assert result["cancel_status"] == "cancelled"


def test_worker_create_observation_precedes_deliberate_coordinator_kill(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, driver = setup_live()
    kill = Client.kill
    observed = []

    def kill_after_observation(client: Client) -> None:
        annotation = driver.annotations["omp:completed-workflow"]["worker_observation"]
        assert annotation["container_id"] == "engine-observed-worker-id"
        assert annotation["create_event"]["Actor"]["ID"] == annotation["container_id"]
        observed.append(annotation)
        kill(client)

    monkeypatch.setattr(Client, "kill", kill_after_observation)
    completed(live)
    assert len(observed) == 1
    assert driver.annotations["omp:completed-workflow"]["worker_observation"] == observed[0]


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
def test_post_observation_second_create_is_not_hidden_by_stored_worker(
    setup_live: Any, role: str
) -> None:
    live, driver = setup_live()
    if role == "completed-workflow":
        completed(live)
    else:
        cancelled(live)
    assert live.gate.effects["isolated_containers"] == 1
    driver.engine.creates = 2
    with pytest.raises(GateHoldError, match="exactly one worker create, observed 2"):
        live.count_workers()
    assert live.gate.effects["isolated_containers"] == 2


def event_after(anchor: dict[str, Any], index: int, action: str = "exec_die") -> dict[str, Any]:
    event = json.loads(json.dumps(anchor))
    event["timeNano"] += index
    event["Action"] = action
    return dict(event)


def rolling_checkpoint(live: Any, driver: Driver, index: int) -> dict[str, Any]:
    anchor = live.worker_observation["create_event"]
    checkpoint = event_after(anchor, index)
    previous = anchor if driver.engine.event_history is None else driver.engine.event_history[-1]
    driver.engine.event_history = [previous, checkpoint]
    assert live.count_workers() == ["engine-observed-worker-id"]
    return checkpoint


def test_rolling_coverage_survives_initial_create_eviction(setup_live: Any) -> None:
    live, driver = setup_live()
    completed(live)
    rolling_checkpoint(live, driver, 1)
    final = rolling_checkpoint(live, driver, 2)
    assert live.worker_observation["create_event"] not in driver.engine.event_history
    coverage = json.loads(Path(live.worker_observation["event_coverage_path"]).read_text())
    assert coverage["checkpoint"] == final
    assert len(coverage["windows"]) == 3
    assert live.gate.effects["isolated_containers"] == 1


def test_rolling_coverage_rejects_later_duplicate_create(setup_live: Any) -> None:
    live, driver = setup_live()
    completed(live)
    checkpoint = rolling_checkpoint(live, driver, 1)
    anchor = live.worker_observation["create_event"]
    driver.engine.event_history = [checkpoint, event_after(anchor, 2, "create")]
    with pytest.raises(GateHoldError, match="exactly one worker create, observed 2"):
        live.count_workers()
    assert live.gate.effects["isolated_containers"] == 2


@pytest.mark.parametrize("missing", ["index", "window", "middle", "overlap", "replaced"])
def test_rolling_coverage_rejects_missing_checkpoint_or_gap(setup_live: Any, missing: str) -> None:
    live, driver = setup_live()
    completed(live)
    rolling_checkpoint(live, driver, 1)
    checkpoint = rolling_checkpoint(live, driver, 2)
    path = Path(live.worker_observation["event_coverage_path"])
    coverage = json.loads(path.read_text())
    if missing == "index":
        path.unlink()
    elif missing == "window":
        Path(coverage["windows"][-1]["path"]).unlink()
    elif missing == "middle":
        coverage["windows"].pop(1)
        path.write_text(json.dumps(coverage))
    elif missing == "overlap":
        driver.engine.event_history = [event_after(checkpoint, 1)]
    else:
        replacement = dict(checkpoint, Action="replacement")
        driver.engine.event_history = [replacement]
    with pytest.raises(GateHoldError, match=r"checkpoint|coverage"):
        live.count_workers()


def test_rolling_coverage_rejects_tampered_window(setup_live: Any) -> None:
    live, driver = setup_live()
    completed(live)
    rolling_checkpoint(live, driver, 1)
    path = Path(live.worker_observation["event_coverage_path"])
    coverage = json.loads(path.read_text())
    window = Path(coverage["windows"][0]["path"])
    window.write_text(window.read_text().replace('"create"', '"exec_die"'))
    with pytest.raises(GateHoldError, match="retained engine event coverage"):
        live.count_workers()


@pytest.mark.parametrize("gap", [False, True])
def test_reconcile_rolling_coverage_uses_last_durable_checkpoint(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, gap: bool
) -> None:
    live, driver = setup_live()
    completed(live)
    checkpoint = rolling_checkpoint(live, driver, 1)
    # Serialize like the real manifest, not a shared reference to the original coordinator.
    slot = json.loads(json.dumps(driver.annotations["omp:completed-workflow"]))
    next_event = event_after(checkpoint, 1)
    driver.engine.event_history = [next_event] if gap else [checkpoint, next_event]
    recovered = fresh_boundary(live)
    recovered.popen = lambda argv, **kwargs: SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: recovered)
    driver.engine.calls.clear()
    driver.annotations.clear()
    result = live_module.reconcile_interrupted(live.gate, "omp:completed-workflow", slot)
    assert not driver.annotations, "reconciliation already owns the campaign lock"
    assert result["no_launch"]
    if gap:
        assert result["terminal_status"] == "failed"
        assert result["worker_count_status"] == "unprovable"
    else:
        assert result["terminal_status"] == "completed"
        assert result["workflow"]["worker_launches"] == ["engine-observed-worker-id"]
        queries = [args for args in driver.engine.calls if args[0] == "events"]
        assert queries[0][queries[0].index("--since") + 1] == live._create_boundary(checkpoint)
        path = Path(slot["worker_observation"]["event_coverage_path"])
        assert json.loads(path.read_text())["checkpoint"] == next_event
        # A second crash must use the checkpoint written during reconciliation, not the slot.
        driver.engine.event_history = [next_event, event_after(next_event, 1)]
        result = live_module.reconcile_interrupted(live.gate, "omp:completed-workflow", slot)
        assert result["terminal_status"] == "completed"


def test_reconcile_rolling_coverage_keeps_original_evidence_directory(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    live, driver = setup_live()
    completed(live)
    checkpoint = rolling_checkpoint(live, driver, 1)
    slot = json.loads(json.dumps(driver.annotations["omp:completed-workflow"]))
    path = Path(slot["worker_observation"]["event_coverage_path"])
    next_event = event_after(checkpoint, 1)
    driver.engine.event_history = [checkpoint, next_event]
    # Any invocation can reconcile every dead slot, including another harness's evidence run.
    live.gate.options.raw_dir = live.gate.options.raw_dir.parent / "protocol-claude.raw"
    recovered = fresh_boundary(live)
    recovered.popen = lambda argv, **kwargs: SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0)
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: recovered)
    result = live_module.reconcile_interrupted(live.gate, "omp:completed-workflow", slot)
    assert result["terminal_status"] == "completed"
    assert result["workflow"]["worker_launches"] == ["engine-observed-worker-id"]
    assert not recovered.cleanup_marker().exists()
    coverage = json.loads(path.read_text())
    assert coverage["checkpoint"] == next_event
    assert all(Path(window["path"]).parent == path.parent for window in coverage["windows"])
    assert not (recovered.directory / path.name).exists()


@pytest.mark.parametrize("invalid", ["relative", "wrong-name", "non-string"])
def test_rolling_coverage_rejects_invalid_durable_index_location(
    setup_live: Any, invalid: str
) -> None:
    live, _driver = setup_live()
    completed(live)
    observation = live.worker_observation
    path = Path(observation["event_coverage_path"])
    observation["event_coverage_path"] = {
        "relative": path.name,
        "wrong-name": str(path.with_name("other.json")),
        "non-string": None,
    }[invalid]
    with pytest.raises(GateHoldError, match="coverage checkpoint"):
        live.count_workers()


def test_owner_wait_polls_readonly_state_without_repeated_continue_clients(
    setup_live: Any,
) -> None:
    live, driver = setup_live()
    driver.decisions = False
    statuses: list[float] = []
    waits: list[float] = []
    run = driver.run

    def owner_wait(argv: list[str], **kwargs: Any) -> Any:
        if argv[1] == "status" and argv[2] == driver.run_id and driver.resumed:
            statuses.append(driver.clock.now)
            if not driver.decisions:
                driver.status = "passed"
                driver.engine.alive = False
                driver.clients[-1].returncode = 0
                driver.resumed = False
                result = run(argv, **kwargs)
                driver.resumed = True
                report = json.loads(result.stdout)
                report["next_decision"] = {"action": "approve"}
                result.stdout = json.dumps(report)
                return result
        return run(argv, **kwargs)

    def sleep(seconds: float) -> None:
        assert len(driver.clients) == 2, "an unchanged owner wait must not spawn continue clients"
        waits.append(seconds)
        driver.clock.sleep(seconds)
        anchor = live.worker_observation["create_event"]
        event = event_after(anchor, len(waits))
        history = driver.engine.event_history or [anchor]
        driver.engine.event_history = [*history, event][-8:]
        if driver.clock.now >= 12:
            driver.decisions = True  # The node remains passed; only the owner decision changes.

    live.run_command = owner_wait
    live.sleep = sleep
    result = completed(live)
    assert result["worker_launches"] == ["engine-observed-worker-id"]
    assert len(driver.clients) == 2
    assert statuses[-1] >= 12 and len(statuses) <= 3
    assert all(0 < seconds <= 1 for seconds in waits)
    assert live.worker_observation["create_event"] not in driver.engine.event_history
    coverage = json.loads(Path(live.worker_observation["event_coverage_path"]).read_text())
    assert len(coverage["windows"]) >= 3


def test_live_issue_snapshot_ignores_volatile_activity_metadata(setup_live: Any) -> None:
    live, driver = setup_live()
    snapshot = json.loads(driver.initial_issue)
    snapshot.update(
        updated_at="2026-10-18T01:00:00Z",
        comments=19,
        node_id="volatile-api-metadata",
    )
    driver.initial_issue = json.dumps(snapshot, indent=2) + "\n"
    assert live._verify_issue_snapshot() == snapshot


def test_first_engine_kind_hold_leaves_binding_correctable(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import factory_gate_protocol as protocol
    from factory_gate_campaign import Campaign

    live, driver = setup_live()
    options = live.options
    options.live = True
    options.campaign_id = "first-binding"
    options.evidence_out = tmp_path / "protocol.json"
    monkeypatch.setattr(protocol, "campaign_binding", lambda options: dict(live.binding))

    def boundary(gate: Any) -> Any:
        live.gate = gate
        live.binding = gate.binding
        return live

    monkeypatch.setattr(live_module, "LiveProtocol", boundary)
    evidence = SimpleNamespace(details={})
    live.binding["engine_kind"] = "docker"
    with pytest.raises(GateHoldError, match=r"qualified engine kind"):
        protocol.ProtocolGate(options, evidence, live.gate.scrubber, live.gate.engine_factory)
    assert Campaign(options).read()["binding"] is None
    live.binding["engine_kind"] = driver.engine.kind
    gate = protocol.ProtocolGate(options, evidence, live.gate.scrubber, live.gate.engine_factory)
    assert gate.campaign.read()["binding"]["engine_kind"] == driver.engine.kind
    live.binding["engine_kind"] = "docker"
    with pytest.raises(GateHoldError, match=r"campaign binding mismatch"):
        protocol.ProtocolGate(options, evidence, gate.scrubber, gate.engine_factory)


@pytest.mark.parametrize(
    "failure",
    [
        MissingPrerequisiteError("engine unavailable"),
        OSError("engine executable unavailable"),
        ValueError("invalid engine response"),
        KeyError("image"),
        subprocess.SubprocessError("engine command failed"),
    ],
)
def test_first_binding_prerequisite_failure_records_hold(
    setup_live: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception
) -> None:
    import factory_gate_protocol as protocol
    import run_factory_gate
    from factory_gate_campaign import Campaign

    live, driver = setup_live()
    options = live.options
    options.live = True
    options.scenario = "protocol"
    options.campaign_id = "first-binding-failure"
    options.subject_commit = None
    options.docker_context = None
    options.evidence_out = tmp_path / "protocol.json"
    monkeypatch.setattr(protocol, "campaign_binding", lambda options: dict(live.binding))
    monkeypatch.setattr(run_factory_gate, "_gateway_prerequisites", lambda options: None)

    def unavailable() -> None:
        raise failure

    monkeypatch.setattr(driver.engine, "qualify", unavailable)

    def boundary(gate: Any) -> Any:
        live.gate = gate
        live.binding = gate.binding
        return live

    monkeypatch.setattr(live_module, "LiveProtocol", boundary)
    monkeypatch.setattr(run_factory_gate, "parse_options", lambda *args: options)
    code = run_factory_gate.main([])
    document = json.loads(options.evidence_out.read_text())
    assert (document["verdict"], code) == ("hold", 3)
    assert "live prerequisite failed before binding" in document["reason"]
    assert Campaign(options).read()["binding"] is None
    assert not driver.reservations and not driver.worker_started


@pytest.mark.parametrize("remaining_lifetime", [0, -1])
@pytest.mark.parametrize(
    ("role", "cancel_issued"),
    [("completed-workflow", False), ("cancellation", False), ("cancellation", True)],
)
def test_expired_interrupted_worker_is_cleaned_and_reconciled_once(
    setup_live: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    cancel_issued: bool,
    remaining_lifetime: int,
) -> None:
    live, driver = setup_live()
    key = f"omp:{role}"
    if role == "completed-workflow":
        live.preflight(role)
        live.gate.campaign.reserve("omp", role)
        live._launch_qualified(role)
        client = live.spawn_continue()
        first_event = live.await_worker(client)
        client.kill()
        client.wait(timeout=10)
        driver.annotate(key, coordinator_kills=1, first_event=first_event)
        slot = dict(driver.annotations[key])
    else:
        slot = dispatched_cancellation(live, driver)
        if cancel_issued:
            slot.update(first_event=live.await_worker(), cancel_issued=True)
    assert driver.engine.alive
    slot["deadline_at"] = 10000 + remaining_lifetime
    monkeypatch.setattr(live_module.time, "time", lambda: 10000)
    recovered = fresh_boundary(live)
    recovered.read_state = None
    monkeypatch.setattr(live_module, "bound_interpreter", lambda _: "/fixture/python")

    def command(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == "/fixture/python":
            state = driver.read_state(Path(argv[-2]), argv[-1])
            return subprocess.CompletedProcess(argv, 0, json.dumps(state), "")
        return driver.run(argv, **kwargs)

    recovered.run_command = command
    recovered.popen = lambda *_args, **_kwargs: pytest.fail("expired worker was resumed")
    clients_before = len(driver.clients)
    advances_before = driver.advance_count
    reservations_before = list(driver.reservations)
    monkeypatch.setattr(live_module, "LiveProtocol", lambda gate, **kwargs: recovered)
    result = live_module.reconcile_interrupted(live.gate, key, slot)
    assert result["terminal_status"] == "failed"
    assert result["cleanup_abort"] is True
    assert recovered.cleanup_marker().is_file()
    assert result["container_gone"] and result["group_gone"]
    assert result["no_launch"] is True
    assert "workflow" not in result
    assert driver.status == "cancelled"
    assert not driver.engine.alive
    assert recovered.deadline == driver.clock() + recovered.budget
    assert len(driver.clients) == clients_before
    assert driver.advance_count == advances_before
    assert driver.reservations == reservations_before
