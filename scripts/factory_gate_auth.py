"""The ``runner-auth`` scenario: exactly one non-mutating authentication session through the relay.

Without ``--live`` it runs the same code path against a loopback stub upstream that answers a
minimal valid stream for the chosen harness's route (mode ``local``). With ``--live`` the relay's
upstream is the operator's real gateway and its token file is the operator's: the driver makes the
session count, the process-group deadline and the relay's request log part of the evidence. A live
run is an owner-authorized action: this module never starts one by itself and the command line
refuses it without an explicit confirmation.

What the evidence rests on:

* the number of harness containers is counted host-side from the engine's own event stream for the
  session window, never from a counter the driver set itself;
* the deadline is enforced by a supervisor that kills the launcher's whole process group and the
  container, then proves both are gone;
* the gateway token value is searched for in every artifact source, in-process, before any of them
  is scrubbed or cleaned up (the value is never printed);
* a conforming auth-custody receipt for the host is required (and recorded by digest);
* a gateway 401/403/429, no reported usage figures or a deadline in a live run is a HOLD with a
  stated reason, never a failure of the product.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from enginery.adapters.claude_code import ClaudeCodeAdapterConfig, ClaudeCodeHarness
from enginery.adapters.oci.engine import OPERATION_LABEL
from enginery.adapters.oci.launch import container_name_for
from enginery.adapters.oci.probe import OciProbeRunner
from enginery.adapters.oci.relay import RelayManager
from enginery.adapters.omp import OmpAdapterConfig, OmpHarness
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.work_ports import HarnessTask
from enginery.cli.runner import _inspect_token_file
from enginery.domain.custody import (
    AuthCustodyReceipt,
    CustodyHost,
    build_custody_receipt,
    custody_receipt_matches_host,
)
from enginery.domain.errors import EngineryError
from enginery.domain.execution_selection import (
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)
from enginery.domain.ids import NodeAttemptId, NodeId, OperationId, RunId
from enginery.domain.runner import (
    MODEL_ACCESS_RELAY,
    RelayEndpoint,
    RunnerContext,
    RunnerSelection,
)
from enginery.ledger.artifact_store import ArtifactStore
from factory_gate_boundary import (
    REQUIRED as BOUNDARY_REQUIRED,
)
from factory_gate_boundary import (
    _count_decisions,
    _relay_up,
    step_read,
    step_setup,
)
from factory_gate_context import Gate, GateHoldError, receipt_summary
from factory_gate_support import (
    UpstreamStub,
    launcher_environment,
    new_task_workspace,
    read_json,
)

HARNESS_IDS = {"omp": "omp", "claude": "claude-code"}
ROUTES = {"omp": "/v1/pi/stream", "claude": "/v1/messages"}
MAX_DEADLINE_SECONDS = 900
TASK_BUDGET_SECONDS = 600
PROMPT = "Reply with the single word OK and nothing else. Do not use any tools."

# omp refreshes its model catalog at startup with GETs the relay refuses (they are not one of the
# three forwarded routes) and that never reach the gateway; the session does not depend on them.
# The tolerance is exactly these two method+path pairs for the omp harness and nothing else: a
# denied POST, any other denied path or any denied request of another harness is a failure.
_OMP_DISCOVERY_GETS = frozenset({("GET", "/models"), ("GET", "/codex/models")})

GATEWAY_HOLD_STATUSES = frozenset({401, 403, 429})
_KILL_GRACE_SECONDS = 10
_GONE_WAIT_SECONDS = 8.0


def _is_refused_discovery(harness_name: str, entry: dict[str, Any]) -> bool:
    return harness_name == "omp" and (entry["method"], entry["path"]) in _OMP_DISCOVERY_GETS


AUTH_REQUIRED: dict[str, str] = {
    "auth-custody-receipt": (
        "A conforming auth-custody receipt for this host exists and its digest is recorded"
    ),
    "auth-harness-container-count": (
        "Exactly one harness container was created during the session, counted host-side from "
        "the engine's event stream"
    ),
    "auth-session-completed": (
        "The one session ran the real harness in the runner through the relay and answered OK"
    ),
    "auth-relay-request-log": (
        "The relay's request log shows allowed model requests only on the harness's route and "
        "zero denied requests"
    ),
    "auth-harness-identity": (
        "The harness version and protocol probed inside the image match the image labels and the "
        "execution catalog"
    ),
    "auth-usage-recorded": "Usage reported by the harness output is recorded (or reported absent)",
    "auth-deadline-enforced": (
        "The driver enforced an external process-group deadline of at most 900 seconds, the "
        "session finished inside it, and no process or container of the session survived"
    ),
    "auth-profile-violations-none": (
        "The session's runner receipt has an empty profile_violations and no permission bypass"
    ),
    "auth-no-token-in-artifacts": (
        "The gateway token value appears in no artifact source: relay log, receipts, launch spec, "
        "artifact store, evidence, raw directory, launcher output"
    ),
}


class SessionCounter:
    """Counts authentication sessions; starting a second one is an error, not a retry."""

    def __init__(self) -> None:
        self.count = 0

    def begin(self) -> None:
        if self.count >= 1:
            raise RuntimeError("a second authentication session was requested")
        self.count += 1


def required_assertions(options: Any) -> dict[str, str]:
    """Credential-read refusals (before the session, same image) plus the session assertions."""
    required = {key: value for key, value in BOUNDARY_REQUIRED.items() if key.startswith("a-")}
    required.update(AUTH_REQUIRED)
    return required


# --- supervision: the deadline kills the process group and the container ---------------------


class SessionEngine(Protocol):
    """The engine surface the supervisor needs (a real OciEngine or a test double)."""

    def kill(self, name: str) -> None: ...

    def remove(self, name: str, *, force: bool = True) -> None: ...

    def container_exists(self, name: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class Supervised:
    """What running the launcher under the deadline left behind."""

    completed: subprocess.CompletedProcess[str]
    timed_out: bool
    duration: float
    group_gone: bool
    stragglers_killed: bool
    container_gone: bool

    @property
    def cleaned_up(self) -> bool:
        return self.group_gone and self.container_gone


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate: Callable[[], bool], seconds: float) -> bool:
    end = time.monotonic() + seconds
    while True:
        if predicate():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def supervise(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    deadline_seconds: float,
    engine: SessionEngine,
    container: str,
) -> Supervised:
    """Run ``argv`` in its own process group under a hard deadline, then prove nothing survived.

    At the deadline the container is killed and removed, the group gets SIGTERM and, after a
    grace period, SIGKILL. On every path (deadline or not) the group is sent SIGKILL once more
    and the supervisor waits until the group and the container are verifiably gone.
    """
    started = time.monotonic()
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
        start_new_session=True,
    )
    pgid = process.pid
    timed_out = False

    def stop_container() -> None:
        with contextlib.suppress(EngineryError):
            engine.kill(container)
        with contextlib.suppress(EngineryError):
            engine.remove(container, force=True)

    try:
        stdout, stderr = process.communicate(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_container()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, 15)
        try:
            stdout, stderr = process.communicate(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, 9)
            stdout, stderr = process.communicate()
    duration = time.monotonic() - started
    stragglers = _group_alive(pgid)
    if stragglers:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, 9)
    group_gone = _wait_until(lambda: not _group_alive(pgid), _GONE_WAIT_SECONDS)
    if timed_out:
        stop_container()
    container_gone = _wait_until(lambda: not engine.container_exists(container), _GONE_WAIT_SECONDS)
    return Supervised(
        completed=subprocess.CompletedProcess(
            list(argv), process.returncode, stdout or "", stderr or ""
        ),
        timed_out=timed_out,
        duration=duration,
        group_gone=group_gone,
        stragglers_killed=stragglers,
        container_gone=container_gone,
    )


# --- host-side container count ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContainerCount:
    harness: tuple[str, ...]
    stores: tuple[str, ...]
    others: tuple[str, ...]


def count_created_containers(
    events_text: str, *, operation_id: str, expected_name: str, relay_name: str
) -> ContainerCount:
    """Harness containers among the ``container create`` events of the engine's event stream.

    A harness container is one that carries this session's operation label or has its container
    name; containers of anything else (the relay, probes, a concurrent run on the same engine)
    are listed separately and never counted.
    """
    harness: list[str] = []
    stores: list[str] = []
    others: list[str] = []
    for line in events_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("Type") != "container" or event.get("Action") != "create":
            continue
        actor = event.get("Actor")
        attributes = actor.get("Attributes", {}) if isinstance(actor, dict) else {}
        name = str(attributes.get("name", ""))
        if name == relay_name:
            continue
        if name == f"{expected_name}-store":
            # The attempt's store container seeds and reads back the capped volumes; it is the
            # launcher's own helper, not a second harness.
            stores.append(name)
            continue
        label = name or str(event.get("id", ""))[:12]
        if name == expected_name or attributes.get(OPERATION_LABEL) == operation_id:
            harness.append(label)
        else:
            others.append(label)
    return ContainerCount(tuple(harness), tuple(stores), tuple(others))


def engine_time(gate: Gate) -> str | None:
    """The engine's own clock (immune to a skewed VM clock); ``None`` when it cannot be read."""
    completed = gate.engine_cli("info", "--format", "{{.SystemTime}}", timeout=60)
    text = completed.stdout.strip()
    return text if completed.returncode == 0 and text else None


