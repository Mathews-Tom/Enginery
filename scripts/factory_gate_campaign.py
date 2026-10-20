"""Locked, fail-closed four-slot qualification campaigns and byte-exact recovery."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from enginery.application.execution_catalog import load_execution_catalog
from factory_gate_context import GateHoldError, GateOptions, host_identity
from factory_gate_host_reads import bound_interpreter

CAMPAIGN_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,62}\Z")
SLOTS = frozenset(
    f"{h}:{r}" for h in ("omp", "claude") for r in ("completed-workflow", "cancellation")
)
TERMINAL = frozenset({"completed", "cancelled", "failed", "reconciled"})
TRANSITIONS = {
    "reserved": {"running", "failed", "interrupted"},
    "running": {"completed", "cancelled", "failed", "interrupted"},
    "completed": set(),
    "cancelled": set(),
    "failed": set(),
    "interrupted": {"reconciled"},
    "reconciled": set(),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_host_identity() -> dict[str, Any]:
    """Bind the machine, not a boot, OS release, or evaluator Python version."""
    observed = host_identity()
    if not observed.get("machine_id"):
        raise GateHoldError("stable host machine identity is unavailable")
    return {key: observed[key] for key in ("machine_id", "arch")}


def issue_binding(snapshot: bytes) -> dict[str, Any]:
    """Freeze task inputs, excluding volatile GitHub transport and activity metadata."""
    issue = json.loads(snapshot)
    selected = {key: issue[key] for key in ("number", "title", "body", "state")}
    selected["labels"] = sorted(label["name"] for label in issue["labels"])
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        **selected,
        "snapshot_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def configuration(options: GateOptions) -> dict[str, Any]:
    if options.campaign_config is None:
        if options.live:
            raise GateHoldError("live campaigns require --campaign-config")
        return {"qualification_target": "synthetic"}
    try:
        result = json.loads(options.campaign_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateHoldError(f"campaign configuration is unreadable: {error}") from error
    if not isinstance(result, dict):
        raise GateHoldError("campaign configuration must be an object")
    if result.get("qualification_target") not in ("synthetic", "real"):
        raise GateHoldError("campaign qualification_target must be synthetic or real")
    return result


def campaign_binding(options: GateOptions) -> dict[str, Any]:
    config = configuration(options)
    catalog = load_execution_catalog()
    if not options.live:
        pins = {"omp": "fixture/model-1", "claude": "claude-fixture-1"}
        if options.model is not None and pins.get(options.harness) != options.model:
            raise GateHoldError("--model differs from the deterministic campaign model pin")
        return {
            "subject_commit": options.subject_commit,
            "qualification_target": "synthetic",
            "wheel_sha256": "stand-in",
            "installed_distribution": {"kind": "deterministic-stand-in"},
            "catalog_digest": str(catalog.digest),
            "runner_image": None,
            "relay_image": None,
            "docker_context": None,
            "engine_kind": "stand-in",
            "custody_receipt_sha256": None,
            "host": "stand-in",
            "gateway_url": None,
            "repository": "owner/repo",
            "issues": {
                harness: {
                    "number": 1,
                    "snapshot_sha256": hashlib.sha256(
                        f"stand-in issue {harness}".encode()
                    ).hexdigest(),
                }
                for harness in ("omp", "claude")
            },
            "model_pins": pins,
            "effort_pins": {"omp": "medium", "claude": "medium"},
        }
    from factory_gate_auth import load_live_custody

    assert options.gateway_url is not None and options.gateway_token_file is not None
    custody = load_live_custody(
        options.custody_receipt,
        gateway_url=options.gateway_url,
        token_file=options.gateway_token_file,
    )
    required = (
        "wheel",
        "executable",
        "project_path",
        "repository",
        "issues",
        "designated_workflow",
    )
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise GateHoldError("campaign configuration missing: " + ", ".join(missing))
    executable = Path(config["executable"]).absolute()
    wheel = Path(config["wheel"])
    try:
        interpreter = bound_interpreter(executable)
        probe = subprocess.run(
            [
                interpreter,
                "-I",
                str(Path(__file__).with_name("factory_gate_identity_probe.py")),
                str(wheel.resolve()),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        identity = json.loads(probe.stdout)
        wheel_digest = sha256(wheel)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise GateHoldError(f"installed wheel identity unavailable: {error}") from error
    if (
        identity["prefix"] == identity["base_prefix"]
        or executable.parent != Path(identity["prefix"]) / "bin"
    ):
        raise GateHoldError("the enginery executable is not in its own virtual environment")
    repository = config["repository"]
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise GateHoldError("campaign repository must be an explicit owner/repository allowlist")
    issues = {}
    previous = Campaign(options).read()
    for harness in ("omp", "claude"):
        number = config["issues"].get(harness)
        if not isinstance(number, int) or number < 1:
            raise GateHoldError(f"campaign issues must designate a positive number for {harness}")
        spent = f"{harness}:completed-workflow" in previous["slots"]
        old_binding = previous.get("binding") or {}
        if spent:
            original = old_binding.get("issues", {}).get(harness)
            if original is None:
                raise GateHoldError("spent campaign slot has no frozen issue snapshot")
            issues[harness] = {**original, "number": number}
        else:
            snapshot = subprocess.run(
                ["gh", "api", f"repos/{repository}/issues/{number}"],
                check=True,
                capture_output=True,
                timeout=30,
            ).stdout
            issues[harness] = issue_binding(snapshot)
            if issues[harness]["number"] != number:
                raise GateHoldError("issue snapshot differs from the designated issue number")
    if config["designated_workflow"] not in ("omp", "claude"):
        raise GateHoldError("designated_workflow must name omp or claude")
    pins = config.get("model_pins", {"omp": None, "claude": None})
    if options.model is not None and pins.get(options.harness) != options.model:
        raise GateHoldError("--model differs from the campaign's immutable model pin")
    project_paths = config.get(
        "project_paths", dict.fromkeys(("omp", "claude"), config["project_path"])
    )
    if set(project_paths) != {"omp", "claude"}:
        raise GateHoldError("project_paths must designate both reference harnesses")
    validations = config.get("validation_commands")
    try:
        valid_commands = (
            isinstance(validations, list)
            and bool(validations)
            and all(isinstance(command, str) and shlex.split(command) for command in validations)
        )
    except ValueError as error:
        raise GateHoldError(f"campaign validation command is malformed: {error}") from error
    if not valid_commands:
        raise GateHoldError("campaign validation_commands must be nonempty command strings")
    engine_kind = config.get("engine_kind")
    if not engine_kind:
        raise GateHoldError("campaign requires the engine_kind observed in runner admission")
    return {
        "subject_commit": options.subject_commit,
        "qualification_target": config["qualification_target"],
        "wheel_sha256": wheel_digest,
        "installed_distribution": identity,
        "catalog_digest": str(catalog.digest),
        "runner_image": options.runner_image,
        "relay_image": options.relay_image,
        "docker_context": options.docker_context,
        "engine_kind": engine_kind,
        "custody_receipt_sha256": sha256(options.custody_receipt),
        "host": {**stable_host_identity(), "custody_host": custody.host.value},
        "gateway_url": options.gateway_url,
        "repository": repository,
        "issues": issues,
        "model_pins": pins,
        "effort_pins": config.get("effort_pins", {"omp": None, "claude": None}),
        "project_path": str(Path(config["project_path"]).resolve()),
        "project_paths": {h: str(Path(p).resolve()) for h, p in project_paths.items()},
        "validation_commands": validations,
        "executable": str(executable),
        "designated_workflow": config["designated_workflow"],
    }


class Campaign:
    def __init__(self, options: GateOptions) -> None:
        if options.campaign_id is None or not CAMPAIGN_ID.fullmatch(options.campaign_id):
            raise GateHoldError("campaign id must match [a-z0-9][a-z0-9-]{2,62}")
        self.campaign_id = options.campaign_id
        root = (options.evidence_out.parent / "campaigns" / self.campaign_id).resolve()
        self.path = root / "manifest.json"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    value = (
                        json.loads(self.path.read_text())
                        if self.path.exists()
                        else {
                            "schema_version": 2,
                            "campaign_id": self.campaign_id,
                            "binding": None,
                            "slots": {},
                            "evidence": {},
                            "protocol_evidence": {},
                            "reuse": [],
                            "designated_workflow": None,
                        }
                    )
                except (OSError, ValueError) as error:
                    raise GateHoldError(f"campaign manifest is unreadable: {error}") from error
                if (
                    not isinstance(value, dict)
                    or value.get("schema_version") != 2
                    or value.get("campaign_id") != self.campaign_id
                    or not isinstance(value.get("slots"), dict)
                    or not isinstance(value.get("evidence"), dict)
                    or not isinstance(value.get("protocol_evidence"), dict)
                    or not isinstance(value.get("reuse"), list)
                ):
                    raise GateHoldError("unsupported or mismatching campaign manifest")
                yield value
                descriptor, name = tempfile.mkstemp(
                    prefix="manifest-", suffix=".tmp", dir=self.path.parent
                )
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        json.dump(value, stream, sort_keys=True, indent=2)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, self.path)
                    directory = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(name)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        with self._locked() as document:
            return json.loads(json.dumps(document))

    def bind(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        with self._locked() as document:
            if document["binding"] is None:
                document["binding"] = dict(binding)
                document["designated_workflow"] = (
                    binding.get("designated_workflow", "omp") + ":completed-workflow"
                )
            else:
                keys = set(binding) | set(document["binding"])
                changed = sorted(
                    key
                    for key in keys
                    if key not in binding
                    or key not in document["binding"]
                    or binding[key] != document["binding"][key]
                )
                if changed:
                    raise GateHoldError("campaign binding mismatch: " + ", ".join(changed))
            return json.loads(json.dumps(document))

    def reserve(self, harness: str, role: str) -> str:
        key = f"{harness}:{role}"
        with self._locked() as document:
            if document["binding"] is None:
                raise GateHoldError("campaign must be bound before reservation")
            if key not in SLOTS or len(document["slots"]) >= 4:
                raise GateHoldError("campaign four-session budget exhausted or invalid slot")
            if key in document["slots"]:
                raise GateHoldError("used campaign slot cannot be reserved again or relaunched")
            if any(
                slot["state"] in {"reserved", "running", "interrupted"}
                for slot in document["slots"].values()
            ):
                raise GateHoldError(
                    "campaign concurrency is one; reconcile the outstanding slot first"
                )
            document["slots"][key] = {
                "state": "reserved",
                "history": ["reserved"],
                "owner_pid": os.getpid(),
                "owner_machine_id": stable_host_identity()["machine_id"],
                "owner_boot_id": host_identity().get("boot_id"),
                "operation_id": f"campaign-{self.campaign_id}-{harness}-{role}",
            }
        return key

    def annotate(self, key: str, **observations: Any) -> None:
        """Persist actual product identities before allowing a worker to start."""
        if {
            "state",
            "history",
            "owner_pid",
            "owner_machine_id",
            "owner_boot_id",
        } & observations.keys():
            raise GateHoldError("campaign ownership and state cannot be overwritten")
        with self._locked() as document:
            slot = document["slots"][key]
            if slot["state"] not in {"reserved", "running"}:
                raise GateHoldError("terminal campaign slots cannot change identity")
            slot.update(observations)

    def transition(self, key: str, state: str, **observations: Any) -> None:
        with self._locked() as document:
            slot = document["slots"][key]
            if state not in TRANSITIONS[slot["state"]]:
                raise GateHoldError(f"invalid campaign transition {slot['state']} -> {state}")
            slot.update(observations, state=state)
            slot["history"].append(state)

    def finalize(
        self,
        key: str,
        state: str,
        record: Mapping[str, Any] | None = None,
        **observations: Any,
    ) -> None:
        """Commit a terminal transition and its retained evidence in one manifest replace."""
        with self._locked() as document:
            slot = document["slots"][key]
            if state not in TERMINAL or state not in TRANSITIONS[slot["state"]]:
                raise GateHoldError(f"invalid campaign finalization {slot['state']} -> {state}")
            if key in document["evidence"]:
                raise GateHoldError("campaign evidence is immutable")
            slot.update(observations, state=state)
            slot["history"].append(state)
            if record is not None:
                document["evidence"][key] = dict(record)

    def reconcile_dead(
        self, inspect: Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
    ) -> list[str]:
        """Serialize recovery separately so reservations never wait for live inspection."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".reconcile.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise GateHoldError(
                    "campaign reconciliation is still alive; concurrency is one"
                ) from error
            try:
                return self._reconcile_dead(inspect)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _reconcile_dead(
        self, inspect: Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
    ) -> list[str]:
        reconciled = []
        pending = []
        with self._locked() as document:
            for key, slot in document["slots"].items():
                if slot["state"] not in {"reserved", "running", "interrupted"}:
                    continue
                if slot["owner_machine_id"] != stable_host_identity()["machine_id"]:
                    raise GateHoldError(
                        "outstanding invocation belongs to another host; cannot establish death"
                    )
                current_boot = host_identity().get("boot_id")
                if not current_boot or not slot.get("owner_boot_id"):
                    raise GateHoldError("cannot establish campaign invocation boot identity")
                if slot["owner_boot_id"] == current_boot:
                    try:
                        os.kill(slot["owner_pid"], 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError as error:
                        raise GateHoldError(
                            "cannot establish whether campaign invocation is alive"
                        ) from error
                    else:
                        raise GateHoldError(
                            "campaign invocation is still alive; concurrency is one"
                        )
                if slot["state"] != "interrupted":
                    slot["state"] = "interrupted"
                    slot["history"].append("interrupted")
                pending.append((key, json.loads(json.dumps(slot))))
        for key, slot in pending:
            observation = dict(inspect(key, slot))
            if not observation.get("container_gone"):
                raise GateHoldError("interrupted worker remains; reconcile it without relaunch")
            record = observation.pop("evidence", None)
            self.finalize(key, "reconciled", record, reconciliation=observation)
            reconciled.append(key)
        return reconciled

    def record_protocol_evidence(self, harness: str, path: Path) -> None:
        """Bind the latest finalized scenario bytes, replacing any earlier verdict."""
        if harness not in {"omp", "claude"}:
            raise GateHoldError("protocol evidence requires a reference harness")
        path = path.absolute()
        problem = None
        with self._locked() as document:
            record: dict[str, Any] = {"path": str(path), "sha256": None, "verdict": "invalid"}
            try:
                payload = path.read_bytes()
                record["sha256"] = hashlib.sha256(payload).hexdigest()
                evidence = json.loads(payload)
                if (
                    evidence["schema_version"] != 1
                    or evidence["scenario"] != "protocol"
                    or evidence.get("verdict") not in {"pass", "fail", "hold"}
                ):
                    raise ValueError("mismatching protocol evidence identity or verdict")
                from factory_gate_evidence import exit_code_for

                if evidence["verdict"] == "pass":
                    config = evidence["identities"]["config"]
                    if (
                        config["campaign_id"] != self.campaign_id
                        or config["harness"] != harness
                        or exit_code_for(evidence) != 0
                    ):
                        raise ValueError("protocol pass lacks matching identity or assertions")
                record["verdict"] = evidence["verdict"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                problem = f"protocol evidence is unavailable or invalid: {error}"
            document["protocol_evidence"][harness] = record
        if problem is not None:
            raise GateHoldError(problem)

    def verify_evidence(self) -> list[str]:
        reasons = []
        document = self.read()
        for key in sorted(SLOTS):
            slot = document["slots"].get(key)
            record = document["evidence"].get(key)
            expected = "completed" if key.endswith("completed-workflow") else "cancelled"
            terminal = slot is not None and (
                slot["state"] == expected
                or (
                    slot["state"] == "reconciled"
                    and slot.get("reconciliation", {}).get("terminal_status") == expected
                )
            )
            if not terminal or not record:
                reasons.append(f"{key}: missing successful terminal evidence")
                continue
            originals = {record["path"]: record["sha256"], **record.get("raw_originals", {})}
            for name, expected_digest in originals.items():
                path = Path(name)
                if not path.is_file() or sha256(path) != expected_digest:
                    reasons.append(f"{key}: missing or stale digest {name}")
        for harness in ("omp", "claude"):
            record = document["protocol_evidence"].get(harness)
            if not isinstance(record, dict) or record.get("verdict") != "pass":
                reasons.append(f"{harness}: missing passing protocol evidence")
                continue
            try:
                path = Path(record["path"])
                if not path.is_absolute() or sha256(path) != record["sha256"]:
                    reasons.append(f"{harness}: missing or stale protocol evidence digest")
            except (OSError, KeyError, TypeError):
                reasons.append(f"{harness}: missing or stale protocol evidence digest")
        return reasons

    def record_reuse(self, records: Any, reasons: list[str]) -> None:
        with self._locked() as document:
            document["reuse"].append({"records": records, "reasons": reasons})
