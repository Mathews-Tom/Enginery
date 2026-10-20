"""Installed, isolated product lifecycle qualification (never an alternate harness runner).

``project_paths`` may select one prepared clone per harness; ``project_path`` is its default.
``validation_commands`` contains shell-split command strings, not argv arrays. Existing project
policies must select the bound harness, image, Docker context and relay, with repair_limit=0.
The owner must prepare the project policy, commit behavioral acceptance and supply real
terminal decisions while the bounded journey waits; this module never decides or initializes.
The first cancellation event is the durable supervisor/container-start event, not a claim that
provider JSON is streamed: the pinned worker publishes merged output only when it exits.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from enginery.adapters.oci.engine import OPERATION_LABEL, RUNNER_LABEL, ObjectNotFoundError
from enginery.adapters.oci.launch import container_name_for
from enginery.adapters.oci.relay import RelayManager
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.execution_controls import expected_native_arguments
from enginery.domain.digests import Digest
from enginery.domain.errors import OperationTimeoutError
from enginery.domain.execution_receipt import ExecutionReceipt
from enginery.domain.execution_selection import ExecutionSelection
from enginery.domain.runner import RunnerReceipt, RunnerSelection
from enginery.ledger.artifact_store import ArtifactStore
from factory_gate_auth import (
    MAX_DEADLINE_SECONDS,
    _events,
    _group_alive,
    _usage,
    count_created_containers,
    engine_creates,
    engine_time,
    later,
    load_live_custody,
)
from factory_gate_campaign import configuration, issue_binding, sha256
from factory_gate_context import GateHoldError
from factory_gate_host_reads import bound_interpreter, pr_snapshot
from factory_gate_project_probe import valid_human_decision

OWNER_POLL_SECONDS = 1.0
EVENT_CHECKPOINT_SECONDS = 5.0


class WorkerCountUnprovableError(GateHoldError):
    """A spent worker cannot be counted across a gap in the engine event history."""


class LiveProtocol:
    """Small injectable CLI/process boundary; tests replace it without hosted/OCI calls."""

    def __init__(
        self,
        gate: Any,
        *,
        run_command: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        group_alive: Callable[[int], bool] = _group_alive,
        read_state: Callable[[Path, str], dict[str, Any]] | None = None,
        relay_factory: Callable[..., Any] = RelayManager,
        harness: str | None = None,
    ) -> None:
        self.gate = gate
        self.options = gate.options
        self.binding = gate.binding
        self.config = configuration(self.options)
        self.harness = str(self.options.harness if harness is None else harness)
        self.provider = "claude-code" if self.harness == "claude" else "omp"
        paths = self.binding.get("project_paths", self.config.get("project_paths", {}))
        self.project = Path(paths.get(self.harness, self.binding["project_path"])).resolve()
        self.executable = self.binding["executable"]
        self.run_command, self.popen = run_command, popen
        self.clock, self.sleep, self.group_alive = clock, sleep, group_alive
        self.read_state, self.relay_factory = read_state, relay_factory
        self.engine = gate.engine_factory(context=self.options.docker_context)
        self.commands = gate.commands
        self.directory = self.options.raw_dir / (self.harness + "-live")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clients: list[Any] = []
        self.files: list[Any] = []
        self.sequence = 0
        self.deadline: float | None = None
        self.worker: dict[str, Any] | None = None
        self.operation_id: str | None = None
        self.database: Path | None = None
        self.run_id: str | None = None
        self.owner = "factory"
        self.budget = self.options.session_deadline_seconds
        if not 1 <= self.budget <= MAX_DEADLINE_SECONDS:
            raise GateHoldError("worker deadline must be between 1 and 900 seconds")
        commands = self.binding.get("validation_commands", self.config.get("validation_commands"))
        if (
            not isinstance(commands, list)
            or not commands
            or any(not isinstance(command, str) or not shlex.split(command) for command in commands)
        ):
            raise GateHoldError("validation_commands must be nonempty shell-split command strings")
        self.validation_commands = commands
        # Reserve cleanup time inside the outer deadline, rather than resetting on continue.
        self.worker_budget = max(1, self.budget - min(120, self.budget // 3))
        self.environment = dict(os.environ)
        self.environment.pop("PYTHONPATH", None)
        self.environment.pop("PYTHONHOME", None)
        if self.options.docker_context is not None:
            self.environment["DOCKER_CONTEXT"] = self.options.docker_context
        self.since: str | None = None
        self.identity: dict[str, Any] | None = None
        self.qualified: dict[str, Any] | None = None
        self.runner_profile: dict[str, Any] | None = None
        self.worker_observation: dict[str, Any] | None = None
        self.last_event_checkpoint = 0.0
        self.counted_worker_creates = 0
        self.recovering = False

    def _save(self, name: str, value: Any, *, directory: Path | None = None) -> Path:
        path = (self.directory if directory is None else directory) / name
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, sort_keys=True, indent=2, default=str) + "\n"
        )
        hits = self.gate.scrubber.contains(text)
        text = self.gate.scrubber.text(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if hits:
            raise GateHoldError(
                "secret in live protocol output; retained text scrubbed; qualification held"
            )
        return path

    def remaining(self) -> float:
        if self.deadline is None:
            return 30.0
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise GateHoldError("live worker lifetime deadline reached; no relaunch is permitted")
        return remaining

    def command(
        self, argv: Sequence[str], *, allowed: tuple[int, ...] = (0,), json_output: bool = True
    ) -> Any:
        self.sequence += 1
        self.commands.append(shlex.join(argv))
        try:
            result = self.run_command(
                list(argv),
                cwd=self.project,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=min(30.0, self.remaining()),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise GateHoldError(
                f"installed live command failed: {shlex.join(argv[:3])}: {error}"
            ) from error
        self._save(
            f"command-{self.sequence:03d}.json",
            {
                "argv": list(argv),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
        if result.returncode not in allowed:
            raise GateHoldError(
                f"installed live command held or failed ({result.returncode}); see {self.directory}"
            )
        if not json_output:
            return result.stdout
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise GateHoldError("installed command did not emit parseable JSON") from error

    def cli(self, *argv: str, allowed: tuple[int, ...] = (0,)) -> Any:
        return self.command([self.executable, *argv], allowed=allowed)

    def project_cli(self, command: str, *argv: str, allowed: tuple[int, ...] = (0,)) -> Any:
        return self.cli(command, *argv, "--project", str(self.project), "--json", allowed=allowed)

    def engine_cli(self, *argv: str, timeout: float = 30) -> Any:
        # Adapt the existing auth clock/event helpers to the real engine wrapper.
        try:
            return self.engine.run_cli(argv, timeout=min(timeout, self.remaining()))
        except OperationTimeoutError:
            # A command cut short by the worker lifetime deadline is that deadline's hold,
            # not an unexpected engine failure; remaining() raises it once the time is spent.
            self.remaining()
            raise

    def prepare(self) -> None:
        assert self.options.gateway_url is not None and self.options.gateway_token_file is not None
        load_live_custody(
            self.options.custody_receipt,
            gateway_url=self.options.gateway_url,
            token_file=self.options.gateway_token_file,
        )
        token = self.options.gateway_token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise GateHoldError("gateway token file is empty")
        self.gate.scrubber.register("gateway-token", token)
        self.gate.effects["hosted_inference_calls"] = None
        self.gate.effects["hosted_inference_observation"] = (
            "not exposed by product receipt; usage is recorded from this run's "
            "retained harness output"
        )
        identity = self.engine.qualify()
        if identity.kind != self.binding.get("engine_kind"):
            raise GateHoldError("qualified engine kind differs from the immutable campaign binding")
        self.identity = identity.to_state()
        self._save("engine-identity.json", self.identity)
        relay = self.relay_factory(self.engine)
        relay_status = relay.up(
            relay_image=self.options.relay_image,
            upstream=self.options.gateway_url,
            token_file=self.options.gateway_token_file,
            recreate=False,
        )
        self._save("relay-status.json", relay_status.to_state())
        if not relay_status.healthy:
            raise GateHoldError("bound relay is not healthy; no worker launched")
        if not (self.project / ".enginery" / "project.toml").is_file():
            raise GateHoldError("prepare the project policy before launch; project.toml is missing")
        explanation = self.project_cli("explain", allowed=(0, 5))
        fields = {row["name"]: row["value"] for row in explanation.get("fields", [])}
        expected = {
            "harness": self.provider,
            "runner_mode": "isolated-oci",
            "runner_image": self.options.runner_image,
            "model_access": "relay",
            "docker_context": self.options.docker_context,
            "repair_limit": 0,
        }
        expected["validation_commands"] = [
            shlex.split(command) for command in self.validation_commands
        ]
        wrong = [key for key, value in expected.items() if fields.get(key) != value]
        if wrong:
            raise GateHoldError(
                "prepare the project policy before launch; effective configuration mismatch: "
                + ", ".join(wrong)
            )
        if explanation.get("holds"):
            raise GateHoldError(
                "project effective configuration is held; see retained explain output"
            )
        remote = self.command(["git", "remote", "get-url", "origin"], json_output=False).strip()
        repository = self.binding["repository"]
        if remote.rstrip("/").removesuffix(".git") not in {
            "https://github.com/" + repository,
            "ssh://git@github.com/" + repository,
            "git@github.com:" + repository,
        }:
            raise GateHoldError("project origin differs from the campaign repository allowlist")
        self.effective_config = explanation
        self.relay_name = relay.container_name

    def preflight(self, role: str) -> None:
        """Qualify a durable request without consuming a worker-session reservation."""
        if role not in {"completed-workflow", "cancellation"}:
            raise GateHoldError("unknown live protocol role")
        self.prepare()
        path = self.gate.campaign.path.parent / "qualified" / f"{self.harness}-{role}.json"
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            self._load_qualified(role, record)
            if role == "cancellation":
                remote = self.remote_effects(record["head_branch"])
                if remote != record["remote_before"] or remote["refs"] or remote["pull_requests"]:
                    raise GateHoldError("qualified cancellation has remote branch/PR effects")
            self._prove_no_worker()
            # No worker ever started, so a fresh launch lifetime is safe. Keep the original
            # engine observation boundary so a prior create can never disappear from proof.
            self.deadline = self.clock() + self.budget
            self.deadline_at = time.time() + self.budget
            return
        intent_path = path.with_suffix(".intent.json")
        if intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            self._check_qualification_binding(role, intent)
            self.owner = intent["owner"]
            self.since = intent["engine_since"]
            self.deadline = self.clock() + self.budget
            self.deadline_at = time.time() + self.budget
            record = self._recover_qualification(role, intent)
        else:
            record = None
        if record is None:
            if intent_path.exists():
                self._prove_no_intent_create(intent)
                intent_path.unlink()
            self.start_window()
            record = (
                self.qualify_completed(intent_path)
                if role == "completed-workflow"
                else self.qualify_cancellation(
                    f"campaign-{self.gate.campaign.campaign_id}-{self.harness}-cancellation",
                    intent_path,
                )
            )
        self._load_qualified(role, record)
        self._prove_no_worker()
        self._write_qualification(path, record)

    @staticmethod
    def _write_qualification(path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _begin_qualification(self, path: Path, role: str, **observations: Any) -> dict[str, Any]:
        intent = {
            "role": role,
            "binding": self.binding,
            "project_path": str(self.project),
            "owner": self.owner,
            "engine_since": self.since,
            "validation_commands": [shlex.split(command) for command in self.validation_commands],
            "worker_budget": self.worker_budget,
            **observations,
        }
        # This durable intent precedes the command that can commit product qualification,
        # including a crash after that commit but before its CLI response reaches this process.
        self._write_qualification(path, intent)
        return intent

    def _recover_qualification(self, role: str, intent: dict[str, Any]) -> dict[str, Any] | None:
        if role == "completed-workflow":
            status = self.project_cli("status", allowed=(0, 5))
            if status.get("holds") or "runs" not in status:
                raise GateHoldError("product status cannot prove qualification committed nothing")
            runs = [
                row
                for row in status.get("runs", [])
                if row.get("issue") == intent["external_reference"]
            ]
            if not runs:
                return None
            if any(not row.get("run_id") for row in runs):
                raise GateHoldError("qualification intent has an unidentified matching run")
            runs = [row for row in runs if row["run_id"] not in intent["existing_run_ids"]]
            if not runs:
                return None
            if len(runs) != 1:
                raise GateHoldError("qualification intent has no unique newly bound matching run")
            report = self.project_cli("status", runs[0]["run_id"], allowed=(0, 5))
            if report.get("holds") or report.get("next_action") != "implement":
                raise GateHoldError(
                    "qualification intent run is not qualified for its first worker"
                )
            run_id = runs[0]["run_id"]
            path, database = Path(report["request_path"]), Path(report["database"])
            observations: dict[str, Any] = {}
        else:
            run_id = intent["run_id"]
            path, database = Path(intent["request_path"]), Path(intent["database"])
            if not path.exists() and not database.exists():
                return None
            observations = {
                "remote_before": intent["remote_before"],
                "head_branch": intent["head_branch"],
            }
            remote = self.remote_effects(intent["head_branch"])
            if remote != intent["remote_before"] or remote["refs"] or remote["pull_requests"]:
                raise GateHoldError("qualification intent has remote cancellation effects")
        if not path.is_file() or not database.is_file():
            raise GateHoldError("qualification intent is missing its durable product request/run")
        request = json.loads(path.read_text(encoding="utf-8"))
        self._match_intent_request(intent, request, run_id)
        return self._qualification(role, path, database, **observations)

    def _prove_no_intent_create(self, intent: dict[str, Any]) -> None:
        """Discard only intents with no worker create they could own."""
        assert self.since is not None
        until = engine_time(self)
        end = None if until is None else later(until, 1)
        events = None if end is None else engine_creates(self, self.since, end)
        if events is None:
            raise GateHoldError("cannot discard qualification intent without engine observations")
        if intent["role"] == "cancellation":
            operation_id = intent["operation_id"]
            creates = count_created_containers(
                events,
                operation_id=operation_id,
                expected_name=container_name_for(operation_id),
                relay_name=self.relay_name,
            ).harness
        else:
            known = {
                slot.get("operation_id") for slot in self.gate.campaign.read()["slots"].values()
            }
            creates = [
                event
                for event in _events(events)
                if event.get("Type") == "container"
                and event.get("Action") == "create"
                and (labels := event.get("Actor", {}).get("Attributes", {})).get(RUNNER_LABEL)
                == "1"
                and labels.get(OPERATION_LABEL, "").startswith("implement:")
                and labels.get(OPERATION_LABEL) not in known
                and labels.get("name") != self.relay_name
                and not labels.get("name", "").endswith("-store")
            ]
        if creates:
            raise GateHoldError("qualification intent has an engine create; never requalify it")

    def _match_intent_request(
        self, intent: dict[str, Any], request: dict[str, Any], run_id: str
    ) -> None:
        work = request.get("work_item", {}).get("data", {})
        expected_operation = intent.get("operation_id", "implement:" + run_id)
        if (
            request.get("run_id") != run_id
            or request.get("implementation", {}).get("operation_id") != expected_operation
            or request.get("repository_path") != str(self.project)
            or request.get("execution_configuration", {}).get("github_repository")
            != self.binding["repository"]
            or work.get("external_reference") != intent["external_reference"]
            or request.get("run", {}).get("data", {}).get("base_revision")
            != intent["base_revision"]
            or request.get("base_branch") != intent["base_branch"]
            or request.get("head_branch") != intent.get("head_branch", "enginery/" + run_id)
            or request.get("validation_commands") != intent["validation_commands"]
            or request.get("implementation", {}).get("time_budget_seconds")
            != intent["worker_budget"]
        ):
            raise GateHoldError("product request does not exactly match prequalification intent")
        snapshot = intent.get("issue_snapshot")
        if snapshot is not None and (
            work.get("title") != snapshot["title"]
            or work.get("objective") != (snapshot.get("body") or snapshot["title"])
            or (
                snapshot.get("updated_at")
                and not request.get("source_revision", "").startswith(snapshot["updated_at"] + ":")
            )
        ):
            raise GateHoldError("product request differs from the intent's frozen issue snapshot")

    def _qualification(
        self, role: str, path: Path, database: Path, **observations: Any
    ) -> dict[str, Any]:
        return {
            "role": role,
            "binding": self.binding,
            "project_path": str(self.project),
            "request_path": str(path.resolve()),
            "request_sha256": sha256(path),
            "database": str(database.resolve()),
            "owner": self.owner,
            "engine_since": self.since,
            **observations,
        }

    def _check_qualification_binding(self, role: str, record: dict[str, Any]) -> None:
        if (
            record.get("role") != role
            or record.get("binding") != self.binding
            or record.get("project_path") != str(self.project)
            or not record.get("engine_since")
        ):
            raise GateHoldError("qualified request differs from this campaign/project/role")

    def _load_qualified(self, role: str, record: dict[str, Any]) -> None:
        self._check_qualification_binding(role, record)
        path = Path(record["request_path"])
        if not path.is_file() or sha256(path) != record.get("request_sha256"):
            raise GateHoldError("qualified request is missing or changed; never rebind it")
        request = json.loads(path.read_text(encoding="utf-8"))
        self._check_request(request)
        self.run_id = request["run_id"]
        self.operation_id = request["implementation"]["operation_id"]
        self.database = Path(record["database"])
        self.owner = record["owner"]
        self.since = record["engine_since"]
        if not self.database.is_file():
            raise GateHoldError("qualified product ledger is missing")
        if role == "cancellation" and self.operation_id != (
            f"campaign-{self.gate.campaign.campaign_id}-{self.harness}-cancellation"
        ):
            raise GateHoldError("qualified cancellation has a different operation identity")
        # initial_state() adds status/request_digest to the canonical request, and the CLI
        # adds effective_config. Compare every remaining byte-equivalent JSON field to the
        # digest observed from the durable product run, not merely a file's self-claimed hash.
        state = {
            name: value
            for name, value in request.items()
            if name not in {"status", "request_digest", "effective_config"}
        }
        product = self.engine_action("evidence")
        if product.get("hold") or product.get("holds"):
            raise GateHoldError("qualified product run is held before its first worker")
        observed_digest = product.get("request_digest")
        if (
            not observed_digest
            or request.get("request_digest") != observed_digest
            or str(Digest.of_json(state)) != observed_digest
        ):
            raise GateHoldError(
                "qualified request does not match the durable product request digest"
            )
        self.qualified = {**record, "request": request}

    def labelled_containers(self) -> list[str]:
        assert self.operation_id is not None
        listed = self.engine.run_cli(
            (
                "ps",
                "-a",
                "--filter",
                f"label={OPERATION_LABEL}={self.operation_id}",
                "--format",
                "{{.Names}}",
            ),
            timeout=30,
        )
        if listed.returncode:
            raise GateHoldError("cannot inspect operation-labelled containers")
        return listed.stdout.splitlines()

    def _prove_no_worker(self) -> None:
        """Absence requires both durable supervision and engine event/container evidence."""
        assert self.operation_id is not None and self.since is not None
        state = self.state()
        if state["supervisors"] or any(
            row.get("node_id") == "implement" for row in state["attempts"]
        ):
            raise GateHoldError("qualified run already has worker supervision or attempts")
        implementation = [row for row in state["nodes"] if row.get("node_id") == "implement"]
        qualification = [row for row in state["nodes"] if row.get("node_id") == "qualify"]
        if implementation or len(qualification) != 1 or qualification[0].get("status") != "passed":
            raise GateHoldError("qualified run is not awaiting its first implementation")
        containers = self.labelled_containers()
        until = engine_time(self)
        end = None if until is None else later(until, 1)
        events = None if end is None else engine_creates(self, self.since, end)
        if containers or events is None:
            raise GateHoldError("qualified run has a container or unavailable engine observations")
        observed = count_created_containers(
            events,
            operation_id=self.operation_id,
            expected_name=container_name_for(self.operation_id),
            relay_name=self.relay_name,
        )
        if observed.harness:
            raise GateHoldError("qualified run already has a worker create event; never relaunch")

    def _launch_qualified(self, role: str) -> dict[str, Any]:
        if self.qualified is None or self.qualified["role"] != role:
            raise GateHoldError("live protocol requires preflight before reservation")
        request = self.qualified["request"]
        self.annotate(
            self.harness + ":" + role,
            Path(self.qualified["request_path"]),
            request,
            database=Path(self.qualified["database"]),
        )
        return request

    def start_window(self) -> None:
        self.deadline = self.clock() + self.budget
        self.deadline_at = time.time() + self.budget
        self.since = engine_time(self)
        if self.since is None:
            raise GateHoldError("engine event clock unavailable; no uncounted worker may launch")

    def annotate(
        self, key: str, request_path: Path, request: dict[str, Any], *, database: Path
    ) -> None:
        self.run_id = request["run_id"]
        self.operation_id = request["implementation"]["operation_id"]
        self.database = database
        record = {
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "project_path": str(self.project),
            "database": str(database),
            "owner": self.owner,
            "request_path": str(request_path),
            "artifact_root": request["execution_configuration"]["artifact_root"],
            "engine_since": self.since,
            "deadline_seconds": self.budget,
        }
        record["deadline_at"] = self.deadline_at
        # Persist actual product operation label before the command that can launch a worker.
        self.gate.campaign.annotate(key, **record)
        self._save("operation-" + key.replace(":", "-") + ".json", record)

    def _check_request(self, request: dict[str, Any]) -> None:
        runner = request.get("runner", {})
        if (
            runner.get("trust_mode") != "isolated-oci"
            or runner.get("image") != self.options.runner_image
            or runner.get("model_access") != "relay"
            or runner.get("docker_context") != self.options.docker_context
        ):
            raise GateHoldError("durable request does not bind the exact isolated relay runner")
        if (
            request["execution_configuration"]["harness_provider"] != self.provider
            or request.get("repair_limit") != 0
        ):
            raise GateHoldError(
                "durable request must select this harness and prohibit second workers"
            )
        if not 1 <= request["implementation"]["time_budget_seconds"] <= self.worker_budget:
            raise GateHoldError("durable worker budget exceeds the session lifetime bound")
        selection = ExecutionSelection.from_state(request["execution_selection"])
        catalog = load_execution_catalog()
        if (
            selection.catalog_digest != catalog.digest
            or selection.host_protocol_fingerprint
            != catalog.host(self.provider).protocol_fingerprint
        ):
            raise GateHoldError("durable request does not bind the qualified harness catalog")
        for name, selected in (
            ("model_pins", selection.selected_model),
            ("effort_pins", selection.selected_effort),
        ):
            if self.binding.get(name, {}).get(self.harness) != selected:
                raise GateHoldError("durable request controls differ from immutable campaign pins")

    def check_receipt(self, request: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        selection = ExecutionSelection.from_state(request["execution_selection"])
        receipt = ExecutionReceipt.from_state(raw)
        catalog = load_execution_catalog()
        host = catalog.host(self.provider)
        if (
            receipt.harness != self.provider
            or receipt.selection_digest != selection.digest
            or selection.catalog_digest != catalog.digest
            or selection.host_protocol_fingerprint != host.protocol_fingerprint
            or receipt.submitted_arguments != expected_native_arguments(selection, catalog=catalog)
            or receipt.runner is None
            or receipt.runner.image_id != self.options.runner_image
            or receipt.runner.trust_mode != "isolated-oci"
        ):
            raise GateHoldError(
                "actual product execution receipt does not bind submitted controls/image version"
            )
        # Native HostObservation is intentionally absent for host-default selections. The
        # separately retained runner receipt records the image probe for every isolated launch.
        if receipt.host is not None and (
            receipt.host.tested_executable_version != host.tested_executable_version
            or receipt.host.observed_executable_version_at_launch != host.tested_executable_version
        ):
            raise GateHoldError("native execution receipt records a different image executable")
        store = ArtifactStore(
            Path(request["execution_configuration"]["artifact_root"]), create_directories=False
        )
        proof = RunnerReceipt.from_state(
            json.loads(store.read_bytes(receipt.runner.receipt_digest))
        )
        if (
            proof.digest != receipt.runner.receipt_digest
            or proof.operation_id != request["implementation"]["operation_id"]
            or proof.container_name != container_name_for(proof.operation_id)
            or proof.trust_mode.value != "isolated-oci"
            or proof.image.image_id != receipt.runner.image_id
            or proof.profile_digest != receipt.runner.profile_digest
            or proof.profile_digest != RunnerSelection.from_state(request["runner"]).profile_digest
            or proof.engine.kind != self.binding["engine_kind"]
            or proof.harness_version != host.tested_executable_version
            or proof.harness_protocol != host.protocol_fingerprint
            or dict(proof.image.harness_versions).get(self.provider) != proof.harness_version
        ):
            raise GateHoldError("product runner receipt does not prove the pinned image executable")
        # Decoding enforces observed-with-value or UNKNOWN-without-value. Do not fill either
        # effective value from the selected pin; these hosts expose no effective effort.
        if receipt.effective_effort.state.value != "unknown":
            raise GateHoldError("effective effort must remain explicit UNKNOWN for these hosts")
        for name, value in (
            ("model_pins", selection.selected_model),
            ("effort_pins", selection.selected_effort),
        ):
            pin = self.binding.get(name, {}).get(self.harness)
            if pin is not None and pin != value:
                raise GateHoldError("selected controls differ from immutable campaign pins")
        observation = {
            "source": (
                "digest-verified product RunnerReceipt from the adapter's in-image executable probe"
            ),
            "runner_receipt_digest": str(receipt.runner.receipt_digest),
            "image_id": proof.image.image_id,
            "observed_executable_version": proof.harness_version,
            "observed_protocol": proof.harness_protocol,
            "image_label_version": dict(proof.image.harness_versions).get(self.provider),
            "catalog_tested_version": host.tested_executable_version,
            "native_host_observation": raw.get("host"),
        }
        self.runner_profile = {
            "name": proof.profile_name,
            "profile_digest": str(proof.profile_digest),
            "effective": proof.effective.to_state(),
            "bounds": None if proof.bounds is None else proof.bounds.to_state(),
            "runner_receipt_digest": str(receipt.runner.receipt_digest),
        }
        self._save("image-harness-observation.json", observation)
        return observation

    def engine_action(self, command: str, *argv: str) -> dict[str, Any]:
        assert self.database is not None and self.run_id is not None
        return self.cli(
            "engine",
            command,
            "--database",
            str(self.database),
            "--owner",
            self.owner,
            "--run-id",
            self.run_id,
            *argv,
        )

    def spawn_continue(self) -> Any:
        assert self.run_id is not None
        argv = [
            self.executable,
            "continue",
            self.run_id,
            "--project",
            str(self.project),
            "--wait-seconds",
            str(max(0.0, self.remaining() - 1)),
            "--json",
        ]
        self.commands.append(shlex.join(argv))
        self.sequence += 1
        output = (self.directory / f"client-{self.sequence:03d}.log").open("w+")
        self.files.append(output)
        client = self.popen(
            argv,
            cwd=self.project,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.clients.append(client)
        return client

    def state(self) -> dict[str, Any]:
        assert self.database is not None and self.run_id is not None
        if self.read_state is not None:
            return self.read_state(self.database, self.run_id)
        interpreter = bound_interpreter(self.executable)
        return self.command(
            [
                interpreter,
                "-I",
                str(Path(__file__).with_name("factory_gate_project_probe.py").resolve()),
                str(self.database),
                self.run_id,
            ]
        )

    def await_worker(self, client: Any | None = None) -> dict[str, Any]:
        assert self.operation_id is not None
        name = container_name_for(self.operation_id)
        while True:
            self.remaining()
            state = self.state()
            records = [
                row
                for row in state["supervisors"]
                if row.get("operation_id") == self.operation_id and row.get("status") == "running"
            ]
            if records and self.engine.container_exists(name):
                observed = self.engine.container_inspect(name)
                labels = observed.get("Config", {}).get("Labels", {})
                if (
                    labels.get(OPERATION_LABEL) != self.operation_id
                    or labels.get(RUNNER_LABEL) != "1"
                ):
                    raise GateHoldError("running worker lacks its exact operation/runner labels")
                worker = records[0]
                if observed.get("State", {}).get("Running") and self.group_alive(
                    worker["process_group_id"]
                ):
                    self.worker = worker
                    assert self.since is not None and self.qualified is not None
                    if self.worker_observation is None:
                        creates = self._worker_create_events(self.since)
                        identity = self._count_worker_events(creates)[0]
                        if observed.get("Id") != identity:
                            raise GateHoldError(
                                "observed create differs from the running container id"
                            )
                        self.worker_observation = {
                            "container_id": identity,
                            "container": name,
                            "engine_since": self._create_boundary(creates[0]),
                            "create_event": creates[0],
                            "event_coverage_path": str(self._coverage_path()),
                        }
                        # Bind the journal before coordinator loss; recovery never re-anchors.
                        self.gate.campaign.annotate(
                            self.harness + ":" + self.qualified["role"],
                            worker_observation=self.worker_observation,
                        )
                        self._advance_worker_event_coverage(initial=True)
                    elif observed.get("Id") != self.count_workers()[0]:
                        raise GateHoldError("running worker differs from its durable observation")
                    return {
                        "kind": "worker.started",
                        "source": (
                            "durable worker-supervisor running record plus engine container Running"
                        ),
                        "worker": worker,
                        "container": name,
                        "provider_json_before_cancel": "not exposed by pinned worker",
                    }
            if client is not None and client.poll() is not None:
                raise GateHoldError(
                    "coordinator stopped before an observable live worker; no retry"
                )
            self.sleep(min(0.1, self.remaining()))

    @staticmethod
    def _create_boundary(event: Mapping[str, Any]) -> str:
        nanoseconds = event.get("timeNano")
        if isinstance(nanoseconds, int) and not isinstance(nanoseconds, bool) and nanoseconds > 0:
            seconds, fraction = divmod(nanoseconds, 1_000_000_000)
            return f"{seconds}.{fraction:09d}"
        seconds = event.get("time")
        if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
            return str(seconds)
        raise WorkerCountUnprovableError("worker create event has no engine timestamp")

    def _worker_create_events(self, since: str) -> list[dict[str, Any]]:
        assert self.operation_id is not None
        until = engine_time(self)
        end = None if until is None else later(until, 1)
        events = None if end is None else engine_creates(self, since, end)
        if events is None:
            raise WorkerCountUnprovableError(
                "engine create events unavailable; worker count is unknown"
            )
        self._save("engine-creates-" + self.operation_id.replace(":", "-") + ".jsonl", events)
        return self._matching_worker_creates(_events(events))

    def _matching_worker_creates(self, events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        assert self.operation_id is not None
        expected = container_name_for(self.operation_id)
        return [
            event
            for event in events
            if event.get("Type") == "container"
            and event.get("Action") == "create"
            and event.get("Actor", {}).get("Attributes", {}).get("name")
            not in {self.relay_name, expected + "-store"}
            and (
                event.get("Actor", {}).get("Attributes", {}).get("name") == expected
                or event.get("Actor", {}).get("Attributes", {}).get(OPERATION_LABEL)
                == self.operation_id
            )
        ]

    def _count_worker_events(self, events: list[dict[str, Any]]) -> list[str]:
        # Count creates, not unique names/ids: a repeated create is never deduplicated.
        if not self.recovering:
            self.gate.effects["isolated_containers"] += max(
                0, len(events) - self.counted_worker_creates
            )
            self.counted_worker_creates = max(self.counted_worker_creates, len(events))
        if len(events) != 1:
            raise GateHoldError(f"expected exactly one worker create, observed {len(events)}")
        identity = events[0].get("Actor", {}).get("ID") or events[0].get("id")
        if not identity:
            raise GateHoldError("engine create event omitted its actual container id")
        return [str(identity)]

    def _coverage_path(self) -> Path:
        assert self.operation_id is not None
        name = "engine-coverage-" + self.operation_id.replace(":", "-") + ".json"
        if self.worker_observation is None:
            return (self.directory / name).resolve()
        stored = self.worker_observation.get("event_coverage_path")
        if not isinstance(stored, str):
            raise WorkerCountUnprovableError("durable worker event coverage checkpoint is missing")
        path = Path(stored)
        if not path.is_absolute() or path.name != name:
            raise WorkerCountUnprovableError(
                "durable worker event coverage checkpoint path is invalid"
            )
        return path

    def _coverage_events(self, text: str) -> list[dict[str, Any]]:
        """Unlike harness output, an engine proof window must not silently skip malformed lines."""
        try:
            events = [json.loads(line) for line in text.splitlines() if line.strip()]
            if not events or any(not isinstance(event, dict) for event in events):
                raise ValueError("empty or non-object engine window")
            times = [self._create_boundary(event) for event in events]
            if times != sorted(times, key=lambda value: tuple(map(int, value.split(".")))):
                raise ValueError("out-of-order engine window")
        except (TypeError, ValueError) as error:
            raise WorkerCountUnprovableError("invalid retained engine event window") from error
        return events

    def _advance_worker_event_coverage(self, *, initial: bool = False) -> list[str]:
        """Replay digest-bound overlapping windows, then durably extend the last checkpoint."""
        assert self.worker_observation is not None and self.operation_id is not None
        path = self._coverage_path()
        checkpoint = self.worker_observation["create_event"]
        windows: list[dict[str, Any]] = []
        creates: list[dict[str, Any]] = []
        if initial:
            if path.exists():
                raise WorkerCountUnprovableError("worker event coverage already exists")
        else:
            try:
                coverage = json.loads(path.read_text(encoding="utf-8"))
                if (
                    coverage["operation_id"] != self.operation_id
                    or not isinstance(coverage["windows"], list)
                    or not coverage["windows"]
                ):
                    raise ValueError("invalid coverage identity/windows")
                for index, window in enumerate(coverage["windows"]):
                    expected = path.with_name(f"{path.stem}-{index:04d}.jsonl")
                    if window["path"] != str(expected) or window["since"] != self._create_boundary(
                        checkpoint
                    ):
                        raise ValueError("missing or discontinuous coverage checkpoint")
                    raw = expected.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != window["sha256"]:
                        raise ValueError("tampered retained engine event window digest")
                    events = self._coverage_events(raw.decode("utf-8"))
                    overlap = events.index(checkpoint)
                    creates.extend(self._matching_worker_creates(events[overlap + bool(windows) :]))
                    checkpoint = events[-1]
                    windows.append(window)
                if checkpoint != coverage["checkpoint"]:
                    raise ValueError("replaced coverage checkpoint")
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise WorkerCountUnprovableError(
                    "retained engine event coverage checkpoint is missing, stale or discontinuous"
                ) from error
            self._count_worker_events(creates)
        until = engine_time(self)
        if until is None:
            raise WorkerCountUnprovableError("engine event clock unavailable for rolling coverage")
        since = self._create_boundary(checkpoint)
        completed = self.engine_cli(
            "events",
            "--since",
            since,
            "--until",
            later(until, 1),
            "--format",
            "{{json .}}",
            timeout=90,
        )
        if completed.returncode != 0:
            raise WorkerCountUnprovableError("engine event window unavailable")
        events = self._coverage_events(completed.stdout)
        try:
            overlap = events.index(checkpoint)
        except ValueError as error:
            # An evicted anchor is safe only if every later window still overlaps its predecessor.
            raise WorkerCountUnprovableError(
                "engine event history no longer covers the rolling observation checkpoint"
            ) from error
        creates.extend(self._matching_worker_creates(events[overlap + bool(windows) :]))
        identities = self._count_worker_events(creates)
        if not windows or events[-1] != checkpoint:
            window_path = self._save(
                f"{path.stem}-{len(windows):04d}.jsonl",
                completed.stdout,
                directory=path.parent,
            )
            windows.append(
                {"path": str(window_path), "sha256": sha256(window_path), "since": since}
            )
            # _save fsyncs both window and index. The separate reconciliation lock
            # serializes recovery of this sidecar without holding the manifest lock.
            self._save(
                path.name,
                {
                    "operation_id": self.operation_id,
                    "checkpoint": events[-1],
                    "windows": windows,
                },
                directory=path.parent,
            )
        self.last_event_checkpoint = self.clock()
        return identities

    def count_workers(self) -> list[str]:
        assert self.operation_id is not None and self.since is not None
        observation = self.worker_observation
        if observation is None:
            if self.recovering:
                raise WorkerCountUnprovableError("slot has no durable worker create observation")
            return self._count_worker_events(self._worker_create_events(self.since))
        anchor = observation.get("create_event")
        if (
            not isinstance(anchor, Mapping)
            or not observation.get("container_id")
            or observation["container_id"]
            != (anchor.get("Actor", {}).get("ID") or anchor.get("id"))
            or observation.get("container") != container_name_for(self.operation_id)
            or observation.get("engine_since") != self._create_boundary(anchor)
        ):
            raise WorkerCountUnprovableError("durable worker create observation is invalid")
        return self._advance_worker_event_coverage()

    def retained(
        self, artifact_root: str, role: str, artifact_refs: Sequence[str] = ()
    ) -> dict[str, Any]:
        from factory_gate_protocol import retain_artifacts

        store = ArtifactStore(Path(artifact_root), create_directories=False)
        events: list[dict[str, Any]] = []
        for digest in store.iter_digests():
            data = store.read_bytes(digest)
            text = data.decode("utf-8", errors="replace")
            if self.gate.scrubber.contains(text):
                self._save(
                    "token-leak.json",
                    {
                        "status": "hold",
                        "artifact": str(digest),
                        "reason": "gateway token found in product artifact",
                    },
                )
                # Do not retain a leaking copy as a claimed byte-exact original.
                raise GateHoldError("gateway token found in product artifact; qualification held")
            if str(digest) in artifact_refs:
                events.extend(event for event in _events(text) if "type" in event)
        retained = retain_artifacts(Path(artifact_root), self.directory / role / "raw")
        usage = _usage(events)
        return {
            **retained,
            "events": {
                "count": len(events),
                "kinds": sorted({str(event.get("type")) for event in events}),
            },
            "usage": usage,
            "usage_reported_by_harness": usage is not None,
            "usage_source": "this run's harness artifact_references",
            "raw_original_semantics": (
                "product-retained secret-redacted merged stdout+stderr; "
                "provider JSON available after exit only"
            ),
        }

    def _gone(self) -> tuple[bool, bool]:
        assert self.operation_id is not None
        group_gone = self.worker is not None and not self.group_alive(
            self.worker["process_group_id"]
        )
        container_gone = not self.engine.container_exists(container_name_for(self.operation_id))
        return group_gone, container_gone

    def close(self) -> None:
        for client in self.clients:
            if client.poll() is None:
                try:
                    client.wait(timeout=10)
                except subprocess.TimeoutExpired as error:
                    client.kill()
                    client.wait(timeout=10)
                    raise GateHoldError(
                        "coordinator remained after the terminal workflow"
                    ) from error
            else:
                client.wait(timeout=10)
        leaked = False
        files, self.files = self.files, []
        for stream in files:
            stream.flush()
            stream.seek(0)
            text = stream.read()
            stream.close()
            hits = self.gate.scrubber.contains(text)
            leaked = leaked or bool(hits)
            # Scrub all client logs even when more than one contains a leak.
            path = Path(stream.name)
            path.write_text(self.gate.scrubber.text(text), encoding="utf-8")
        if leaked:
            raise GateHoldError("secret in coordinator output; logs scrubbed; qualification held")

    def cleanup_marker(self) -> Path:
        assert self.operation_id is not None
        name = hashlib.sha256(self.operation_id.encode()).hexdigest() + ".json"
        return self.gate.campaign.path.parent / "cleanup" / name

    def abort_worker(self) -> None:
        if self.operation_id is None or self.database is None or self.run_id is None:
            return
        # Only cancellation may mutate after an exception; never advance a possibly queued run.
        deadline = self.deadline
        self.deadline = None
        try:
            for client in self.clients:
                if client.poll() is None:
                    client.kill()
                    client.wait(timeout=10)
            if not self.database.exists():
                return
            state = self.state()
            node = next((row for row in state["nodes"] if row["node_id"] == "implement"), None)
            try:
                if node is not None and node.get("status") in {
                    "running",
                    "queued",
                    "awaiting_human",
                }:
                    marker = self.cleanup_marker()
                    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    self._write_qualification(
                        marker, {"operation_id": self.operation_id, "cleanup_abort": True}
                    )
                    self.engine_action("cancel", "--node-id", "implement")
            finally:
                name = container_name_for(self.operation_id)
                try:
                    if self.engine.container_exists(name):
                        self.engine.kill(name)
                        self.engine.remove(name, force=True)
                finally:
                    records = state.get("supervisors", [])
                    if records:
                        from enginery.engine.supervisor import (
                            _terminate_exact_group,
                            identity_from_state,
                        )

                        reason = _terminate_exact_group(identity_from_state(records[-1]))
                        if reason is not None:
                            raise GateHoldError(
                                "worker process group cleanup is unproved: " + reason
                            )
            if self.engine.container_exists(name):
                raise GateHoldError("worker container survived cancellation cleanup")
        finally:
            self.deadline = deadline

    def _verify_issue_snapshot(self) -> dict[str, Any]:
        issue = self.binding["issues"][self.harness]
        endpoint = f"repos/{self.binding['repository']}/issues/{issue['number']}"
        # Compare frozen task inputs, not volatile activity/transport metadata.
        original = self.command(["gh", "api", "--method", "GET", endpoint], json_output=False)
        if issue_binding(original.encode()) != issue:
            raise GateHoldError("bound issue snapshot changed before its first worker launch")
        snapshot = json.loads(original)
        if not isinstance(snapshot, dict):
            raise GateHoldError("bound issue snapshot is not an object")
        return snapshot

    def qualify_completed(self, intent_path: Path) -> dict[str, Any]:
        snapshot = self._verify_issue_snapshot()
        issue = self.binding["issues"][self.harness]
        self.project_cli("collect")
        existing = self.project_cli("status")
        reference = f"{self.binding['repository']}#{issue['number']}"
        if any(row.get("issue") == reference for row in existing.get("runs", [])):
            raise GateHoldError(
                "completed workflow issue already has a project run; never reuse/relaunch it"
            )
        intent = self._begin_qualification(
            intent_path,
            "completed-workflow",
            external_reference=reference,
            base_revision=self.command(["git", "rev-parse", "HEAD"], json_output=False).strip(),
            base_branch=self.command(
                ["git", "symbolic-ref", "--short", "HEAD"], json_output=False
            ).strip(),
            existing_run_ids=[row["run_id"] for row in existing.get("runs", [])],
            issue_snapshot=snapshot,
        )
        # run --once qualifies only; the first continue is the worker-launch boundary.
        arguments = [
            reference,
            "--once",
            "--runner",
            "isolated-oci",
            "--time-budget-seconds",
            str(self.worker_budget),
        ]
        for name, flag in (("model_pins", "--model"), ("effort_pins", "--effort")):
            value = self.binding.get(name, {}).get(self.harness)
            if value is not None:
                arguments.extend([flag, value])
        report = self.project_cli("run", *arguments, allowed=(0, 5))
        if (
            not report.get("run_id")
            or report.get("holds")
            or report.get("next_action") != "implement"
        ):
            raise GateHoldError(
                "product qualification requires owner action before launch; see raw report"
            )
        path = Path(report["request_path"])
        request = json.loads(path.read_text(encoding="utf-8"))
        self._match_intent_request(intent, request, report["run_id"])
        return self._qualification("completed-workflow", path, Path(report["database"]))

    def completed_workflow(self) -> dict[str, Any]:
        try:
            return self._completed_workflow()
        except BaseException:
            self.abort_worker()
            raise
        finally:
            self.close()

    def _completed_workflow(self) -> dict[str, Any]:
        request = self._launch_qualified("completed-workflow")
        key = self.harness + ":completed-workflow"
        client = self.spawn_continue()
        first_event = self.await_worker(client)
        # Kill this coordinator/client only. The product-supervised launcher is a separate group.
        client.kill()
        client.wait(timeout=10)
        survived = self.group_alive(self.worker["process_group_id"])
        if survived:
            try:
                state = self.engine.container_inspect(first_event["container"]).get("State", {})
            except ObjectNotFoundError:  # the worker already exited and was removed
                state = {}
            survived = bool(state.get("Running"))
        if not survived:
            raise GateHoldError("worker did not survive the single coordinator kill")
        self._save(
            "coordinator-kill.json",
            {
                "client_pid": client.pid,
                "worker_survived": True,
                "first_event": first_event,
                "kill_count": 1,
            },
        )
        self.gate.campaign.annotate(key, coordinator_kills=1, first_event=first_event)
        return self.finish_completed(request, first_event)

    def finish_completed(
        self, request: dict[str, Any], first_event: dict[str, Any]
    ) -> dict[str, Any]:
        """Continue an already dispatched worker; never bind or dispatch another request."""
        node = next((row for row in self.state()["nodes"] if row["node_id"] == "implement"), {})
        if node.get("status") not in {"running", "passed"}:
            raise GateHoldError("recovery cannot launch an absent, queued or failed implementation")
        # Close any interruption gap before a continue client can emit fresh OCI probe events.
        self.count_workers()
        resumed = self.spawn_continue()
        last_state: dict[str, Any] | None = None
        waiting_for_decision = False
        while True:
            self.remaining()
            if self.clock() - self.last_event_checkpoint >= EVENT_CHECKPOINT_SECONDS:
                self.count_workers()
            state = self.state()
            if state == last_state and (waiting_for_decision or resumed.poll() is None):
                self.sleep(min(OWNER_POLL_SECONDS, self.remaining()))
                continue
            last_state = state
            report = self.project_cli("status", self.run_id, allowed=(0, 5))
            self._save("current-status.json", report)
            waiting_for_decision = bool(report.get("next_decision"))
            if report.get("workflow_complete") and report.get("terminal_decision") == "merge_ready":
                break
            if resumed.poll() is not None:
                # Human decisions are made externally. Continue only this same bound run; a
                # repair is impossible because its immutable repair_limit was checked as zero.
                if any(
                    row.get("status") in {"failed", "cancelled", "blocked"}
                    and row.get("node_id") == "implement"
                    for row in report.get("nodes", [])
                ):
                    raise GateHoldError("live implementation failed; no replacement worker allowed")
                self._save(
                    "owner-action-required.json",
                    {
                        "run_id": self.run_id,
                        "holds": report.get("holds", []),
                        "next_action": report.get("next_action"),
                        "decision_policy": (
                            "owner uses real terminal review/decide; gate never decides"
                        ),
                    },
                )
                self.sleep(min(1.0, self.remaining()))
                if not waiting_for_decision:
                    resumed = self.spawn_continue()
            else:
                self.sleep(min(0.2, self.remaining()))
        state = self.state()
        receipts = state["execution_receipts"]
        if len(receipts) != 1 or len(state["supervisors"]) != 1:
            raise GateHoldError("completed workflow lacks one actual product execution receipt")
        implementation = [row for row in state["nodes"] if row["node_id"] == "implement"]
        attempts = [row for row in state["attempts"] if row.get("node_id") == "implement"]
        if (
            len(implementation) != 1
            or implementation[0].get("status") != "passed"
            or len(attempts) != 1
            or attempts[0].get("terminal_result") != "passed"
            or attempts[0].get("result", {}).get("harness_status") != "succeeded"
        ):
            raise GateHoldError("product ledger has no single successful terminal harness result")
        image_observation = self.check_receipt(request, receipts[0])
        launches = self.count_workers()
        group_gone, container_gone = self._gone()
        if not group_gone or not container_gone:
            raise GateHoldError("terminal workflow left a worker group or container")
        pr_number = report.get("pull_request", {}).get("number")
        verified = report.get("pull_request", {}).get("head_revision")
        pr = self.pr_snapshot(pr_number)
        if (
            pr.get("state") != "open"
            or pr.get("merged")
            or pr.get("head", {}).get("sha") != verified
            or not pr["changed_paths"]
            or report.get("terminal_evidence", {}).get("verify_status") != "passed"
        ):
            raise GateHoldError("fresh host PR snapshot is not the verified open changed head")
        if any(check.get("head_sha") != verified for check in pr["check_runs"]):
            raise GateHoldError("PR check-run snapshot contains a different head")
        artifact_refs = [
            digest
            for attempt in state["attempts"]
            for digest in attempt.get("artifact_references", [])
        ]
        retained = self.retained(
            request["execution_configuration"]["artifact_root"], "completed-workflow", artifact_refs
        )
        retained["image_harness_observation"] = image_observation
        if not retained["raw_originals"]:
            raise GateHoldError("completed workflow has no retained product originals")
        if not retained["events"]["count"]:
            raise GateHoldError("completed workflow retained no harness protocol events")
        decisions = [
            row
            for row in state.get("decisions", [])
            if valid_human_decision(row, str(self.run_id), verified)
        ]
        if not decisions:
            raise GateHoldError("verified head lacks an actual human terminal action decision")
        decision_reference = {
            "run_id": self.run_id,
            "head_revision": verified,
            "decisions": decisions,
        }
        assertions = {
            "verified-open-pr-outcome": {
                "status": "pass",
                "observed": {"report": report, "pr": pr},
            },
            "client-resume-same-identity": {
                "status": "pass",
                "observed": {
                    "run_id": self.run_id,
                    "operation_id": self.operation_id,
                    "worker_creates": len(launches),
                    "kill_count": 1,
                },
            },
            "client-kill-worker-survives": {"status": "pass", "observed": first_event},
        }
        return {
            "project_path": str(self.project),
            "database": str(self.database),
            "artifact_root": request["execution_configuration"]["artifact_root"],
            "workspace_path": request["workspace_path"],
            "base_revision": request["run"]["data"]["base_revision"],
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "pr_number": pr_number,
            "pr_url": pr.get("html_url"),
            "verified_head": verified,
            "pr": pr,
            "state": state,
            "assertions": assertions,
            "worker_launches": launches,
            "selected_harness": self.harness,
            "selected_controls": request.get("execution_selection"),
            "effective_config": self.effective_config,
            "execution_receipts": receipts,
            "runner_profile": self.runner_profile,
            "decision_receipt_reference": decision_reference,
            "lifecycle": {
                "start": first_event,
                "result": "succeeded",
                "reconcile": "same run continued",
                "coordinator_kills": 1,
            },
            "engine": self.identity,
            "mode": "live-installed-isolated",
            "executable": self.executable,
            "executable_argv": [self.executable],
            "group_gone": group_gone,
            "container_gone": container_gone,
            **retained,
        }

    def pr_snapshot(self, number: int) -> dict[str, Any]:
        pr = pr_snapshot(self.binding["repository"], number, self.command)
        self._save("fresh-pr-snapshot.json", pr)
        return pr

    def qualify_cancellation(self, operation_id: str, intent_path: Path) -> dict[str, Any]:
        self.owner = "factory-gate-cancel"
        self.run_id = operation_id
        self.operation_id = operation_id
        root = self.gate.campaign.path.parent / "cancel" / self.harness
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = root / "ledger.sqlite"
        path = root / "request.json"
        if path.exists() or self.database.exists():
            raise GateHoldError(
                "cancellation request already exists; inspect/reconcile it, never relaunch"
            )
        issue = self.binding["issues"][self.harness]["number"]
        reference = f"{self.binding['repository']}#{issue}"
        base = self.command(["git", "rev-parse", "HEAD"], json_output=False).strip()
        branch = "enginery/" + self.run_id
        remote_before = self.remote_effects(branch)
        if remote_before["refs"] or remote_before["pull_requests"]:
            raise GateHoldError(
                "cancellation branch already has remote effects; no worker launched"
            )
        base_branch = self.command(
            ["git", "symbolic-ref", "--short", "HEAD"], json_output=False
        ).strip()
        intent = self._begin_qualification(
            intent_path,
            "cancellation",
            run_id=self.run_id,
            operation_id=operation_id,
            external_reference=reference,
            base_revision=base,
            base_branch=base_branch,
            request_path=str(path.resolve()),
            database=str(self.database.resolve()),
            head_branch=branch,
            remote_before=remote_before,
        )
        argv = [
            "engine",
            "build-request",
            "--output",
            str(path),
            "--run-id",
            self.run_id,
            "--repository",
            self.binding["repository"],
            "--external-reference",
            reference,
            "--repository-path",
            str(self.project),
            "--workspace-path",
            str(root / "workspace"),
            "--artifact-root",
            str(root / "artifacts"),
            "--base-revision",
            base,
            "--base-branch",
            base_branch,
            "--head-branch",
            branch,
            "--implementation-operation-id",
            operation_id,
            "--implementation-time-budget-seconds",
            str(self.worker_budget),
            "--harness-provider",
            self.provider,
            "--runner",
            "isolated-oci",
            "--runner-image",
            self.options.runner_image,
            "--docker-context",
            self.options.docker_context,
            "--model-access",
            "relay",
            "--repair-limit",
            "0",
        ]
        for command in self.validation_commands:
            argv.extend(["--validation-command", command])
        for name, flag in (("model_pins", "--model"), ("effort_pins", "--effort")):
            value = self.binding.get(name, {}).get(self.harness)
            if value is not None:
                argv.extend([flag, value])
        self.cli(*argv)
        request = json.loads(path.read_text(encoding="utf-8"))
        self._check_request(request)
        self._match_intent_request(intent, request, str(self.run_id))
        self.cli(
            "engine",
            "start",
            "--database",
            str(self.database),
            "--owner",
            self.owner,
            "--request",
            str(path),
        )
        qualified = self.engine_action("next", "--advance")
        if qualified.get("next_action") != "implement":
            raise GateHoldError("cancellation issue did not qualify; owner action is required")
        return self._qualification(
            "cancellation", path, self.database, remote_before=remote_before, head_branch=branch
        )

    def cancellation(self, operation_id: str) -> dict[str, Any]:
        try:
            return self._cancellation(operation_id)
        except BaseException:
            self.abort_worker()
            raise
        finally:
            self.close()

    def _cancellation(self, operation_id: str) -> dict[str, Any]:
        if self.operation_id != operation_id:
            raise GateHoldError("reservation does not match the preflight cancellation operation")
        request = self._launch_qualified("cancellation")
        assert self.qualified is not None
        remote_before = self.qualified["remote_before"]
        branch = self.qualified["head_branch"]
        self.gate.campaign.annotate(
            self.harness + ":cancellation", remote_before=remote_before, head_branch=branch
        )
        # Exactly one advance dispatches implementation. No validation/publication advance follows.
        self.engine_action("next", "--advance")
        first_event = self.await_worker()
        self.gate.campaign.annotate(
            self.harness + ":cancellation", first_event=first_event, cancel_issued=True
        )
        cancel = self.engine_action("cancel", "--node-id", "implement")
        self.gate.campaign.annotate(
            self.harness + ":cancellation", cancel_status=cancel.get("status")
        )
        reconcile = self.engine_action("next")  # read-only, cannot dispatch another worker
        evidence = self.engine_action("evidence")
        state = self.state()
        node = next((row for row in state["nodes"] if row["node_id"] == "implement"), {})
        terminal = node.get("status")
        group_gone, container_gone = self._gone()
        remote_after = self.remote_effects(branch)
        if (
            cancel.get("status") != "cancelled"
            or terminal != "cancelled"
            or not group_gone
            or not container_gone
            or remote_before != remote_after
            or remote_after["refs"]
            or remote_after["pull_requests"]
        ):
            raise GateHoldError("canonical cancellation did not reconcile cleanly without push/PR")
        if any(
            row["node_id"] in {"publish_branch", "open_pr", "wait_for_ci", "verify"}
            for row in state["nodes"]
        ):
            raise GateHoldError("cancellation unexpectedly reached a publication/verification node")
        launches = self.count_workers()
        retained = self.retained(
            request["execution_configuration"]["artifact_root"], "cancellation"
        )
        if not retained["raw_originals"]:
            retained["raw_absence"] = {
                "source": (
                    "installed engine evidence and node_attempt projections "
                    "after CoordinatorRuntime.cancel_node"
                ),
                "terminal_status": terminal,
                "artifact_references": [
                    digest
                    for attempt in state["attempts"]
                    for digest in attempt.get("artifact_references", [])
                ],
                "product_evidence": evidence,
                "reason": (
                    "product cancellation ingests terminal cancellation with no harness artifact "
                    "and cleans the supervised workspace; no original bytes were published"
                ),
            }
        return {
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "database": str(self.database),
            "artifact_root": request["execution_configuration"]["artifact_root"],
            "terminal_status": terminal,
            "cancel_status": cancel.get("status"),
            "reconcile_status": terminal,
            "cancel": cancel,
            "reconcile": {"status": terminal, "product": reconcile},
            "event_count": 1 + retained["events"]["count"],
            "first_event": first_event,
            "cancel_issued": True,
            "lifecycle": {
                "start": "supervised_command via engine next --advance",
                "events": "durable worker.started + operation-labelled running container",
                "cancel": "CoordinatorRuntime.cancel_node via engine cancel",
                "result": terminal,
                "reconcile": "read-only engine next/evidence plus persisted cancelled node",
            },
            "group_gone": group_gone,
            "container_gone": container_gone,
            "pushes": 0,
            "pull_requests": 0,
            "remote_before": remote_before,
            "remote_after": remote_after,
            "worker_launches": launches,
            "state": state,
            "artifacts": evidence,
            "mode": "live-installed-isolated",
            **retained,
        }

    def remote_effects(self, branch: str) -> dict[str, Any]:
        refs = self.command(
            ["git", "ls-remote", "--heads", "origin", "refs/heads/" + branch], json_output=False
        ).strip()
        pulls = self.command(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{self.binding['repository']}/pulls",
                "-f",
                "state=all",
                "-f",
                "head=" + self.binding["repository"].split("/")[0] + ":" + branch,
            ]
        )
        return {"refs": refs, "pull_requests": pulls}


def preflight(gate: Any, role: str) -> LiveProtocol:
    """Return the qualified instance that the caller uses immediately after reservation."""
    live = LiveProtocol(gate)
    try:
        live.preflight(role)
    except BaseException:
        live.close()
        raise
    return live


def reconcile_interrupted(gate: Any, key: str, slot: Mapping[str, Any]) -> dict[str, Any]:
    """Recover an existing dispatched operation; this callback never touches the manifest lock."""
    live = LiveProtocol(gate, harness=key.split(":")[0])
    live.owner = str(slot.get("owner", "factory"))
    live.operation_id = str(slot["operation_id"])
    live.run_id = slot.get("run_id")
    live.database = Path(slot["database"]) if slot.get("database") else None
    live.recovering = True
    live.worker_observation = slot.get("worker_observation")
    containers = live.labelled_containers()
    live._save(
        "interrupted-container-inspection.json",
        {"key": key, "operation_id": live.operation_id, "containers": containers},
    )
    if live.run_id is None or live.database is None:
        if containers:
            raise GateHoldError(
                "interrupted worker has no durable product identity; owner reconciliation required"
            )
        return {
            "terminal_status": "failed",
            "container_gone": True,
            "operation_id": live.operation_id,
            "observation": (
                "reserved slot never bound a product worker; no operation-labelled container"
            ),
            "worker_launches": [],
        }
    cancel_status = slot.get("cancel_status")
    try:
        live.prepare()
        request = json.loads(Path(slot["request_path"]).read_text(encoding="utf-8"))
        live._check_request(request)
        live.since = slot.get("engine_since")
        state = live.state()
        records = state["supervisors"]
        live.worker = records[-1] if records else None
        worker_alive = live.worker is not None and live.group_alive(live.worker["process_group_id"])
        # The original worker's deadline is never restarted by a fresh invocation.
        remaining_lifetime = float(slot.get("deadline_at", 0)) - time.time()
        expired_worker = worker_alive and remaining_lifetime <= 0
        if expired_worker:
            live.abort_worker()
            worker_alive = False
        live.deadline = live.clock() + (remaining_lifetime if worker_alive else live.budget)
        if (
            not expired_worker
            and key.endswith(":completed-workflow")
            and slot.get("coordinator_kills") == 1
        ):
            node = next((row for row in state["nodes"] if row["node_id"] == "implement"), {})
            if node.get("status") in {"running", "passed"} and (
                not worker_alive or remaining_lifetime > 0
            ):
                result = live.finish_completed(request, dict(slot["first_event"]))
                return {
                    "terminal_status": "completed",
                    "container_gone": result["container_gone"],
                    "group_gone": result["group_gone"],
                    "workflow": result,
                    "no_launch": True,
                }
        # Collect an already exited handoff through product collect_supervised, but never
        # advance qualify/implement/queued work or publication as part of cancellation recovery.
        if not worker_alive:
            action = live.engine_action("next")
            if action.get("next_action") in {"collect_implementation", "reconcile_worker"}:
                live.engine_action("next", "--advance")
        observed_first_event = (
            isinstance(slot.get("first_event"), Mapping)
            and slot["first_event"].get("kind") == "worker.started"
        )
        cancellation_issued = bool(
            not expired_worker
            and key.endswith(":cancellation")
            and observed_first_event
            and slot.get("cancel_issued") is True
            and not live.cleanup_marker().exists()
        )
        if cancellation_issued:
            node = next((row for row in live.state()["nodes"] if row["node_id"] == "implement"), {})
            if node.get("status") == "cancelled":
                # The original cancel may have committed before its CLI response was retained.
                cancel_status = slot.get("cancel_status", node["status"])
            else:
                cancel_status = live.engine_action("cancel", "--node-id", "implement").get("status")
        else:
            # This cancellation is cleanup only. It must never qualify the canonical journey.
            live.abort_worker()
        report = live.engine_action("next")
        artifacts = live.engine_action("evidence")
        state = live.state()
        group_gone = live.worker is None or not live.group_alive(live.worker["process_group_id"])
        if live.labelled_containers() or not group_gone:
            raise GateHoldError(
                "interrupted worker group/container still present after product cancellation"
            )
        refs = [
            digest
            for attempt in state["attempts"]
            for digest in attempt.get("artifact_references", [])
        ]
        retained = live.retained(str(slot["artifact_root"]), "interrupted", refs)
        terminal = next(
            (row.get("status") for row in state["nodes"] if row["node_id"] == "implement"), None
        )
        result = {
            "terminal_status": terminal,
            "container_gone": True,
            "group_gone": True,
            "run_id": live.run_id,
            "operation_id": live.operation_id,
            "artifact_root": str(slot["artifact_root"]),
            "database": str(live.database),
            "cancel_status": cancel_status,
            "reconcile_status": terminal,
            "first_event": slot.get("first_event"),
            "cancel_issued": slot.get("cancel_issued") is True,
            "cleanup_abort": live.cleanup_marker().exists(),
            "event_count": int(observed_first_event) + retained["events"]["count"],
            "reconcile": {"status": terminal, "product": report},
            "artifacts": artifacts,
            "state": state,
            "no_launch": True,
            **retained,
        }
        if (
            cancellation_issued
            and cancel_status == "cancelled"
            and terminal == "cancelled"
            and slot.get("remote_before") is not None
        ):
            remote = live.remote_effects(request["head_branch"])
            if remote != slot["remote_before"] or remote["refs"] or remote["pull_requests"]:
                raise GateHoldError("recovered cancellation has remote branch/PR effects")
            if any(
                row["node_id"] in {"publish_branch", "open_pr", "wait_for_ci", "verify"}
                for row in state["nodes"]
            ):
                raise GateHoldError("recovered cancellation reached publication")
            result.update(
                worker_launches=live.count_workers(),
                pushes=0,
                pull_requests=0,
                remote_before=slot["remote_before"],
                remote_after=remote,
            )
            if not retained["raw_originals"]:
                result["raw_absence"] = {
                    "source": "installed engine evidence after recovered cancellation",
                    "terminal_status": terminal,
                    "artifact_references": refs,
                    "product_evidence": artifacts,
                    "reason": "canonical cancellation published no harness output artifact",
                }
            return {**result, "workflow": dict(result)}
        return {**result, "terminal_status": "failed"}
    except WorkerCountUnprovableError as error:
        live.abort_worker()
        state = live.state()
        group_gone = live.worker is None or not live.group_alive(live.worker["process_group_id"])
        if live.labelled_containers() or not group_gone:
            raise GateHoldError(
                "unprovable worker count still has a worker group/container"
            ) from error
        result = {
            "terminal_status": "failed",
            "product_terminal_status": next(
                (row.get("status") for row in state["nodes"] if row["node_id"] == "implement"),
                None,
            ),
            "container_gone": True,
            "group_gone": True,
            "run_id": live.run_id,
            "operation_id": live.operation_id,
            "cancel_status": cancel_status,
            "first_event": slot.get("first_event"),
            "cancel_issued": slot.get("cancel_issued") is True,
            "cleanup_abort": live.cleanup_marker().exists(),
            "worker_observation": live.worker_observation,
            "worker_launches": None,
            "worker_count_status": "unprovable",
            "worker_count_reason": str(error),
            "no_launch": True,
            "state": state,
        }
        live._save("interrupted-worker-count-unprovable.json", result)
        return result
    except BaseException:
        live.abort_worker()
        raise
    finally:
        live.close()