def engine_creates(gate: Gate, since: str, until: str) -> str | None:
    completed = gate.engine_cli(
        "events",
        "--since",
        since,
        "--until",
        until,
        "--filter",
        "type=container",
        "--filter",
        "event=create",
        "--format",
        "{{json .}}",
        timeout=90,
    )
    return completed.stdout if completed.returncode == 0 else None


_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def later(stamp: str, seconds: int) -> str | None:
    """``stamp`` (RFC 3339, optional fraction) plus ``seconds``, as RFC 3339; ``None`` if odd."""
    match = _STAMP.fullmatch(stamp.strip())
    if match is None:
        return None
    base, fraction, zone = match.groups()
    moment = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S") + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + (fraction or "") + zone


# --- the token scan --------------------------------------------------------------------------

EXPECTED_SCAN_SOURCES = (
    "relay-log",
    "launcher-stdout",
    "launcher-stderr",
    "launch-spec",
    "harness-output",
    "receipt-file",
    "evidence",
    "upstream-requests",
    "directory:receipts",
    "directory:artifact-store",
    "directory:result-out",
    "directory:raw",
)


def scan_for_token(
    token: str, texts: Mapping[str, str], directories: Mapping[str, Path]
) -> tuple[dict[str, list[str]], list[str]]:
    """Which sources hold ``token``: ``({source: [file names]}, [sources scanned])``.

    Only source labels and file names are returned; the value itself never leaves this function.
    """
    hits: dict[str, list[str]] = {}
    scanned: list[str] = []
    needle = token.encode("utf-8")
    if not needle:
        raise ValueError("an empty token cannot be scanned for")
    for label, text in texts.items():
        scanned.append(label)
        if needle in text.encode("utf-8", "replace"):
            hits.setdefault(label, []).append("<memory>")
    for label, directory in directories.items():
        scanned.append(label)
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                hits.setdefault(label, []).append(f"{path.name} (unreadable)")
                continue
            if needle in data:
                hits.setdefault(label, []).append(str(path.relative_to(directory)))
    return hits, scanned


# --- verdict mapping -------------------------------------------------------------------------


def parse_relay_log(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "relay":
            entries.append(
                {"method": parts[1], "path": parts[2], "decision": parts[3], "status": parts[4]}
            )
    return entries


def gateway_hold_reason(
    log: Sequence[Mapping[str, str]], *, live: bool, usage: Mapping[str, Any] | None
) -> str | None:
    """Why the session cannot be judged: the gateway refused it or no usage was reported.

    Only a *forwarded* request's status is the gateway's answer; the relay's own refusals are
    product behaviour and stay failures.
    """
    for entry in log:
        if not entry["decision"].startswith("forward"):
            continue
        try:
            status = int(entry["status"])
        except ValueError:
            continue
        if status in GATEWAY_HOLD_STATUSES:
            return (
                f"the gateway answered {status} to {entry['method']} {entry['path']} "
                "(upstream authentication/authorization/rate limit, not a runner defect)"
            )
    if live and not usage:
        return (
            "no usage figures were reported: the harness output carried none, so the session "
            "cannot be shown to have run on the subscription"
        )
    return None


def decide_outcome(
    *,
    live: bool,
    security_ok: bool,
    timed_out: bool,
    cleaned_up: bool,
    deadline_seconds: float,
    gateway_reason: str | None,
) -> tuple[str | None, str | None]:
    """``("hold"|"fail"|None, reason)`` beyond what the individual assertions already say.

    A security assertion that failed (leak, container count, profile, survivors) is never
    downgraded to a hold. A deadline that was hit and fully cleaned up is a hold in a live run
    (the gateway or model was slow) and a failure against the local stub, which cannot be slow.
    """
    if not security_ok:
        return None, None
    if timed_out:
        reason = f"the session deadline of {deadline_seconds:g}s was reached"
        if not cleaned_up:
            return "fail", reason + " and a process or container of the session survived"
        return ("hold" if live else "fail"), reason
    if gateway_reason is not None:
        return "hold", gateway_reason
    return None, None


# --- custody ---------------------------------------------------------------------------------


def _custody_host() -> CustodyHost:
    return CustodyHost.MACOS if platform.system() == "Darwin" else CustodyHost.UBUNTU_LIMA


def load_live_custody(
    path: Path | None, *, gateway_url: str, token_file: Path, host: CustodyHost | None = None
) -> AuthCustodyReceipt:
    """The operator's receipt, or a ``GateHoldError`` saying exactly what is missing or wrong."""
    if path is None:
        raise GateHoldError(
            "a live session requires --custody-receipt PATH (made with "
            "`enginery runner custody-receipt`); none was given"
        )
    try:
        with path.open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise GateHoldError("the custody receipt is larger than 1 MiB")
        state = json.loads(raw.decode("utf-8"))
        receipt = AuthCustodyReceipt.from_state(state)
    except GateHoldError:
        raise
    except (OSError, ValueError, EngineryError) as error:
        raise GateHoldError(f"the custody receipt cannot be used: {error}") from error
    gate_host = _custody_host() if host is None else host
    if not custody_receipt_matches_host(state, gate_host):
        raise GateHoldError(
            f"the custody receipt was made for {receipt.host.value}, but this gate runs on "
            f"{gate_host.value}; make one on this host"
        )
    if not receipt.conforms:
        raise GateHoldError(
            "the custody receipt does not conform: " + "; ".join(receipt.findings)[:400]
        )
    if receipt.gateway.url != gateway_url:
        raise GateHoldError("the custody receipt describes a different gateway url")
    if receipt.token_file.path != str(token_file):
        raise GateHoldError("the custody receipt describes a different token file")
    current = build_custody_receipt(
        host=receipt.host,
        gateway_url=receipt.gateway.url,
        broker_url=receipt.broker.url,
        token_file=_inspect_token_file(token_file),
        created_at=datetime.now(UTC),
    )
    if not current.conforms:
        raise GateHoldError(
            "the token file no longer conforms: " + "; ".join(current.findings)[:400]
        )
    return receipt


def build_local_custody(*, gateway_url: str, token_file: Path) -> AuthCustodyReceipt:
    """Receipt for a non-live run: built over the stub token file; metadata only."""
    return build_custody_receipt(
        host=_custody_host(),
        gateway_url=gateway_url,
        broker_url="http://127.0.0.1:1",
        token_file=_inspect_token_file(token_file),
        created_at=datetime.now(UTC),
    )


def with_relay_container(argv: Sequence[str], relay_container: str) -> list[str]:
    """Point the launch spec in ``argv`` at the gate's own relay container."""
    out = list(argv)
    index = out.index("--spec") + 1
    state = json.loads(out[index])
    state["relay_container"] = relay_container
    out[index] = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return out


def _harness_for(gate: Gate, context: RunnerContext, store: ArtifactStore) -> Any:
    probe = OciProbeRunner(gate.engine, gate.runner_image.image_id, gate.profile)
    if gate.options.harness == "omp":
        return OmpHarness(
            OmpAdapterConfig(credential_reference="relay-placeholder-credential"),
            store,
            command_runner=probe,
            runner=context,
        )
    return ClaudeCodeHarness(
        ClaudeCodeAdapterConfig(credential_reference="relay-placeholder-credential"),
        store,
        command_runner=probe,
        runner=context,
    )


def _task(gate: Gate, workspace: Path, harness_id: str) -> HarnessTask:
    catalog = load_execution_catalog()
    model = gate.options.model
    selection = resolve_execution_selection(
        (
            LayerControls(
                layer=SelectionLayer.RUN,
                harness=harness_id,
                model=model,
                effort=None,
                model_pinned=model is not None,
                effort_pinned=False,
                strict_effective=None,
            ),
        ),
        catalog=catalog,
    )
    return HarnessTask(
        run_id=RunId(f"gate-auth-{gate.rid}"),
        node_id=NodeId("authenticate"),
        attempt_id=NodeAttemptId("attempt-1"),
        operation_id=OperationId(f"gate-auth-{gate.rid}"),
        workspace_path=workspace,
        objective=PROMPT,
        acceptance_criteria=("the reply is exactly the word OK",),
        constraints=("do not call any tool", "do not read or modify any file"),
        permitted_capabilities=("none",),
        evidence_requirements=("none",),
        time_budget_seconds=TASK_BUDGET_SECONDS,
        cost_budget=Decimal(0),
        selection=selection,
    )


def _events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                document = json.loads(line)
            except ValueError:
                continue
            if isinstance(document, dict):
                events.append(document)
    return events


def _texts(value: Any, found: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("text", "result") and isinstance(item, str):
                found.append(item)
            else:
                _texts(item, found)
    elif isinstance(value, list):
        for item in value:
            _texts(item, found)


def _usage(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    for event in events:
        stack: list[Any] = [event]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                usage = item.get("usage")
                if isinstance(usage, dict) and usage:
                    latest = {
                        key: value
                        for key, value in usage.items()
                        if isinstance(value, int | float | str)
                    }
                if "total_cost_usd" in item and latest is not None:
                    latest["total_cost_usd"] = item["total_cost_usd"]
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return latest


def _replied_ok(events: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    found: list[str] = []
    for event in events:
        _texts(event, found)
    normalized = [text.strip().rstrip(".!").strip().upper() for text in found if text.strip()]
    return "OK" in normalized, [text.strip()[:80] for text in found if text.strip()][-6:]


class _Recorder:
    """Records assertions and keeps their pre-redaction text for the token scan."""

    def __init__(self, gate: Gate) -> None:
        self.gate = gate
        self.texts: list[str] = []

    def assertion(self, assertion_id: str, **fields: Any) -> bool:
        self.texts.append(json.dumps({assertion_id: fields}, sort_keys=True, default=str))
        return bool(self.gate.evidence.assertion(assertion_id, **fields))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run(gate: Gate) -> None:
    options = gate.options
    fixture = gate.fixture
    assert fixture is not None and gate.relay_image is not None
    harness_name = str(options.harness)
    harness_id = HARNESS_IDS[harness_name]
    live = bool(options.live)
    recorder = _Recorder(gate)

    # The token, its file and the host's custody receipt come first: a live run without a
    # conforming receipt is held before any container exists.
    stub: UpstreamStub | None = None
    if live:
        assert options.gateway_url is not None and options.gateway_token_file is not None
        token_file = options.gateway_token_file
        custody = load_live_custody(
            getattr(options, "custody_receipt", None),
            gateway_url=options.gateway_url,
            token_file=token_file,
        )
        token = token_file.read_text(encoding="utf-8").strip()
        gate.scrubber.register("gateway-token", token)
        candidates = [options.gateway_url]
    else:
        token = f"gate-fake-gateway-{secrets.token_hex(16)}"
        gate.scrubber.register("fake-gateway-token", token)
        token_dir = gate.workroot / "relay"
        token_dir.mkdir(mode=0o700)
        token_file = token_dir / "gateway.token"
        token_file.write_text(token + "\n", encoding="utf-8")
        token_file.chmod(0o600)
        stub = UpstreamStub(expected_bearer=token, reply_text="OK")
        stub.start()
        gate.upstream = stub
        candidates = [f"http://host.docker.internal:{stub.port}"]
        custody = build_local_custody(gateway_url=candidates[0], token_file=token_file)
    custody_ref = gate.raw_write("auth-custody-receipt.json", custody.to_state())
    custody_digest = str(custody.digest)
    gate.evidence.details["auth_custody_receipt"] = {
        "digest": custody_digest,
        "host": custody.host.value,
        "conforms": custody.conforms,
        "origin": "operator-provided" if live else "built over the stub token file",
    }

    setup = step_setup(gate)
    read_attempt = step_read(gate, name="auth-excluded-credentials")
    gate.evidence.details["excluded_credential_attempt_image"] = read_attempt.receipt_ref
    if (
        stub is not None
        and setup.get("bridge_gateway")
        and gate.engine_identity.kind != "docker-desktop"
    ):
        candidates.append(f"http://{setup['bridge_gateway']}:{stub.port}")
    manager = RelayManager(
        gate.engine,
        profile=gate.profile,
        egress_network=gate.egress_network,
        container_name=gate.relay_container,
    )
    gate.track_container(gate.relay_container)
    gate.track_network(gate.egress_network)

    def release_relay() -> None:
        manager.down()

    gate.on_cleanup(release_relay)
    upstream, notes = _relay_up(
        gate, manager, upstream_candidates=candidates, token_file=token_file, probe_stub=stub
    )
    gate.evidence.details["relay"] = {
        "upstream_reachable": upstream is not None,
        "notes": notes,
        "status": manager.status().to_state(),
    }
    if upstream is None:
        raise EngineryError("the relay could not be started against the configured upstream")

    selection = RunnerSelection.isolated(
        image=gate.runner_image.image_id,
        profile=gate.profile,
        docker_context=gate.docker_context,
        model_access=MODEL_ACCESS_RELAY,
    )
    receipts_dir = Path(str(fixture.receipts))
    context = RunnerContext(
        selection=selection,
        profile=gate.profile,
        receipt_root=str(receipts_dir),
        relay=RelayEndpoint(),
    )
    store_root = fixture.root / "session-artifacts"
    store = ArtifactStore(store_root)
    harness = _harness_for(gate, context, store)
    installed = harness.installed_host()
    workspace = new_task_workspace(fixture, "auth-session")
    task = _task(gate, workspace, harness_id)
    out = Path(str(workspace) + ".out")
    result_path = out / "result.json"
    argv = with_relay_container(
        harness.supervised_command(task, result_path=result_path), gate.relay_container
    )
    container = container_name_for(str(task.operation_id))
    gate.track_container(container)

    deadline = min(options.session_deadline_seconds, MAX_DEADLINE_SECONDS)
    counter = SessionCounter()
    counter.begin()  # the one and only session this driver ever starts
    window_start = engine_time(gate)
    supervised = supervise(
        argv,
        env=launcher_environment(),
        deadline_seconds=deadline,
        engine=gate.engine,
        container=container,
    )
    window_end = engine_time(gate)
    completed = supervised.completed
    timed_out = supervised.timed_out
    duration = supervised.duration

    events_text: str | None = None
    until = None if window_end is None else later(window_end, 1)
    if window_start is not None and until is not None:
        events_text = engine_creates(gate, window_start, until)
    created = (
        None
        if events_text is None
        else count_created_containers(
            events_text,
            operation_id=str(task.operation_id),
            expected_name=container,
            relay_name=gate.relay_container,
        )
    )

    result = read_json(result_path)
    output = str(result.get("output", "")) if result else ""
    launcher_text = completed.stdout + completed.stderr
    leaked = gate.scrubber.contains(output + launcher_text)
    if leaked:
        gate.leaks.append({"attempt": "auth-session", "labels": leaked})
    spec_text = json.dumps(json.loads(argv[argv.index("--spec") + 1]), sort_keys=True)
    gate.raw_write("auth-session.launch-spec.json", json.loads(spec_text))
    gate.raw_write("auth-session.launcher-stderr.txt", completed.stderr)
    if result is not None:
        gate.raw_write("auth-session.result.json", result)
    receipt_path = receipts_dir / f"{task.operation_id}.runner-receipt.json"
    from factory_gate_support import read_receipt, violations_of

    receipt, receipt_document = read_receipt(receipt_path)
    if receipt_document is not None:
        gate.raw_write("auth-session.runner-receipt.json", receipt_document)
    events = _events(output)
    replied, replies = _replied_ok(events)
    usage = _usage(events)
    collected = "not collected"
    try:
        harness.collect_supervised(task, result_path=result_path, launched_command=tuple(argv))
        collected = "collected"
    except EngineryError as error:
        collected = f"{type(error).__name__}: {error}"

    try:
        log_text = gate.engine.logs_tail(gate.relay_container, lines=2000, max_bytes=512 * 1024)
    except EngineryError:
        log_text = ""
    log = parse_relay_log(log_text)
    gate.raw_write("auth-session.relay-log.json", log)
    decisions = _count_decisions(log)
    forwarded = [e for e in log if e["decision"].startswith("forward")]
    posts = [e for e in forwarded if e["method"] == "POST"]
    denied = [e for e in log if e not in forwarded and e["decision"] != "health"]
    unexpected_denied = [e for e in denied if not _is_refused_discovery(harness_name, e)]
    expected_route = ROUTES[harness_name]
    wrong_route = [e for e in posts if e["path"] != expected_route]
    stub_requests = [] if stub is None else stub.snapshot()
    if stub is not None:
        gate.raw_write("auth-session.upstream-requests.json", [r.to_state() for r in stub_requests])
    inner = " ".join(argv[argv.index("--") + 1 : argv.index("--") + 3])
    session_command = [
        f"{Path(argv[0]).name} -m enginery.adapters.oci.launcher --spec <launch spec> -- {inner} "
        f"... (the real {harness_name} command from the adapter, isolated permission mapping)",
    ]
    raw_refs = [
        f"{options.raw_dir.name}/{name}"
        for name in (
            "auth-session.launch-spec.json",
            "auth-session.launcher-stderr.txt",
            "auth-session.result.json",
        )
    ]
    recorder.assertion(
        "auth-custody-receipt",
        passed=custody.conforms,
        commands=[
            "enginery runner custody-receipt --host <host> --gateway-url <url> --broker-url <url> "
            "--token-file <path>  # metadata only"
            if live
            else "build_custody_receipt(...) over the stub token file (metadata only)"
        ],
        observed={
            "digest": custody_digest,
            "host": custody.host.value,
            "conforms": custody.conforms,
            "findings": list(custody.findings),
            "origin": "operator-provided" if live else "built over the stub token file",
        },
        expected="a receipt that conforms (token file regular, owned by the invoker, mode 0600)",
        observer={"token_file_inspected_now": _inspect_token_file(token_file).to_state()},
        raw_artifacts=[custody_ref],
    )
    recorder.assertion(
        "auth-harness-container-count",
        passed=created is not None
        and len(created.harness) == 1
        and created.harness[0] == container,
        commands=[
            "docker info --format '{{.SystemTime}}'  # session window, engine clock",
            "docker events --since <start> --until <end> --filter type=container "
            "--filter event=create",
        ],
        observed={
            "window": {"start": window_start, "end": window_end},
            "harness_containers_created": None if created is None else list(created.harness),
            "harness_container_count": None if created is None else len(created.harness),
            "store_containers_created": None if created is None else list(created.stores),
            "other_containers_created": None if created is None else list(created.others),
            "expected_container": container,
            "events_readable": created is not None,
        },
        expected="exactly one harness container created during the session, the expected one",
        observer={
            "engine_event_lines": 0 if events_text is None else len(events_text.splitlines())
        },
    )
    recorder.assertion(
        "auth-session-completed",
        passed=completed.returncode == 0
        and not timed_out
        and result is not None
        and result.get("terminal_status") == "succeeded"
        and replied
        and collected == "collected",
        commands=session_command,
        observed={
            "launcher_exit_code": completed.returncode,
            "terminal_status": None if result is None else result.get("terminal_status"),
            "replied_ok": replied,
            "reply_texts_tail": replies,
            "harness_events": len(events),
            "adapter_collect_supervised": collected,
            "duration_seconds": round(duration, 1),
            "mode": options.mode,
            "route": expected_route,
        },
        expected="exit 0, terminal_status succeeded, an assistant reply of OK, result collected",
        observer={
            "relay_log_decisions": decisions,
            "stub_upstream_requests": len(stub_requests),
            "container_removed": supervised.container_gone,
        },
        raw_artifacts=raw_refs,
    )
    upstream_posts = [r for r in stub_requests if r.method == "POST"] if stub else []
    recorder.assertion(
        "auth-relay-request-log",
        passed=bool(posts)
        and not unexpected_denied
        and not wrong_route
        and all(r.authorization_matches_gateway for r in stub_requests),
        commands=["docker logs <relay container>  # relay <method> <path> <decision> <status>"],
        observed={
            "decision_counts": decisions,
            "allowed_posts": len(posts),
            "allowed_posts_detail": [f"{e['method']} {e['path']} -> {e['status']}" for e in posts],
            "denied_requests": len(denied),
            "denied_detail": denied,
            "tolerated_discovery_denials": len(denied) - len(unexpected_denied),
            "forwarded_total": len(forwarded),
            "posts_on_unexpected_route": wrong_route,
            "single_post_as_hoped": len(posts) == 1,
            "note": (
                "one session may carry more than one HTTP request (for example a title or "
                "warm-up call); the evidence records the real count"
            ),
            "stub_upstream_posts": len(upstream_posts),
        },
        expected=(
            "at least one allowed POST on the harness route; no denied POST and no denied "
            "model or provider; the only denied requests tolerated are omp's catalog-discovery "
            "GETs (/models, /codex/models), which the relay refuses and which never reach the "
            "gateway"
        ),
        observer={"stub_upstream_requests": [r.to_state() for r in stub_requests]},
        raw_artifacts=[f"{options.raw_dir.name}/auth-session.relay-log.json"],
    )
    version_key = harness_id if harness_id != "omp" else "omp"
    label_version = dict(gate.runner_image.harness_versions).get(version_key)
    catalog = load_execution_catalog()
    catalog_host = catalog.host(harness_id)
    recorder.assertion(
        "auth-harness-identity",
        passed=installed is not None
        and label_version is not None
        and installed.executable_version == label_version
        and installed.executable_version == catalog_host.tested_executable_version
        and installed.protocol_fingerprint == catalog_host.protocol_fingerprint,
        commands=[
            f"OciProbeRunner(...)({harness_name} --version | --help)  # throw-away container"
        ],
        observed={
            "harness": harness_id,
            "probed_version": None if installed is None else installed.executable_version,
            "probed_protocol": None if installed is None else installed.protocol_fingerprint,
            "image_label_version": label_version,
            "catalog_tested_version": catalog_host.tested_executable_version,
            "catalog_accepted_versions": catalog_host.version.details().get(
                "accepted_executable_versions"
            ),
            "catalog_protocol": catalog_host.protocol_fingerprint,
        },
        expected="probed version equals the image label and the catalog entry's tested version",
        observer={"image_labels_read_by_the_engine": dict(gate.runner_image.harness_versions)},
    )
    recorder.assertion(
        "auth-usage-recorded",
        passed=completed.returncode == 0,
        commands=["parse the harness JSON output for usage / total_cost_usd"],
        observed={
            "usage": usage,
            "usage_reported_by_harness": usage is not None,
            "note": None if usage is not None else "the harness output carried no usage figures",
        },
        expected="usage recorded when reported; absence stated",
        observer={"harness_json_events_parsed": len(events)},
    )
    recorder.assertion(
        "auth-deadline-enforced",
        passed=deadline <= MAX_DEADLINE_SECONDS
        and not timed_out
        and duration < deadline
        and supervised.group_gone
        and supervised.container_gone,
        commands=[
            "subprocess.Popen(launcher, start_new_session=True).communicate(timeout=deadline)"
        ],
        observed={
            "deadline_seconds": deadline,
            "maximum_allowed_seconds": MAX_DEADLINE_SECONDS,
            "duration_seconds": round(duration, 1),
            "deadline_hit": timed_out,
            "process_group_gone": supervised.group_gone,
            "stragglers_killed_after_exit": supervised.stragglers_killed,
            "container_gone": supervised.container_gone,
            "kill_path": "docker kill + rm -f <container>, killpg(SIGTERM) then SIGKILL",
        },
        expected=(
            f"deadline <= {MAX_DEADLINE_SECONDS}s, the session ended before it, and neither a "
            "process of its group nor its container survived"
        ),
        observer={
            "launcher_exit_code": completed.returncode,
            "container_exists_now": gate.engine.container_exists(container),
        },
    )
    violations = violations_of(gate.profile, receipt, relay=True)
    recorder.assertion(
        "auth-profile-violations-none",
        passed=receipt is not None
        and not violations
        and not receipt.permissions.unconditional_bypass,
        commands=["profile_violations(profile, receipt.effective, network=<attempt network>)"],
        observed={
            "violations": list(violations),
            "permission_mapping": None if receipt is None else receipt.permissions.name,
            "unconditional_bypass": None
            if receipt is None
            else receipt.permissions.unconditional_bypass,
            "receipt": receipt_summary(receipt),
        },
        expected="no violations and a non-bypass permission mapping",
        observer={"engine_reported_container": receipt_summary(receipt)},
        raw_artifacts=[f"{options.raw_dir.name}/auth-session.runner-receipt.json"],
    )

    # The scan covers every source BEFORE anything is redacted or cleaned up: the texts below are
    # the raw values as produced, not what was written through the scrubber.
    scan_texts = {
        "relay-log": log_text,
        "launcher-stdout": completed.stdout,
        "launcher-stderr": completed.stderr,
        "launch-spec": json.dumps(argv),
        "harness-output": output + json.dumps(result, default=str),
        "receipt-file": _read_text(receipt_path),
        "evidence": "\n".join(recorder.texts)
        + json.dumps(gate.evidence.details, default=str, sort_keys=True),
        "upstream-requests": json.dumps([r.to_state() for r in stub_requests]),
    }
    scan_directories = {
        "directory:receipts": receipts_dir,
        "directory:artifact-store": store_root,
        "directory:result-out": out,
        "directory:raw": options.raw_dir,
    }
    hits, scanned = scan_for_token(token, scan_texts, scan_directories)
    missing_sources = sorted(set(EXPECTED_SCAN_SOURCES) - set(scanned))
    recorder.assertion(
        "auth-no-token-in-artifacts",
        passed=not gate.leaks and not hits and not missing_sources,
        commands=[
            "scan, in-process and before redaction, the relay log, launcher output, launch spec, "
            "harness output, receipts, artifact store, evidence and raw directory for the gateway "
            "token value (never printed)"
        ],
        observed={
            "pre_redaction_leaks_in_container_output": gate.leaks,
            "sources_scanned": scanned,
            "sources_not_scanned": missing_sources,
            "sources_with_token": hits,
        },
        expected="the token value appears in no source, and every expected source was scanned",
        observer={"sources_scanned": len(scanned)},
    )
    gate.effects["hosted_inference_calls"] = len(posts) if live else 0
    gate.effects["authentication_sessions"] = counter.count
    gate.effects["relay_forwarded_requests"] = len(forwarded)
    gate.effects["relay_denied_requests"] = len(denied)

    security_ok = (
        not gate.leaks
        and not hits
        and not missing_sources
        and created is not None
        and len(created.harness) == 1
        and not violations
        and receipt is not None
        and not unexpected_denied
        and not wrong_route
    )
    verdict, reason = decide_outcome(
        live=live,
        security_ok=security_ok,
        timed_out=timed_out,
        cleaned_up=supervised.cleaned_up,
        deadline_seconds=deadline,
        gateway_reason=gateway_hold_reason(log, live=live, usage=usage),
    )
    if verdict == "hold" and reason is not None:
        gate.evidence.hold(reason)
    elif verdict == "fail" and reason is not None:
        gate.evidence.abort(reason)


def finalize(gate: Gate) -> None:
    gate.effects.setdefault("hosted_inference_calls", 0)
    gate.effects.setdefault("authentication_sessions", 0)
    gate.effects["stub_upstream_requests"] = (
        0 if gate.upstream is None else len(gate.upstream.snapshot())
    )
