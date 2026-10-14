"""The ``runner-boundary`` scenario: real attempts against the real isolated runner.

Every step runs hostile code inside a container the product's own launcher created (so the
runner receipt's ``profile_violations`` is checked for each), and judges the outcome from what the
*host* observes independently of the worker: file hashes, listener connection logs, the bare
origin's refs, marker files, the engine's own event stream and the stub gateway's request log.
A step that cannot run is a failed assertion; the gate never counts an unexecuted step as passed.
"""

from __future__ import annotations

import dataclasses
import json
import re
import secrets
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from enginery.adapters.git_publication import GitBranchPublisher
from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import (
    create_arguments,
    effective_from_inspect,
    launch_violations,
    store_names_for,
    store_violations,
)
from enginery.adapters.oci.relay import RelayManager
from enginery.adapters.oci.validation import OciValidationRunner
from enginery.application.branch_publication import BranchPublicationRequest
from enginery.domain.errors import EngineryError, ExternalConflictError
from enginery.domain.execution_selection import HoldReason
from enginery.domain.ids import OperationId
from enginery.domain.runner import (
    NO_NETWORK,
    OUT_MOUNT,
    RELAY_HOSTNAME,
    RELAY_PORT,
    WORKSPACE_MOUNT,
    ResourceLimits,
    RunnerContext,
    RunnerProfile,
    RunnerSelection,
    profile_violations,
)
from enginery.engine.task_repository import (
    intake_branch_bundle,
    intake_path_for,
    validation_path_for,
    verified_head_checkout,
    verify_intake_branch,
)
from factory_gate_context import (
    Gate,
    GateHoldError,
    failing_operations,
    receipt_summary,
    summarize_operations,
)
from factory_gate_support import (
    GATE_LABEL,
    AttemptResult,
    HostFixture,
    UpstreamStub,
    bare_refs,
    create_attempt_store,
    diff_snapshots,
    git,
    iter_enginery_containers,
    new_task_workspace,
    read_receipt,
    remove_attempt_store,
    snapshot_tree,
    violations_of,
)

REFUSAL_ERRNOS = {"EROFS", "EACCES", "EPERM"}
STANDARD_DEVICES = {
    "null",
    "zero",
    "full",
    "random",
    "urandom",
    "tty",
    "console",
    "ptmx",
    "pts",
    "shm",
    "mqueue",
    "fd",
    "stdin",
    "stdout",
    "stderr",
    "core",
}
ALLOWED_MOUNT_POINTS = {
    "/",
    "/proc",
    "/dev",
    "/dev/pts",
    "/dev/shm",
    "/dev/mqueue",
    "/sys",
    "/sys/fs/cgroup",
    "/etc/resolv.conf",
    "/etc/hostname",
    "/etc/hosts",
    "/workspace",
    "/out",
    "/tmp",
    "/home/worker",
    "/usr/sbin/docker-init",  # the engine's own --init binary
}
SYSTEM_PREFIXES = ("/proc/", "/sys/", "/dev/")

REQUIRED: dict[str, str] = {
    "a-read-protected-paths": (
        "Reads of the ledger, artifact store, config, broker/gateway tokens, installed engine, "
        "holdouts, ~/.ssh and gh credentials by host path are refused inside the runner"
    ),
    "a-read-filesystem-search": (
        "A filesystem search for the canary file names and contents finds nothing visible in the "
        "runner (a planted control file is found, so the search works)"
    ),
    "a-read-environment": (
        "The runner environment holds no canary value and no GH_/SSH_/token-shaped key"
    ),
    "a-read-proc-environ": (
        "No process environment or command line readable under /proc holds a canary or "
        "credential-shaped key"
    ),
    "b-write-image-paths": (
        "Writes to /etc, /usr, /opt/enginery (worker entry and omp policy overlay) are refused "
        "and the image files are unchanged"
    ),
    "b-write-root-and-host-paths": (
        "Writes to / and to host-looking absolute paths (home, ledger, token paths) are refused"
    ),
    "b-write-workspace-plants": (
        "Planting .git/hooks, conftest and sitecustomize in the workspace succeeds (positive "
        "control) while every protected host file is byte-identical afterwards"
    ),
    "c-escape-docker-socket": "No Docker socket or engine CLI is visible inside the runner",
    "c-escape-devices": "/dev holds only the standard nodes and host devices cannot be opened",
    "c-escape-host-mounts": "Host mount points (/Users, /host_mnt, /mnt, /home, ...) hold nothing",
    "c-escape-mountinfo": "/proc/self/mountinfo shows only the admitted mounts",
    "c-escape-syscalls": (
        "mount(), unshare(), ptrace(), chroot(), setuid(), mknod() and raw sockets are denied; "
        "no capability, no_new_privs set, seccomp filtering, non-root uid"
    ),
    "c-container-profile": (
        "The engine-reported container of every attempt matches the isolated profile "
        "(profile_violations is empty in the receipt)"
    ),
    "d-network-direct-refused": (
        "Direct connections to the stub gateway and broker (via host.docker.internal, "
        "host.lima.internal, the host-gateway and bridge-gateway addresses) are refused"
    ),
    "d-network-internet-and-dns-refused": (
        "Internet addresses (1.1.1.1, 8.8.8.8, the metadata address) and DNS are refused or "
        "unresolvable from the runner"
    ),
    "d-network-host-listeners-zero-connections": (
        "Host listeners standing in for the gateway and broker (loopback and 0.0.0.0) recorded "
        "zero connections from the runner"
    ),
    "e-git-push-refused": (
        "git push/ls-remote to the bare origin by path and URL, GitHub https/ssh/git URLs and "
        "credential helpers are refused"
    ),
    "e-scm-and-broker-network-refused": (
        "HTTPS calls to api.github.com and POSTs to the stub broker/gateway are refused; no gh, "
        "docker, sudo or hub binary exists"
    ),
    "e-credentials-unreadable": (
        "gh, ssh, git-credentials and netrc credentials cannot be read from inside the runner"
    ),
    "e-bare-origin-unchanged": (
        "The bare origin's refs are identical before and after the worker's push attempts"
    ),
    "e-operator-publication": (
        "After host-side intake verification of the worker's bundle, the operator-side "
        "GitBranchPublisher publishes exactly the verified revision (idempotently, "
        "expected-head constrained)"
    ),
    "f-relay-hardened-and-reachable": (
        "The real relay runs hardened and egress-only, and is attached to each model-access "
        "attempt's own internal network for that attempt's lifetime only; the worker on that "
        "network reaches only it"
    ),
    "f-relay-allowed-routes-forwarded": (
        "Allowed routes are forwarded exactly once with the injected bearer and the inbound "
        "Authorization and x-api-key stripped"
    ),
    "f-relay-bare-claude-rewrite": (
        "A bare claude-* id on /v1/messages reaches the upstream as anthropic/<id>"
    ),
    "f-relay-models-filtered": "GET /v1/models is filtered to the permitted providers",
    "f-relay-denied-zero-upstream": (
        "Denied providers, colliding bare ids, metadata routes, other APIs, smuggled model "
        "fields and method/path tricks are refused with zero requests at the upstream"
    ),
    "g-limits-memory": "A memory breach is OOM-killed (exit 137, engine oom event)",
    "g-limits-pids": "A fork bomb is capped at the pids limit (EAGAIN, pids.events max)",
    "g-limits-wall-time": (
        "The worker deadline kills the process group and the container is removed (143 or 124)"
    ),
    "g-limits-launcher-deadline": (
        "A worker that ignores termination is killed by the launcher's hard deadline (exit 124)"
    ),
    "g-limits-cpu": "CPU use is throttled to the configured limit",
    "h-hooks-not-executed-on-host": (
        "Planted hooks, conftest, core.sshCommand and attribute drivers never execute on the host "
        "through intake, validation or publication"
    ),
    "h-engine-install-unchanged": (
        "The installed engine directory and every protected host file are byte-identical after "
        "the hostile candidate was ingested"
    ),
    "h-validation-in-runner": (
        "Validation of the verified head runs inside the runner against a clean checkout, "
        "without the worker's uncommitted leftovers"
    ),
    "h-image-paths-read-only": (
        "In-container writes to /opt/enginery, site-packages and node_modules fail with EROFS"
    ),
    "i-profile-violations-flagged": (
        "profile_violations flags each deliberately weaker container the gate creates with raw "
        "docker, and is empty for the container the product's own arguments create"
    ),
    "i-admission-missing-image": "Admission holds a selection whose image id does not exist",
    "i-admission-wrong-recipe": "Admission holds an image whose recipe label differs",
    "i-admission-unreachable-engine": (
        "Admission holds when the engine is unreachable (expected outcome of this sub-check: held)"
    ),
    "i-admission-profile-drift": "Admission holds a selection bound to a different profile digest",
    "i-launcher-no-fallback": (
        "The launcher refuses (exit 125) a missing image, an unreachable engine and a "
        "model-access worker whose relay is not running, and never runs the command on the host"
    ),
    "z-profile-violations-all-receipts": (
        "Every runner receipt written by the gate has an empty profile_violations"
    ),
    "z-canary-integrity": (
        "No canary value appeared in any container output and no protected host file changed "
        "during any attempt"
    ),
    "z-cleanup-no-stray-containers": (
        "No enginery-* container, gate network or created image tag remains"
    ),
    "z-no-hosted-inference": "No hosted inference was performed",
}
REAL_GATEWAY: dict[str, str] = {
    "f-relay-real-gateway-models-and-denials": (
        "Through the relay, a real GET /v1/models succeeds (count only) and the denied routes are "
        "refused by the relay without reaching the gateway"
    ),
}


def required_assertions(options: Any) -> dict[str, str]:
    import factory_gate_review as review

    required = {**REQUIRED, **review.REQUIRED}
    if options.gateway_url is not None:
        required.update(REAL_GATEWAY)
    return required


def _record(
    gate: Gate,
    assertion_id: str,
    attempt: AttemptResult,
    *,
    passed: bool,
    observed: Any,
    expected: str,
    extra_observer: dict[str, Any] | None = None,
    extra_commands: list[str] | None = None,
) -> None:
    observer = {**(attempt.observer or {}), **(extra_observer or {})}
    observer["receipt"] = receipt_summary(attempt.receipt)
    observer["container_removed"] = attempt.container_gone
    observer["launcher_exit_code"] = attempt.exit_code
    gate.evidence.assertion(
        assertion_id,
        passed=passed,
        commands=[*gate.evidence_commands(attempt), *(extra_commands or [])],
        observed=observed,
        expected=expected,
        observer=observer,
        raw_artifacts=gate.raw_refs(attempt),
    )


def _clean(attempt: AttemptResult) -> bool:
    """The attempt ran under the launcher, wrote a clean receipt and changed nothing on the host."""
    observer = attempt.observer or {}
    return (
        attempt.exit_code == 0
        and attempt.obs is not None
        and not attempt.violations
        and attempt.container_gone
        and not observer.get("host_protected_changes")
    )


def _ops(attempt: AttemptResult, prefix: str) -> list[dict[str, Any]]:
    return [op for op in attempt.operations if op["id"].startswith(prefix)]


def _ops_ok(attempt: AttemptResult, prefix: str) -> bool:
    selected = _ops(attempt, prefix)
    return bool(selected) and all(op["ok"] for op in selected)


# --- setup and controls ----------------------------------------------------------------------


def _bridge_gateway(gate: Gate, network: str) -> str | None:
    completed = gate.engine_cli("network", "inspect", network)
    if completed.returncode != 0:
        return None
    try:
        document = json.loads(completed.stdout)
        configs = document[0].get("IPAM", {}).get("Config") or []
        return str(configs[0].get("Gateway")) or None
    except (ValueError, IndexError, KeyError, TypeError):
        return None


def step_setup(gate: Gate) -> dict[str, Any]:
    # A scratch internal network, only to learn the engine's bridge-gateway address (a host
    # address the workers must not reach); no worker is ever attached to it.
    created = gate.engine_cli("network", "create", "--internal", gate.internal_network)
    if created.returncode != 0:
        raise GateHoldError(f"the gate cannot create a scratch network: {created.stderr.strip()}")
    gate.track_network(gate.internal_network)
    return {"bridge_gateway": _bridge_gateway(gate, gate.internal_network)}


_CONTROL_SCRIPT = """
import json, socket, sys
spec = json.loads(sys.argv[1])
out = {"resolved": {}, "connects": {}}
for host in spec["hosts"]:
    try:
        out["resolved"][host] = socket.gethostbyname(host)
    except OSError as error:
        out["resolved"][host] = "unresolved:" + type(error).__name__
for host in spec["hosts"]:
    for scope, port in spec["ports"].items():
        key = host + ":" + scope
        try:
            conn = socket.create_connection((host, port), timeout=5)
            conn.sendall(b"GET / HTTP/1.0\\r\\n\\r\\n")
            conn.recv(64)
            conn.close()
            out["connects"][key] = "connected"
        except OSError as error:
            out["connects"][key] = type(error).__name__
print("GATE_CONTROL " + json.dumps(out))
"""


def step_controls(gate: Gate, setup: dict[str, Any]) -> dict[str, Any]:
    """An unhardened container on the default bridge proves the host listeners are reachable."""
    assert gate.stubs is not None
    name = f"enginery-gate-{gate.rid}-control"
    gate.track_container(name)
    ports = gate.stubs.ports("control")
    spec = {"hosts": ["host.docker.internal", "host.lima.internal"], "ports": ports}
    # A rootless engine resolves both host names itself (Lima maps them to the host); its
    # "host-gateway" is the bridge inside the engine's own network namespace, not the host.
    host_aliases: tuple[str, ...] = (
        ()
        if gate.engine_identity.kind == "rootless-docker"
        else (
            "--add-host",
            "host.docker.internal:host-gateway",
            "--add-host",
            "host.lima.internal:host-gateway",
        )
    )
    arguments = (
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        f"{GATE_LABEL}={gate.rid}",
        "--network",
        "bridge",
        *host_aliases,
        "--user",
        "65532:65532",
        gate.runner_image.image_id,
        "python3",
        "-c",
        _CONTROL_SCRIPT,
        json.dumps(spec),
    )
    completed = gate.engine_cli(*arguments, timeout=120)
    report: dict[str, Any] = {}
    for line in completed.stdout.splitlines():
        if line.startswith("GATE_CONTROL "):
            report = json.loads(line.removeprefix("GATE_CONTROL "))
    reached_any = any(
        outcome == "connected"
        for key, outcome in report.get("connects", {}).items()
        if key.endswith(":any")
    )
    reached_loopback = any(
        outcome == "connected"
        for key, outcome in report.get("connects", {}).items()
        if key.endswith(":loopback")
    )
    listener_any = gate.stubs.listeners["control-any"].count()
    gate.evidence.control(
        "control-unhardened-container-reaches-host-listener",
        description=(
            "A container with default networking reaches a host 0.0.0.0 listener, so the zero "
            "connection counts recorded for the runner mean the runner could not reach it"
        ),
        passed=completed.returncode == 0 and reached_any and listener_any >= 1,
        observed={
            "connects": report.get("connects"),
            "resolved": report.get("resolved"),
            "control_listener_connections": gate.stubs.counts(),
            "docker_exit": completed.returncode,
        },
        expected="the unhardened control connects to the host listener and the host logs it",
        commands=[
            gate.docker_command(*arguments[:-1], "<spec>").replace(_CONTROL_SCRIPT, "<script>")
        ],
    )
    gate.evidence.details["informational"] = {
        "host_loopback_listener_reachable_from_unhardened_container": reached_loopback,
        "control_resolved_addresses": report.get("resolved"),
    }
    addresses = sorted(
        {
            value
            for value in (report.get("resolved") or {}).values()
            if isinstance(value, str) and value[:1].isdigit()
        }
    )
    return {"host_addresses": addresses, "control": report}


# --- (a) read --------------------------------------------------------------------------------


def step_read(gate: Gate, name: str = "read") -> AttemptResult:
    fixture = gate.fixture
    assert fixture is not None
    workspace = new_task_workspace(fixture, name)
    control_name = f".gate-control-{gate.rid}.txt"
    control_value = f"gate-positive-control-{secrets.token_hex(8)}"
    (workspace / control_name).write_text(control_value + "\n", encoding="utf-8")
    params = {
        "protected_paths": [*fixture.protected_paths, *fixture.host_looking_paths],
        "canary_names": fixture.canary_names(),
        "canary_values": fixture.canary_values(),
        "control_name": control_name,
        "control_value": control_value,
        "allowed_reads": ["/workspace/README.md", "/etc/os-release"],
    }
    attempt = gate.attempt(name, "read", params, workspace=workspace)
    ops = attempt.operations
    protected_exist = all(Path(path).exists() for path in fixture.protected_paths)
    search = (attempt.obs or {}).get("filename_search", {})
    contents = (attempt.obs or {}).get("content_search", {})
    environment = (attempt.obs or {}).get("environment", {})
    proc = (attempt.obs or {}).get("proc_environ", {})
    control_reads = _ops(attempt, "control-read:")
    clean = _clean(attempt)
    refused = [op for op in ops if op["id"].startswith(("read:", "stat:"))]
    _record(
        gate,
        "a-read-protected-paths",
        attempt,
        passed=clean
        and protected_exist
        and bool(refused)
        and all(op["ok"] for op in refused)
        and bool(control_reads)
        and all(op["ok"] for op in control_reads),
        observed={
            "operations": summarize_operations(refused),
            "positive_control_reads": summarize_operations(control_reads),
            "failing": failing_operations(ops),
        },
        expected="every read/stat of a host path is refused; reads of image/workspace files work",
        extra_observer={"host_targets_exist": protected_exist},
    )
    _record(
        gate,
        "a-read-filesystem-search",
        attempt,
        passed=clean
        and not search.get("foreign_matches")
        and not search.get("find_foreign_matches")
        and not contents.get("foreign_hits")
        and bool(search.get("control_matches"))
        and bool(contents.get("control_hits"))
        and search.get("find_exit") in (0, 1),
        observed={
            "walked_directories": search.get("walked_directories"),
            "walked_files": search.get("walked_files"),
            "scanned_content_files": contents.get("scanned_files"),
            "filename_matches_of_canaries": search.get("foreign_matches"),
            "find_matches_of_canaries": search.get("find_foreign_matches"),
            "content_hits_of_canaries": contents.get("foreign_hits"),
            "positive_control_found_by_name": search.get("control_matches"),
            "positive_control_found_by_content": contents.get("control_hits"),
            "find_command": search.get("find_command"),
        },
        expected="no canary name or value found; the planted positive control is found",
        extra_commands=[search.get("find_command", "find /")],
    )
    _record(
        gate,
        "a-read-environment",
        attempt,
        passed=clean
        and not environment.get("credential_shaped_keys")
        and not environment.get("canary_values_present")
        and "PATH" in environment.get("keys", []),
        observed={
            "environment_keys": environment.get("keys"),
            "credential_shaped_keys": environment.get("credential_shaped_keys"),
            "canary_values_present": environment.get("canary_values_present"),
        },
        expected="no canary value and no credential-shaped key in the environment",
    )
    _record(
        gate,
        "a-read-proc-environ",
        attempt,
        passed=clean
        and not proc.get("canary_hits")
        and not proc.get("credential_shaped_keys")
        and int(proc.get("environ_readable", 0)) >= 1,
        observed=proc,
        expected="no /proc/*/environ or cmdline holds a canary or credential-shaped key",
        extra_commands=[
            "[in container] read /proc/<pid>/environ and /proc/<pid>/cmdline for all pids"
        ],
    )
    return attempt


# --- (b) write -------------------------------------------------------------------------------


def _hook(fixture: HostFixture, name: str) -> str:
    target = shlex.quote(str(fixture.markers / f"executed-{name}"))
    return f"#!/bin/sh\nprintf 'executed\\n' >> {target}\n"


def _marker_scripts(fixture: HostFixture) -> dict[str, Path]:
    """Host scripts that leave an ``executed-<name>`` marker only if run on the host."""
    scripts: dict[str, Path] = {}
    for name in (
        "ssh",
        "fsmonitor",
        "textconv",
        "smudge",
        "credential",
        "pager",
        "editor",
        "packhook",
        "sitecustomize",
        "engine-install",
    ):
        path = fixture.markers / f"{name}.sh"
        body = f"printf 'executed\\n' >> {shlex.quote(str(fixture.markers / f'executed-{name}'))}\n"
        if name == "engine-install":
            engine_file = fixture.engine_dir / "enginery" / "__init__.py"
            body += f"printf 'x' >> {shlex.quote(str(engine_file))}\n"
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        scripts[name] = path
    return scripts


def _executed_markers(fixture: HostFixture) -> list[str]:
    return sorted(path.name for path in fixture.markers.glob("executed-*"))


def step_write(gate: Gate) -> AttemptResult:
    fixture = gate.fixture
    assert fixture is not None
    image_targets = [
        ("create", "/etc/enginery-gate-write"),
        ("append", "/etc/passwd"),
        ("truncate", "/etc/os-release"),
        ("create", "/usr/enginery-gate-write"),
        ("create", "/usr/local/bin/enginery-gate-write"),
        ("append", "/usr/bin/git"),
        ("create", "/opt/enginery/planted.py"),
        ("append", "/opt/enginery/worker_entry.py"),
        ("truncate", "/opt/enginery/worker_entry.py"),
        ("chmod", "/opt/enginery/worker_entry.py"),
        ("rename", "/opt/enginery/worker_entry.py"),
        ("unlink", "/opt/enginery/worker_entry.py"),
        ("append", "/opt/enginery/omp-isolated-policy.yml"),
        ("truncate", "/opt/enginery/omp-isolated-policy.yml"),
        ("unlink", "/opt/enginery/omp-isolated-policy.yml"),
        ("create", "/var/lib/enginery-gate-write"),
        ("create", "/root/enginery-gate-write"),
        ("create", "/lib/enginery-gate-write"),
    ]
    root_targets = [
        ("create", "/enginery-gate-root"),
        ("mkdirs", "/enginery-gate-dir"),
        ("symlink", "/enginery-gate-link"),
        ("link", "/enginery-gate-hardlink"),
    ]
    host_targets: list[tuple[str, str]] = []
    for path in fixture.host_looking_paths:
        host_targets += [("mkdirs", str(Path(path).parent)), ("create", path)]
    for path in fixture.protected_paths[:6]:
        host_targets += [("append", path), ("unlink", path)]
    refused = (
        [{"id": f"img:{a}:{p}", "action": a, "path": p} for a, p in image_targets]
        + [{"id": f"root:{a}:{p}", "action": a, "path": p} for a, p in root_targets]
        + [{"id": f"host:{a}:{p}", "action": a, "path": p} for a, p in host_targets]
    )
    allowed = [
        {"id": f"control:create:{p}", "action": "create", "path": p}
        for p in (
            "/workspace/gate-positive.txt",
            "/out/gate-positive-out.txt",
            "/tmp/gate-positive",
            "/home/worker/gate-positive",
        )
    ]
    plants = [
        {
            "id": f"plant:{path}",
            "path": path,
            "content": _hook(fixture, Path(path).name),
            "mode": 0o755,
        }
        for path in (
            "/workspace/.git/hooks/pre-commit",
            "/workspace/.git/hooks/post-commit",
            "/workspace/.git/hooks/pre-push",
            "/workspace/.git/hooks/post-checkout",
            "/workspace/.git/hooks/reference-transaction",
        )
    ]
    plants += [
        {
            "id": "plant:/workspace/conftest.py",
            "path": "/workspace/conftest.py",
            "content": (
                "import pathlib\n"
                f"pathlib.Path({str(fixture.markers / 'executed-conftest')!r}).write_text('x')\n"
            ),
            "mode": 0o644,
        },
        {
            "id": "plant:/workspace/sitecustomize.py",
            "path": "/workspace/sitecustomize.py",
            "content": "# planted by the gate\n",
            "mode": 0o644,
        },
    ]
    params = {
        "refused": refused,
        "allowed": allowed,
        "plants": plants,
        "watched_files": [
            "/opt/enginery/worker_entry.py",
            "/opt/enginery/omp-isolated-policy.yml",
            "/etc/passwd",
            "/usr/bin/git",
        ],
    }
    attempt = gate.attempt("write", "write", params)
    clean = _clean(attempt)
    image_ops = _ops(attempt, "img:")
    markers = _executed_markers(fixture)
    _record(
        gate,
        "b-write-image-paths",
        attempt,
        passed=clean
        and bool(image_ops)
        and all(op["ok"] and op["error"] in REFUSAL_ERRNOS for op in image_ops)
        and (attempt.obs or {}).get("watched_unchanged") is True,
        observed={
            "operations": summarize_operations(image_ops),
            "watched_files_unchanged": (attempt.obs or {}).get("watched_unchanged"),
        },
        expected="every write to an image path fails with EROFS/EACCES/EPERM; hashes unchanged",
    )
    root_and_host = [*_ops(attempt, "root:"), *_ops(attempt, "host:")]
    _record(
        gate,
        "b-write-root-and-host-paths",
        attempt,
        passed=clean and bool(root_and_host) and all(op["ok"] for op in root_and_host),
        observed={"operations": summarize_operations(root_and_host)},
        expected="every write to / or a host-looking path is refused",
    )
    controls = _ops(attempt, "control:")
    planted = _ops(attempt, "plant:")
    hook_files_on_host = [
        str(path.relative_to(attempt.workspace))
        for path in (attempt.workspace / ".git" / "hooks").glob("*")
    ]
    _record(
        gate,
        "b-write-workspace-plants",
        attempt,
        passed=clean
        and len(controls) == 4
        and all(op["ok"] for op in controls)
        and bool(planted)
        and all(op["ok"] for op in planted)
        and not markers
        and len(hook_files_on_host) == 5,
        observed={
            "positive_controls": summarize_operations(controls),
            "plants": summarize_operations(planted),
            "planted_hook_files_seen_on_host_workspace": hook_files_on_host,
            "markers_executed_on_host": markers,
        },
        expected=(
            "workspace/out/tmp/home writes work and the planted hooks exist, but nothing executed "
            "and no protected file changed"
        ),
        extra_observer={"executed_markers": markers},
    )
    gate.effects["workspace_plants_written"] = len(planted)
    return attempt


# --- (c) escape ------------------------------------------------------------------------------


def step_escape(gate: Gate) -> AttemptResult:
    params = {
        "socket_paths": [
            "/var/run/docker.sock",
            "/run/docker.sock",
            "/var/run/docker",
            "/run/containerd/containerd.sock",
            "/var/run/containerd/containerd.sock",
            "/run/podman/podman.sock",
            "/run/user/1000/docker.sock",
            "/run/user/65532/docker.sock",
            "/run/host-services/ssh-auth.sock",
            "/run/host-services/docker.proxy.sock",
            "/docker.sock",
        ],
        "host_mount_paths": [
            {"path": "/Users"},
            {"path": "/host_mnt"},
            {"path": "/Volumes"},
            {"path": "/private"},
            {"path": "/mnt"},
            {"path": "/mnt/host"},
            {"path": "/mnt/c"},
            {"path": "/media"},
            {"path": "/host"},
            {"path": "/run/host-services"},
            {"path": "/home", "allowed": ["worker", "node"]},
        ],
        "device_paths": [
            "/dev/sda",
            "/dev/vda",
            "/dev/nvme0n1",
            "/dev/mem",
            "/dev/kmem",
            "/dev/kvm",
            "/dev/fuse",
            "/dev/net/tun",
            "/dev/loop0",
            "/dev/disk",
            "/dev/docker.sock",
        ],
    }
    attempt = gate.attempt("escape", "escape", params)
    obs = attempt.obs or {}
    identity = obs.get("identity", {})
    clean = _clean(attempt)
    sockets = _ops(attempt, "socket-path:")
    dev_entries = set(obs.get("dev_entries", []))
    _record(
        gate,
        "c-escape-docker-socket",
        attempt,
        passed=clean
        and bool(sockets)
        and all(op["ok"] for op in sockets)
        and not obs.get("sockets_found")
        and obs.get("docker_cli") == "",
        observed={
            "operations": summarize_operations(sockets),
            "unix_sockets_visible": obs.get("sockets_found"),
            "docker_cli_path": obs.get("docker_cli"),
        },
        expected="no docker/containerd socket path exists, no unix socket visible, no docker CLI",
    )
    devices = _ops(attempt, "device:")
    receipt = attempt.receipt
    _record(
        gate,
        "c-escape-devices",
        attempt,
        passed=clean
        and dev_entries <= STANDARD_DEVICES
        and bool(devices)
        and all(op["ok"] for op in devices)
        and receipt is not None
        and receipt.effective.devices == (),
        observed={
            "dev_entries": sorted(dev_entries),
            "non_standard_entries": sorted(dev_entries - STANDARD_DEVICES),
            "operations": summarize_operations(devices),
            "engine_reported_devices": [] if receipt is None else list(receipt.effective.devices),
        },
        expected="/dev lists only standard nodes; no host device opens; no device mapped",
    )
    mounts = _ops(attempt, "host-mount:")
    _record(
        gate,
        "c-escape-host-mounts",
        attempt,
        passed=clean and bool(mounts) and all(op["ok"] for op in mounts),
        observed={"operations": summarize_operations(mounts)},
        expected="each host mount point is absent or empty (/home: only the image's own users)",
    )
    mountinfo = obs.get("mountinfo", [])
    unexpected = [
        entry["mount_point"]
        for entry in mountinfo
        if entry["mount_point"] not in ALLOWED_MOUNT_POINTS
        and not entry["mount_point"].startswith(SYSTEM_PREFIXES)
    ]
    admitted = sorted(
        entry["mount_point"]
        for entry in mountinfo
        if entry["mount_point"] in (WORKSPACE_MOUNT, OUT_MOUNT)
    )
    engine_volumes = (
        []
        if receipt is None
        else sorted(dest for kind, dest, _ in receipt.effective.mounts if kind == "volume")
    )
    engine_binds = (
        []
        if receipt is None
        else sorted(dest for kind, dest, _ in receipt.effective.mounts if kind == "bind")
    )
    _record(
        gate,
        "c-escape-mountinfo",
        attempt,
        passed=clean
        and not unexpected
        and admitted == [OUT_MOUNT, WORKSPACE_MOUNT]
        and engine_volumes == [OUT_MOUNT, WORKSPACE_MOUNT]
        and not engine_binds,
        observed={
            "mount_points": sorted(entry["mount_point"] for entry in mountinfo),
            "unexpected_mount_points": unexpected,
            "admitted_mounts_seen": admitted,
            "volume_mounts_reported_by_engine": engine_volumes,
            "bind_mounts_reported_by_engine": engine_binds,
        },
        expected="only the admitted mounts (+ docker's own system mounts) are visible",
        extra_commands=["[in container] read /proc/self/mountinfo"],
    )
    syscalls = [
        op
        for op in attempt.operations
        if op["id"]
        in {
            "mount-tmpfs",
            "mount-bind-root",
            "mount-remount-rootfs-rw",
            "unshare-mount-ns",
            "unshare-user-ns",
            "unshare-pid-ns",
            "ptrace-foreign-pid",
            "chroot",
            "setuid-root",
            "sethostname",
            "mknod-disk",
            "raw-socket",
            "sysrq-trigger",
            "kernel-hostname-write",
            "cgroup-release-agent",
            "proc-visible-processes",
        }
    ]
    zero = "0000000000000000"
    _record(
        gate,
        "c-escape-syscalls",
        attempt,
        passed=clean
        and len(syscalls) == 16
        and all(op["ok"] for op in syscalls)
        and identity.get("uid") == 65532
        and identity.get("cap_eff") == zero
        and identity.get("cap_prm") == zero
        and identity.get("cap_amb") == zero
        and identity.get("no_new_privs") == "1"
        and identity.get("seccomp") == "2",
        observed={
            "operations": summarize_operations(syscalls),
            "identity": identity,
        },
        expected="every privileged syscall is denied; uid 65532, no caps, NoNewPrivs 1, Seccomp 2",
    )
    return attempt


# --- (d) network -----------------------------------------------------------------------------


def step_network(gate: Gate, setup: dict[str, Any], controls: dict[str, Any]) -> AttemptResult:
    assert gate.stubs is not None
    hosts = ["host.docker.internal", "host.lima.internal", *controls["host_addresses"]]
    bridge = setup.get("bridge_gateway")
    if bridge:
        hosts.append(bridge)
    targets: list[dict[str, Any]] = []
    for host in dict.fromkeys(hosts):
        for service in ("gateway", "broker"):
            for scope, port in gate.stubs.ports(service).items():
                targets.append(
                    {"id": f"stub-{service}-{scope}@{host}:{port}", "host": host, "port": port}
                )
        for port in (80, 443, 2375):
            targets.append({"id": f"stub-host-port@{host}:{port}", "host": host, "port": port})
    for host, port in (
        ("1.1.1.1", 443),
        ("1.1.1.1", 80),
        ("8.8.8.8", 53),
        ("169.254.169.254", 80),
        ("example.com", 443),
        ("api.github.com", 443),
    ):
        targets.append({"id": f"internet-{host}:{port}", "host": host, "port": port})
    stub_ports = gate.stubs.all_ports()
    params = {
        "targets": targets,
        "gateway_ports": [*stub_ports, 80, 443],
        "dns_names": ["example.com", "api.github.com", "github.com", "pypi.org"],
        "udp_dns_servers": ["1.1.1.1", "8.8.8.8"],
        "http_posts": [
            {
                "id": f"post-stub-{service}@{host}:{port}",
                "host": host,
                "port": port,
                "path": "/v1/messages" if service == "gateway" else "/v1/actions/merge",
            }
            for host in ("host.docker.internal", "host.lima.internal")
            for service in ("gateway", "broker")
            for port in gate.stubs.ports(service).values()
        ],
        "timeout": 3.0,
    }
    before = gate.stubs.counts()
    attempt = gate.attempt("network", "network", params)
    after = gate.stubs.counts()
    delta = {key: after[key] - before.get(key, 0) for key in after}
    forbidden_delta = sum(v for k, v in delta.items() if k.startswith(("gateway-", "broker-")))
    ops = attempt.operations
    clean = _clean(attempt)
    local_control = [op for op in ops if op["id"] == "control:loopback-connect"]
    gate.evidence.control(
        "control-worker-network-code-executes",
        description="The attempt program really opens sockets inside the runner container",
        passed=bool(local_control) and all(op["ok"] for op in local_control),
        observed=summarize_operations(local_control),
        expected="a connect to a listener opened inside the container succeeds",
        commands=gate.evidence_commands(attempt)[:1],
    )
    stub_ops = [
        op for op in ops if op["id"].startswith(("tcp:stub-",)) or op["id"].startswith("post-stub-")
    ]
    internet_ops = [
        op
        for op in ops
        if op["id"].startswith(("tcp:internet-", "dns:", "udp-dns:", "tcp:default-gateway"))
    ]
    _record(
        gate,
        "d-network-direct-refused",
        attempt,
        passed=clean and bool(stub_ops) and all(op["ok"] for op in stub_ops),
        observed={
            "targets": len(stub_ops),
            "operations": summarize_operations(stub_ops),
            "default_gateway_in_container": (attempt.obs or {}).get("default_gateway"),
            "route": (attempt.obs or {}).get("route"),
        },
        expected="every direct connection to the stub gateway/broker is refused or unreachable",
    )
    _record(
        gate,
        "d-network-internet-and-dns-refused",
        attempt,
        passed=clean and bool(internet_ops) and all(op["ok"] for op in internet_ops),
        observed={
            "operations": summarize_operations(internet_ops),
            "resolv_conf": (attempt.obs or {}).get("resolv_conf"),
        },
        expected="internet connects fail; external names do not resolve; UDP DNS fails",
    )
    _record(
        gate,
        "d-network-host-listeners-zero-connections",
        attempt,
        passed=clean and forbidden_delta == 0,
        observed={
            "connections_during_attempt": delta,
            "listeners": {
                key: {
                    "gateway-loopback": "stub gateway on 127.0.0.1",
                    "gateway-any": "stub gateway on 0.0.0.0",
                    "broker-loopback": "stub broker on 127.0.0.1",
                    "broker-any": "stub broker on 0.0.0.0",
                }.get(key, key)
                for key in sorted(after)
            },
        },
        expected="zero connections recorded on the gateway and broker listeners",
        extra_observer={
            "listener_connection_log": gate.stubs.listeners["gateway-any"].connections()
        },
    )
    return attempt


# --- (e) git, GitHub and broker --------------------------------------------------------------


def step_git(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None and gate.stubs is not None
    branch = "enginery/gate-task"
    origin = fixture.origin
    workspace = new_task_workspace(fixture, "git")
    refs_before = bare_refs(origin)
    stub_before = gate.stubs.counts()
    attack_targets = [
        str(origin),
        f"file://{origin}",
        "../origin.git",
        "https://github.com/enginery-gate/none.git",
        "https://api.github.com/repos/enginery-gate/none/git/refs",
        "ssh://git@github.com/enginery-gate/none.git",
        "git@github.com:enginery-gate/none.git",
        "git://github.com/enginery-gate/none.git",
    ]
    home = fixture.home_dir
    params = {
        "branch": branch,
        "change_text": f"worker change {gate.rid}\n",
        "push_targets": attack_targets,
        "other_remote_targets": [str(origin), f"file://{origin}"],
        "credential_paths": [
            "~/.ssh/id_ed25519",
            "~/.ssh/config",
            "~/.config/gh/hosts.yml",
            "~/.git-credentials",
            "~/.netrc",
            "~/.gitconfig",
            "/root/.ssh/id_ed25519",
            str(home / ".ssh" / "id_ed25519"),
            str(home / ".config" / "gh" / "hosts.yml"),
            str(home / ".git-credentials"),
        ],
        "https_targets": [
            {
                "id": "https-get-api.github.com/user",
                "method": "GET",
                "host": "api.github.com",
                "path": "/user",
            },
            {
                "id": "https-post-api.github.com/refs",
                "method": "POST",
                "host": "api.github.com",
                "path": "/repos/enginery-gate/none/git/refs",
            },
            {"id": "https-get-github.com", "method": "GET", "host": "github.com", "path": "/"},
        ],
        "broker_posts": [
            {
                "id": f"post-{service}@{host}:{port}",
                "host": host,
                "port": port,
                "path": "/v1/messages" if service == "gateway" else "/v1/actions/merge",
            }
            for host in ("host.docker.internal", "host.lima.internal")
            for service in ("gateway", "broker")
            for port in gate.stubs.ports(service).values()
        ],
    }
    protected_before = gate.protected_snapshot()
    attempt = gate.attempt("git", "git", params, workspace=workspace, bundle_branch=branch)
    refs_after_attempt = bare_refs(origin)
    delta = {key: count - stub_before.get(key, 0) for key, count in gate.stubs.counts().items()}
    clean = _clean(attempt)
    ops = attempt.operations
    pushes = [op for op in ops if op["id"].startswith(("git-push:", "git-remote-op:"))]
    positive = (attempt.obs or {}).get("positive", {})
    _record(
        gate,
        "e-git-push-refused",
        attempt,
        passed=clean and len(pushes) == len(attack_targets) + 2 and all(op["ok"] for op in pushes),
        observed={"operations": summarize_operations(pushes)},
        expected="every git push / ls-remote to the origin path, URL or GitHub exits non-zero",
    )
    network_ops = [op for op in ops if op["id"].startswith(("https-", "post-"))]
    absent = [
        op
        for op in ops
        if op["id"] in {f"which:{name}" for name in ("gh", "hub", "docker", "sudo", "ssh-agent")}
    ]
    informational = [op for op in ops if op["id"] in {"which:ssh", "which:curl", "which:wget"}]
    forbidden_delta = sum(v for k, v in delta.items() if k.startswith(("gateway-", "broker-")))
    _record(
        gate,
        "e-scm-and-broker-network-refused",
        attempt,
        passed=clean
        and bool(network_ops)
        and all(op["ok"] for op in network_ops)
        and len(absent) == 5
        and all(op["ok"] for op in absent)
        and forbidden_delta == 0,
        observed={
            "operations": summarize_operations(network_ops),
            "binaries_required_absent": summarize_operations(absent),
            "binaries_informational": [
                {"id": op["id"], "outcome": op["outcome"], "path": op.get("detail")}
                for op in informational
            ],
            "stub_connections_during_attempt": delta,
        },
        expected="HTTPS to GitHub and POSTs to broker/gateway fail; no stub connection; no gh",
    )
    creds = [op for op in ops if op["id"].startswith("cred-read:")]
    fill = [op for op in ops if op["id"] == "git-credential-fill"]
    global_config = str((attempt.obs or {}).get("git_global_config", ""))
    _record(
        gate,
        "e-credentials-unreadable",
        attempt,
        passed=clean
        and bool(creds)
        and all(op["ok"] for op in creds)
        and all(op["ok"] for op in fill)
        and "credential" not in global_config.lower(),
        observed={
            "operations": summarize_operations([*creds, *fill]),
            "git_global_config": global_config,
        },
        expected="no credential file readable, no credential helper, git credential fill fails",
    )
    _record(
        gate,
        "e-bare-origin-unchanged",
        attempt,
        passed=clean and refs_before == refs_after_attempt and "refs/heads/main" in refs_before,
        observed={"refs_before": refs_before, "refs_after_worker_attempts": refs_after_attempt},
        expected="identical refs",
        extra_observer={
            "host_protected_changes": gate.protected_changes(
                protected_before, gate.protected_snapshot()
            )
        },
        extra_commands=[
            f"git --git-dir {origin.name} for-each-ref (before and after the worker ran)"
        ],
    )
    gate.effects["origin_ref_changes_by_worker"] = len(
        set(refs_after_attempt.items()) ^ set(refs_before.items())
    )
    _publish_verified(gate, attempt, workspace, branch, refs_before, positive.get("head"))


def _publish_verified(
    gate: Gate,
    attempt: AttemptResult,
    workspace: Path,
    branch: str,
    refs_before: dict[str, str],
    worker_head: str | None,
) -> None:
    fixture = gate.fixture
    assert fixture is not None
    observed: dict[str, Any] = {"worker_reported_head": worker_head}
    passed = False
    commands = [
        "intake_branch_bundle(repository_path=<host repo>, workspace_path=<task workspace>, "
        f"bundle_path=<out>/branch.bundle, head_branch={branch}, base_revision=<base>)",
        f"verify_intake_branch(intake_path=<workspace>.intake.git, head_branch={branch}, "
        "base_revision=<base>)",
        "GitBranchPublisher(repository_path=<host repo>, source_path=<intake repo>).publish(...)",
        "GitBranchPublisher.publish(...) again (idempotence)",
        "GitBranchPublisher.publish(<absent branch>, expected_remote_revision=<wrong>)",
    ]
    try:
        bundle = attempt.out / "branch.bundle"
        observed["bundle_exists"] = bundle.is_file()
        fetched = intake_branch_bundle(
            repository_path=fixture.repo,
            workspace_path=workspace,
            bundle_path=bundle,
            head_branch=branch,
            base_revision=fixture.base_revision,
        )
        verified = verify_intake_branch(
            intake_path=intake_path_for(workspace),
            head_branch=branch,
            base_revision=fixture.base_revision,
        )
        observed["intake_head"] = fetched
        observed["verified_head"] = verified
        publisher = GitBranchPublisher(
            repository_path=fixture.repo, source_path=intake_path_for(workspace)
        )
        request = BranchPublicationRequest(
            repository_id="gate-fixture",
            branch=branch,
            head_revision=verified,
            expected_remote_revision=None,
            operation_id=OperationId(f"gate-publish-{gate.rid}"),
        )
        published = publisher.publish(request)
        refs_after = bare_refs(fixture.origin)
        again = publisher.publish(request)
        refs_again = bare_refs(fixture.origin)
        conflict: str | None = None
        try:
            publisher.publish(
                dataclasses.replace(
                    request, branch="enginery/gate-conflict", expected_remote_revision="0" * 40
                )
            )
        except ExternalConflictError as error:
            conflict = type(error).__name__
        refs_conflict = bare_refs(fixture.origin)
        expected_after = {**refs_before, f"refs/heads/{branch}": verified}
        observed.update(
            published_revision=published.revision,
            idempotent_second_publish=again.revision,
            origin_refs_after_publication=refs_after,
            origin_refs_after_second_publish=refs_again,
            wrong_expected_head_refused_as=conflict,
            origin_branch_object=git(
                ["--git-dir", str(fixture.origin), "rev-parse", f"refs/heads/{branch}"]
            ).stdout.strip(),
        )
        passed = (
            worker_head is not None
            and verified == worker_head
            and published.revision == verified
            and refs_after == expected_after
            and again.revision == verified
            and refs_again == expected_after
            and conflict == "ExternalConflictError"
            and refs_conflict == expected_after
            and observed["origin_branch_object"] == verified
        )
    except Exception as error:
        observed["error"] = f"{type(error).__name__}: {error}"
    gate.evidence.assertion(
        "e-operator-publication",
        passed=passed,
        commands=commands,
        observed=observed,
        expected=(
            "the revision the worker committed (observed from inside the runner) equals the host-"
            "verified head and is the only ref added to the bare origin"
        ),
        observer={"origin_refs_before": refs_before},
        raw_artifacts=gate.raw_refs(attempt),
    )


# --- (f) relay -------------------------------------------------------------------------------

_MESSAGE_BODY = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 8}


def _case(
    case_id: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | str | None = None,
    forward: bool,
    statuses: tuple[int, ...],
    headers: dict[str, str] | None = None,
    chunked: bool = False,
    raw_body: bool = False,
) -> dict[str, Any]:
    marker = f"gatecase-{case_id}"
    if isinstance(body, dict):
        encoded: str | None = json.dumps({**body, "gate_case": marker})
    elif body is not None and not raw_body:
        encoded = body
    else:
        encoded = body
    if isinstance(body, str) and raw_body:
        encoded = body.replace("__MARKER__", marker)
    separator = "&" if "?" in path else "?"
    full_path = path if encoded is not None else f"{path}{separator}case={marker}"
    return {
        "id": case_id,
        "method": method,
        "path": full_path,
        "body": encoded,
        "headers": headers or {},
        "chunked": chunked,
        "forward": forward,
        "statuses": list(statuses),
        "marker": marker,
    }


def relay_cases(gate: Gate) -> list[dict[str, Any]]:
    inbound = {
        "Authorization": f"Bearer worker-attempt-{gate.rid}",
        "X-Api-Key": f"worker-attempt-{gate.rid}",
        "Cookie": f"session=worker-attempt-{gate.rid}",
    }
    messages = {**_MESSAGE_BODY}
    cases = [
        _case("A1-healthz", "GET", "/healthz", forward=False, statuses=(200,)),
        _case("A2-models", "GET", "/v1/models", forward=True, statuses=(200,), headers=inbound),
        _case(
            "A3-messages",
            "POST",
            "/v1/messages",
            body={**messages, "model": "anthropic/claude-gate-1"},
            forward=True,
            statuses=(200,),
            headers=inbound,
        ),
        _case(
            "A4-messages-bare-claude",
            "POST",
            "/v1/messages",
            body={**messages, "model": "claude-gate-bare"},
            forward=True,
            statuses=(200,),
            headers=inbound,
        ),
        _case(
            "A5-pi-stream-anthropic",
            "POST",
            "/v1/pi/stream",
            body={"modelId": "anthropic/claude-gate-2", "context": {"messages": []}},
            forward=True,
            statuses=(200,),
            headers=inbound,
        ),
        _case(
            "A6-pi-stream-codex",
            "POST",
            "/v1/pi/stream",
            body={"modelId": "openai-codex/gpt-gate", "context": {"messages": []}},
            forward=True,
            statuses=(200,),
        ),
    ]
    denied: list[tuple[str, str, str, Any, tuple[int, ...]]] = [
        (
            "D1-antigravity",
            "POST",
            "/v1/pi/stream",
            {"modelId": "google-antigravity/gemini-gate"},
            (403,),
        ),
        ("D2-bare-colliding-pi", "POST", "/v1/pi/stream", {"modelId": "claude-gate-bare"}, (403,)),
        ("D3-openai-pi", "POST", "/v1/pi/stream", {"modelId": "openai/gpt-4o"}, (403,)),
        (
            "D4-antigravity-messages",
            "POST",
            "/v1/messages",
            {**_MESSAGE_BODY, "model": "google-antigravity/claude-gate"},
            (403,),
        ),
        (
            "D5-openai-messages",
            "POST",
            "/v1/messages",
            {**_MESSAGE_BODY, "model": "openai/gpt-gate"},
            (403,),
        ),
        ("D6-credentials-check-post", "POST", "/v1/credentials/check", {"x": 1}, (403,)),
        ("D7-credentials-check-get", "GET", "/v1/credentials/check", None, (403,)),
        ("D8-usage-get", "GET", "/v1/usage", None, (403,)),
        ("D9-usage-post", "POST", "/v1/usage", {"x": 1}, (403,)),
        (
            "D10-chat-completions",
            "POST",
            "/v1/chat/completions",
            {"model": "anthropic/claude-gate-1"},
            (404,),
        ),
        ("D11-embeddings", "POST", "/v1/embeddings", {"model": "anthropic/claude-gate-1"}, (404,)),
        ("D12-responses", "POST", "/v1/responses", {"model": "anthropic/claude-gate-1"}, (404,)),
        (
            "D13-count-tokens",
            "POST",
            "/v1/messages/count_tokens",
            {"model": "anthropic/claude-gate-1"},
            (404,),
        ),
        ("D14-root", "GET", "/", None, (404,)),
        ("D15-pi-models", "GET", "/v1/pi/models", None, (404,)),
        (
            "D16-smuggle-model-object",
            "POST",
            "/v1/pi/stream",
            {"model": {"id": "google-antigravity/gemini-gate"}},
            (403,),
        ),
        (
            "D17-smuggle-leading-space",
            "POST",
            "/v1/pi/stream",
            {"modelId": " anthropic/claude-gate-1"},
            (403,),
        ),
        (
            "D18-smuggle-trailing-newline",
            "POST",
            "/v1/pi/stream",
            {"modelId": "anthropic/claude-gate-1\n"},
            (403,),
        ),
        (
            "D19-smuggle-zero-width",
            "POST",
            "/v1/pi/stream",
            {"modelId": "anthropic/claude-gate-1\u200b"},
            (403,),
        ),
        (
            "D20-smuggle-fullwidth",
            "POST",
            "/v1/pi/stream",
            {"modelId": "\uff41nthropic/claude-gate-1"},
            (403,),
        ),
        (
            "D21-smuggle-cyrillic",
            "POST",
            "/v1/pi/stream",
            {"modelId": "\u0430nthropic/claude-gate-1"},
            (403,),
        ),
        (
            "D22-smuggle-list",
            "POST",
            "/v1/pi/stream",
            {"model": ["anthropic/claude-gate-1"]},
            (403,),
        ),
        (
            "D23-smuggle-empty-modelid-fallback",
            "POST",
            "/v1/pi/stream",
            {"modelId": "", "model": "google-antigravity/gemini-gate"},
            (403,),
        ),
        (
            "D24-uppercase-provider",
            "POST",
            "/v1/pi/stream",
            {"modelId": "Anthropic/claude-gate-1"},
            (403,),
        ),
        ("D25-provider-only", "POST", "/v1/pi/stream", {"modelId": "anthropic/"}, (403,)),
        (
            "D26-oversized-model-id",
            "POST",
            "/v1/pi/stream",
            {"modelId": "anthropic/" + "a" * 400},
            (403,),
        ),
        ("D27-missing-model", "POST", "/v1/pi/stream", {"context": {}}, (400, 403)),
        (
            "D28-method-put-messages",
            "PUT",
            "/v1/messages",
            {"model": "anthropic/claude-gate-1"},
            (405,),
        ),
        ("D29-method-delete-messages", "DELETE", "/v1/messages", None, (405,)),
        ("D30-method-get-messages", "GET", "/v1/messages", None, (405,)),
        ("D31-method-post-models", "POST", "/v1/models", {"x": 1}, (405,)),
        ("D32-path-traversal", "GET", "/v1/messages/../usage", None, (404,)),
        ("D33-encoded-path", "GET", "/v1/%75sage", None, (404,)),
        ("D34-double-slash", "GET", "//v1/usage", None, (400, 403)),
        ("D35-usage-query", "GET", "/v1/usage?x=1", None, (403,)),
    ]
    for case_id, method, path, body, statuses in denied:
        cases.append(_case(case_id, method, path, body=body, forward=False, statuses=statuses))
    cases.append(
        _case(
            "D36-duplicate-model-keys",
            "POST",
            "/v1/pi/stream",
            body='{"modelId":"anthropic/claude-gate-1","modelId":"google-antigravity/g","m":"__MARKER__"}',
            forward=False,
            statuses=(400, 403),
            raw_body=True,
        )
    )
    cases.append(
        _case(
            "D37-not-json",
            "POST",
            "/v1/messages",
            body="not json __MARKER__",
            forward=False,
            statuses=(400, 403),
            raw_body=True,
        )
    )
    cases.append(
        _case(
            "D38-chunked-body",
            "POST",
            "/v1/pi/stream",
            body={"modelId": "anthropic/claude-gate-1"},
            forward=False,
            statuses=(411,),
            chunked=True,
        )
    )
    return cases


def _judge_relay_cases(
    cases: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    requests: list[Any],
) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    for case in cases:
        matching = [r for r in requests if case["marker"] in (r.path + r.body_text)]
        result = results.get(case["id"], {})
        status_ok = result.get("status") in case["statuses"]
        forwarded_ok = len(matching) == (1 if case["forward"] else 0)
        outcomes.append(
            {
                "id": case["id"],
                "request": f"{case['method']} {case['path'].split('?')[0]}",
                "expected_forward": case["forward"],
                "status": result.get("status"),
                "expected_statuses": case["statuses"],
                "upstream_requests": len(matching),
                "ok": status_ok and forwarded_ok,
            }
        )
    return {
        "cases": outcomes,
        "failing": [o["id"] for o in outcomes if not o["ok"]],
        "upstream_total": len(requests),
    }


def _relay_log(gate: Gate, container: str) -> list[dict[str, str]]:
    try:
        text = gate.engine.logs_tail(container, lines=2000, max_bytes=512 * 1024)
    except EngineryError:
        return []
    entries = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0] == "relay":
            entries.append(
                {"method": parts[1], "path": parts[2], "decision": parts[3], "status": parts[4]}
            )
    return entries


def _relay_up(
    gate: Gate,
    manager: RelayManager,
    *,
    upstream_candidates: list[str],
    token_file: Path,
    probe_stub: UpstreamStub | None,
) -> tuple[str | None, list[str]]:
    """Start the real relay against the first candidate upstream the relay container can reach."""
    assert gate.relay_image is not None
    notes: list[str] = []
    for upstream in upstream_candidates:
        try:
            manager.up(
                relay_image=gate.relay_image.image_id,
                upstream=upstream,
                token_file=token_file,
                recreate=True,
            )
        except EngineryError as error:
            notes.append(f"{upstream}: relay did not start: {error}")
            continue
        if probe_stub is None:
            return upstream, notes
        before = len(probe_stub.snapshot(include_probes=True))
        host = upstream.removeprefix("http://")
        completed = gate.engine_cli(
            "exec",
            manager.container_name,
            "python3",
            "-c",
            "import urllib.request,sys;"
            f"print(urllib.request.urlopen('http://{host}/__gate_probe',timeout=8).status)",
            timeout=60,
        )
        reached = len(probe_stub.snapshot(include_probes=True)) > before
        notes.append(f"{upstream}: reachable from the relay container = {reached}")
        if completed.returncode == 0 and reached:
            return upstream, notes
    return None, notes


def _down(manager: RelayManager) -> Callable[[], None]:
    def action() -> None:
        manager.down()

    return action


def step_relay(gate: Gate, setup: dict[str, Any]) -> None:
    fixture = gate.fixture
    assert fixture is not None and gate.relay_image is not None
    token = f"gate-fake-gateway-{secrets.token_hex(16)}"
    gate.scrubber.register("fake-gateway-token", token)
    token_dir = gate.workroot / "relay"
    token_dir.mkdir(mode=0o700)
    token_file = token_dir / "gateway.token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    stub = UpstreamStub(expected_bearer=token)
    stub.start()
    gate.upstream = stub
    manager = RelayManager(
        gate.engine,
        profile=gate.profile,
        egress_network=gate.egress_network,
        container_name=gate.relay_container,
    )
    gate.track_container(gate.relay_container)
    gate.track_network(gate.egress_network)
    gate.on_cleanup(_down(manager))
    candidates = [f"http://host.docker.internal:{stub.port}"]
    if setup.get("bridge_gateway") and gate.engine_identity.kind != "docker-desktop":
        egress_gateway = _bridge_gateway(gate, gate.egress_network) or setup["bridge_gateway"]
        candidates.append(f"http://{egress_gateway}:{stub.port}")
    upstream, notes = _relay_up(
        gate, manager, upstream_candidates=candidates, token_file=token_file, probe_stub=stub
    )
    status = manager.status()
    up_commands = [
        "RelayManager(engine, profile=<gate profile>, egress_network=<gate egress>).up("
        f"relay_image={gate.relay_image.image_id}, upstream={upstream}, "
        "token_file=<fake 0600 token>)"
    ]
    if upstream is None:
        gate.evidence.assertion(
            "f-relay-hardened-and-reachable",
            passed=False,
            commands=up_commands,
            observed={"notes": notes, "status": status.to_state()},
            expected="the real relay starts hardened and reaches the loopback upstream",
            observer={"relay_status": status.to_state(), "reachability_notes": notes},
        )
        return
    cases = relay_cases(gate)
    params = {
        "relay_url": f"http://{RELAY_HOSTNAME}:{RELAY_PORT}",
        "cases": cases,
        "env_keys": ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"],
    }
    before_requests = len(stub.snapshot())
    attempt = gate.attempt("relay", "relay", params, relay=True)
    status_after = manager.status()
    attempt_network_gone = gate.engine.network_info(attempt.attempt_network) is None
    requests = stub.snapshot()[before_requests:]
    results = {item["id"]: item for item in (attempt.obs or {}).get("results", [])}
    judged = _judge_relay_cases(cases, results, requests)
    raw_requests = gate.raw_write("relay.upstream-requests.json", [r.to_state() for r in requests])
    log = _relay_log(gate, gate.relay_container)
    clean = _clean(attempt)
    gate.evidence.assertion(
        "f-relay-hardened-and-reachable",
        passed=status.healthy
        and set(status.networks) == {gate.egress_network}
        and status_after.healthy
        and set(status_after.networks) == {gate.egress_network}
        and attempt_network_gone
        and clean
        and results.get("A1-healthz", {}).get("status") == 200
        and attempt.receipt is not None
        and attempt.receipt.effective.networks == (attempt.attempt_network,),
        commands=[*up_commands, *gate.evidence_commands(attempt)[:1]],
        observed={
            "relay_status": status.to_state(),
            "upstream": upstream,
            "reachability_notes": notes,
            "relay_networks_before_attempt": list(status.networks),
            "relay_networks_after_attempt": list(status_after.networks),
            "attempt_network_removed": attempt_network_gone,
            "worker_networks": []
            if attempt.receipt is None
            else list(attempt.receipt.effective.networks),
            "healthz_status": results.get("A1-healthz", {}).get("status"),
        },
        expected=(
            "relay healthy on the egress network only while idle; the worker attached to its own "
            "per-attempt internal network only; that network removed and the relay detached after"
        ),
        observer={"relay_container_inspect": status.to_state()},
        raw_artifacts=gate.raw_refs(attempt),
    )
    allowed = [c for c in judged["cases"] if c["id"].startswith("A")]
    forwarded_requests = list(requests)
    credential_ok = bool(forwarded_requests) and all(
        r.authorization_matches_gateway
        and r.authorization_scheme == "Bearer"
        and "x-api-key" not in r.headers
        and "cookie" not in r.headers
        and not any(f"worker-attempt-{gate.rid}" in value for value in r.headers.values())
        for r in forwarded_requests
    )
    response_text = json.dumps(attempt.obs or {})
    leaked_token = token in response_text or token in attempt.output_text()
    gate.evidence.assertion(
        "f-relay-allowed-routes-forwarded",
        passed=clean
        and all(c["ok"] for c in allowed)
        and len(forwarded_requests) == 5
        and credential_ok
        and not leaked_token,
        commands=gate.evidence_commands(attempt)[:1]
        + [f"[in container] {c['method']} {c['path']}" for c in cases if c["id"].startswith("A")],
        observed={
            "allowed_cases": allowed,
            "upstream_requests": [r.to_state() for r in forwarded_requests],
            "bearer_is_injected_gateway_token_on_every_request": credential_ok,
            "inbound_headers_sent_by_worker": ["Authorization", "X-Api-Key", "Cookie"],
            "gateway_token_seen_by_worker": leaked_token,
            "relay_log_decisions": sorted({e["decision"] for e in log}),
        },
        expected="5 forwards; bearer injected; inbound credentials stripped",
        observer={"upstream_request_log": raw_requests},
        raw_artifacts=[*gate.raw_refs(attempt), raw_requests],
    )
    by_model: dict[str, str | None] = {}
    for request in requests:
        for case in cases:
            if case["marker"] in request.body_text:
                try:
                    document = json.loads(request.body_text)
                    by_model[case["id"]] = document.get("model") or document.get("modelId")
                except ValueError:
                    by_model[case["id"]] = None
    gate.evidence.assertion(
        "f-relay-bare-claude-rewrite",
        passed=by_model.get("A4-messages-bare-claude") == "anthropic/claude-gate-bare"
        and by_model.get("A3-messages") == "anthropic/claude-gate-1"
        and by_model.get("A5-pi-stream-anthropic") == "anthropic/claude-gate-2",
        commands=["[in container] POST /v1/messages model=claude-gate-bare"],
        observed={"model_seen_by_upstream": by_model},
        expected="bare claude id canonicalized to anthropic/<id>; canonical ids unchanged",
        observer={"upstream_request_log": raw_requests},
        raw_artifacts=[raw_requests],
    )
    models_body = results.get("A2-models", {}).get("body", "")
    try:
        listed = sorted(item["id"] for item in json.loads(models_body)["data"])
    except (ValueError, KeyError, TypeError):
        listed = ["<unparseable>"]
    gate.evidence.assertion(
        "f-relay-models-filtered",
        passed=listed == ["anthropic/claude-gate-1", "openai-codex/gpt-gate"],
        commands=["[in container] GET /v1/models"],
        observed={"models_returned_to_worker": listed, "models_offered_by_upstream_count": 5},
        expected="only anthropic/* and openai-codex/* models are returned",
        observer={"upstream_request_log": raw_requests},
        raw_artifacts=[raw_requests],
    )
    denied = [c for c in judged["cases"] if c["id"].startswith("D")]
    denied_forwarded = [c["id"] for c in denied if c["upstream_requests"] != 0]
    gate.evidence.assertion(
        "f-relay-denied-zero-upstream",
        passed=clean
        and all(c["ok"] for c in denied)
        and judged["upstream_total"] == 5
        and not denied_forwarded,
        commands=[
            f"[in container] {c['method']} {c['path']}" for c in cases if c["id"].startswith("D")
        ],
        observed={
            "denied_cases": denied,
            "denied_cases_that_reached_upstream": denied_forwarded,
            "upstream_requests_total": judged["upstream_total"],
            "relay_log_counts": _count_decisions(log),
        },
        expected="every denied case is refused with the expected status and zero upstream requests",
        observer={"upstream_request_log": raw_requests},
        raw_artifacts=[*gate.raw_refs(attempt), raw_requests],
    )
    gate.effects["relay_upstream_requests_forwarded"] = len(requests)
    gate.effects["relay_forward_log_entries"] = len(
        [e for e in log if e["decision"].startswith("forward")]
    )
    gate.effects["relay_denied_requests_reaching_upstream"] = len(denied_forwarded)
    manager.down()
    if gate.options.gateway_url is not None:
        _real_gateway(gate, manager)


def _count_decisions(log: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in log:
        counts[entry["decision"]] = counts.get(entry["decision"], 0) + 1
    return counts


def _real_gateway(gate: Gate, manager: RelayManager) -> None:
    """GET /v1/models and refused routes through the relay against the operator's real gateway."""
    options = gate.options
    assert options.gateway_url is not None and options.gateway_token_file is not None
    assert gate.relay_image is not None
    token_file = options.gateway_token_file
    commands = [
        f"RelayManager.up(upstream={options.gateway_url}, token_file=<operator token file>)",
    ]
    cases = [
        _case("R1-models", "GET", "/v1/models", forward=True, statuses=(200,)),
        _case("R2-usage", "GET", "/v1/usage", forward=False, statuses=(403,)),
        _case(
            "R3-credentials-check", "GET", "/v1/credentials/check", forward=False, statuses=(403,)
        ),
        _case(
            "R4-antigravity-model",
            "POST",
            "/v1/pi/stream",
            body={"modelId": "google-antigravity/gemini-gate"},
            forward=False,
            statuses=(403,),
        ),
        _case(
            "R5-openai-model",
            "POST",
            "/v1/messages",
            body={**_MESSAGE_BODY, "model": "openai/gpt-gate"},
            forward=False,
            statuses=(403,),
        ),
    ]
    observed: dict[str, Any] = {}
    passed = False
    refs: list[str] = []
    try:
        manager.up(
            relay_image=gate.relay_image.image_id,
            upstream=options.gateway_url,
            token_file=token_file,
            recreate=True,
        )
        attempt = gate.attempt(
            "relay-real-gateway",
            "relay",
            {"relay_url": f"http://{RELAY_HOSTNAME}:{RELAY_PORT}", "cases": cases, "env_keys": []},
            relay=True,
        )
        refs = gate.raw_refs(attempt)
        commands += gate.evidence_commands(attempt)
        results = {item["id"]: item for item in (attempt.obs or {}).get("results", [])}
        log = _relay_log(gate, gate.relay_container)
        models = results.get("R1-models", {})
        try:
            count = len(json.loads(models.get("body", ""))["data"])
            ids_ok = all(
                item["id"].startswith(("anthropic/", "openai-codex/"))
                for item in json.loads(models.get("body", ""))["data"]
            )
        except (ValueError, KeyError, TypeError):
            count, ids_ok = -1, False
        forwarded = [e for e in log if e["decision"].startswith("forward")]
        statuses = {cid: results.get(cid, {}).get("status") for cid in [c["id"] for c in cases]}
        observed = {
            "models_status": models.get("status"),
            "models_count": count,
            "all_listed_models_in_permitted_providers": ids_ok,
            "statuses": statuses,
            "relay_log_counts": _count_decisions(log),
            "forwarded_requests": [
                f"{e['method']} {e['path']} -> {e['status']}" for e in forwarded
            ],
            "hosted_inference_calls": len(
                [e for e in forwarded if (e["method"], e["path"]) != ("GET", "/v1/models")]
            ),
        }
        passed = (
            _clean(attempt)
            and models.get("status") == 200
            and count >= 0
            and ids_ok
            and all(statuses[c["id"]] in c["statuses"] for c in cases[1:])
            and [f"{e['method']} {e['path']}" for e in forwarded] == ["GET /v1/models"]
        )
        gate.effects["real_gateway_models_requests"] = len(forwarded)
        gate.effects["real_gateway_inference_forwards"] = len(
            [e for e in forwarded if (e["method"], e["path"]) != ("GET", "/v1/models")]
        )
    except EngineryError as error:
        observed["error"] = f"{type(error).__name__}: {error}"
    finally:
        manager.down()
    gate.evidence.assertion(
        "f-relay-real-gateway-models-and-denials",
        passed=passed,
        commands=commands,
        observed=observed,
        expected="models 200 filtered, the four denied requests refused, exactly one forward",
        observer={"relay_log_decisions": observed.get("relay_log_counts")},
        raw_artifacts=refs,
    )


# --- (g) resource limits ---------------------------------------------------------------------


def step_limits(gate: Gate) -> None:
    limits = gate.limits_profile
    expected_memory = limits.resources.memory_bytes
    memory = gate.attempt(
        "limits-memory",
        "memory",
        {"allocate_mib": 1024},
        profile=limits,
        time_budget=120,
        observe=True,
    )
    observer = memory.observer or {}
    oom = "oom" in observer.get("event_actions", []) or observer.get("oom_killed_in_samples")
    receipt = memory.receipt
    gate.evidence.assertion(
        "g-limits-memory",
        passed=memory.exit_code == 137
        and bool(oom)
        and not memory.violations
        and receipt is not None
        and receipt.effective.memory_bytes == expected_memory
        and receipt.effective.memory_swap_bytes == expected_memory
        and memory.container_gone,
        commands=[*gate.evidence_commands(memory)[:1], "docker events --filter container=<name>"],
        observed={
            "launcher_exit_code": memory.exit_code,
            "engine_events": observer.get("event_actions"),
            "oom_killed_sampled_from_docker_inspect": observer.get("oom_killed_in_samples"),
            "last_inspected_state": observer.get("last_state"),
            "limit_mib": limits.resources.memory_mib,
            "allocation_attempted_mib": 1024,
            "worker_output_tail": memory.output_text()[-300:],
        },
        expected="exit 137 and an engine OOM event with memory == memory-swap == 128 MiB",
        observer=observer,
        raw_artifacts=gate.raw_refs(memory),
    )
    pids = gate.attempt(
        "limits-pids",
        "pids",
        {"fork_attempts": 200},
        profile=limits,
        time_budget=120,
        observe=True,
    )
    pobs = pids.obs or {}
    peak = pobs.get("cgroup_at_peak", {})
    events_text = str(pobs.get("cgroup_after", {}).get("pids.events"))
    max_events = 0
    for line in events_text.splitlines():
        if line.startswith("max "):
            max_events = int(line.split()[1])
    gate.evidence.assertion(
        "g-limits-pids",
        passed=pids.exit_code == 0
        and pobs.get("fork_failure") == "EAGAIN"
        and 0 < int(pobs.get("children_created", 0)) < limits.resources.pids
        and peak.get("pids.max") == str(limits.resources.pids)
        and max_events > 0
        and not pids.violations
        and pids.receipt is not None
        and pids.receipt.effective.pids_limit == limits.resources.pids
        and pids.container_gone,
        commands=[*gate.evidence_commands(pids)[:1], "[in container] os.fork() x200"],
        observed={
            "pids_limit": limits.resources.pids,
            "fork_attempts": pobs.get("fork_attempts"),
            "children_created": pobs.get("children_created"),
            "fork_failure": pobs.get("fork_failure"),
            "pids.max": peak.get("pids.max"),
            "pids.current_at_peak": peak.get("pids.current"),
            "pids.events": pobs.get("cgroup_after", {}).get("pids.events"),
        },
        expected="fork fails with EAGAIN below the pids limit and pids.events records max > 0",
        observer=pids.observer,
        raw_artifacts=gate.raw_refs(pids),
    )
    wall_budget = 5
    wall = gate.attempt(
        "limits-wall",
        "sleep",
        {"ignore_term": False},
        profile=limits,
        time_budget=wall_budget,
        observe=True,
    )
    wall_observer = wall.observer or {}
    gate.evidence.assertion(
        "g-limits-wall-time",
        passed=wall.exit_code in (124, 143)
        and wall.container_gone
        and wall.duration_seconds < wall_budget + 60
        and "die" in wall_observer.get("event_actions", [])
        and not wall.violations
        and "process group terminated" in wall.output_text(),
        commands=gate.evidence_commands(wall)[:1],
        observed={
            "time_budget_seconds": wall_budget,
            "launcher_exit_code": wall.exit_code,
            "duration_seconds": wall.duration_seconds,
            "engine_events": wall_observer.get("event_actions"),
            "container_removed_after": wall.container_gone,
            "worker_result_note": wall.output_text()[-200:],
        },
        expected="the worker deadline kills the process group (exit 143 or 124); container removed",
        observer=wall_observer,
        raw_artifacts=gate.raw_refs(wall),
    )
    hard = gate.attempt(
        "limits-launcher-deadline",
        "sleep",
        {"ignore_term": True},
        profile=limits,
        time_budget=3,
        observe=True,
        in_process_slack=0,
    )
    hard_observer = hard.observer or {}
    gate.evidence.assertion(
        "g-limits-launcher-deadline",
        passed=hard.exit_code == 124
        and hard.container_gone
        and "hard deadline" in hard.launcher_stderr
        and "kill" in hard_observer.get("event_actions", []),
        commands=[
            "enginery.adapters.oci.launcher.run_launcher(spec, ['python3', probe, 'sleep'], "
            "deadline_slack_seconds=0)  # worker ignores SIGTERM; launcher deadline = 3s budget "
            f"+ {limits.resources.stop_grace_seconds}s grace"
        ],
        observed={
            "launcher_exit_code": hard.exit_code,
            "duration_seconds": hard.duration_seconds,
            "engine_events": hard_observer.get("event_actions"),
            "launcher_log": hard.launcher_stderr.strip()[-300:],
            "container_removed_after": hard.container_gone,
        },
        expected="launcher exit 124, engine kill event, container removed",
        observer=hard_observer,
        raw_artifacts=gate.raw_refs(hard),
    )
    cpu = gate.attempt(
        "limits-cpu", "cpu", {"seconds": 8}, profile=limits, time_budget=120, observe=False
    )
    cobs = cpu.obs or {}
    cores = cobs.get("cores_used")
    gate.evidence.assertion(
        "g-limits-cpu",
        passed=cpu.exit_code == 0
        and isinstance(cores, int | float)
        and cores <= limits.resources.cpus * 1.25
        and int(cobs.get("throttled_periods", 0)) > 0
        and cpu.receipt is not None
        and cpu.receipt.effective.nano_cpus == limits.resources.nano_cpus
        and not cpu.violations,
        commands=[*gate.evidence_commands(cpu)[:1], "[in container] busy loop for 8 seconds"],
        observed={
            "cpu_limit_cores": limits.resources.cpus,
            "cores_used": cores,
            "throttled_periods": cobs.get("throttled_periods"),
            "throttled_usec": cobs.get("throttled_usec"),
            "cpu.max": cobs.get("cgroup_cpu_max"),
        },
        expected="cores used <= 1.25 x limit and the cgroup recorded throttled periods",
        observer=cpu.observer,
        raw_artifacts=gate.raw_refs(cpu),
    )


# --- (h) hostile candidate -------------------------------------------------------------------


def _hostile_test(fixture: HostFixture) -> str:
    engine_file = str(fixture.engine_dir / "enginery" / "__init__.py")
    host_marker = str(fixture.markers / "executed-validation")
    return (
        "import errno, json, os, unittest\n\n\n"
        "def attempt(fn):\n"
        "    try:\n"
        "        fn()\n"
        "        return 'performed'\n"
        "    except OSError as error:\n"
        "        return errno.errorcode.get(error.errno or 0, 'OSError')\n\n\n"
        "def _append(path):\n"
        "    with open(path, 'a') as handle:\n"
        "        handle.write('x')\n\n\n"
        "def _read(path):\n"
        "    with open(path) as handle:\n"
        "        return handle.read()\n\n\n"
        "class Candidate(unittest.TestCase):\n"
        "    def test_hostile_actions(self):\n"
        "        results = {\n"
        "            'write-installed-engine-host-path': attempt(\n"
        f"                lambda: _append({engine_file!r})\n"
        "            ),\n"
        f"            'write-host-marker': attempt(lambda: _append({host_marker!r})),\n"
        "            'write-opt-enginery': attempt(\n"
        "                lambda: _append('/opt/enginery/worker_entry.py')\n"
        "            ),\n"
        "            'write-site-packages': attempt(\n"
        "                lambda: _append('/usr/lib/python3/dist-packages/gate.pth')\n"
        "            ),\n"
        "            'uid': os.getuid(),\n"
        "            'cwd': os.getcwd(),\n"
        "            'leftover_present': os.path.exists('untracked-leftover.txt'),\n"
        "            'modified_tracked_file_content': _read('src/base.py'),\n"
        "        }\n"
        "        print('GATE-VALIDATION ' + json.dumps(results, sort_keys=True))\n"
    )


def step_hooks(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None
    scripts = _marker_scripts(fixture)
    sitecustomize_marker = str(fixture.markers / "executed-sitecustomize")
    branch = "enginery/gate-hooks"
    workspace = new_task_workspace(fixture, "hooks")
    engine_before = snapshot_tree(fixture.engine_dir)
    protected_before = gate.protected_snapshot()
    committed = [
        {"path": "tests/test_candidate.py", "content": _hostile_test(fixture)},
        {
            "path": "conftest.py",
            "content": (
                "import pathlib\n"
                f"pathlib.Path({str(fixture.markers / 'executed-conftest')!r}).write_text('x')\n"
            ),
        },
        {
            "path": "sitecustomize.py",
            "content": f"open({sitecustomize_marker!r}, 'w').write('x')\n",
        },
        {"path": ".gitattributes", "content": "*.gate diff=gatehook filter=gatehook\n"},
        {"path": "sample.gate", "content": "gate attribute driver trigger\n"},
        {
            "path": ".githooks/pre-commit",
            "content": _hook(fixture, "githooks-pre-commit"),
            "mode": 0o755,
        },
        {"path": "Makefile", "content": f"all:\n\t{scripts['engine-install']}\n"},
    ]
    plants = [
        {"path": f".git/hooks/{name}", "content": _hook(fixture, f"gitdir-{name}"), "mode": 0o755}
        for name in (
            "pre-commit",
            "post-commit",
            "post-checkout",
            "post-merge",
            "pre-push",
            "reference-transaction",
            "pre-receive",
            "update",
            "post-update",
            "fsmonitor-watchman",
            "pre-auto-gc",
        )
    ]
    git_config = {
        "core.sshCommand": str(scripts["ssh"]),
        "core.fsmonitor": str(scripts["fsmonitor"]),
        "core.pager": str(scripts["pager"]),
        "core.editor": str(scripts["editor"]),
        "core.hooksPath": "/workspace/.githooks",
        "diff.gatehook.textconv": str(scripts["textconv"]),
        "filter.gatehook.smudge": str(scripts["smudge"]),
        "filter.gatehook.clean": str(scripts["smudge"]),
        "credential.helper": f"!{scripts['credential']}",
        "uploadpack.packObjectsHook": str(scripts["packhook"]),
        "protocol.ext.allow": "always",
        f"url.ext::{scripts['ssh']} %S.insteadOf": "https://github.com/",
    }
    params = {
        "branch": branch,
        "committed": committed,
        "plants": plants,
        "git_config": git_config,
        "leftovers": [
            {"path": "untracked-leftover.txt", "content": "uncommitted worker leftover\n"},
            {"path": "src/base.py", "content": "VALUE = 'modified-but-uncommitted'\n"},
        ],
    }
    attempt = gate.attempt("hooks", "plant", params, workspace=workspace, bundle_branch=branch)
    ops = attempt.operations
    traps_set = {
        "hooks_present_in_worker_gitdir": sorted(
            p.name for p in (workspace / ".git" / "hooks").glob("*")
        ),
        "planted_core_sshCommand_in_worker_config": "sshCommand"
        in (workspace / ".git" / "config").read_text(),
        "planted_ops_performed": all(op["ok"] for op in ops),
    }
    # Positive control: the marker observer sees an execution when one really happens.
    probe_marker = fixture.markers / "executed-control"
    control_script = fixture.markers / "control.sh"
    control_script.write_text(
        f"#!/bin/sh\nprintf 'executed\\n' >> {shlex.quote(str(probe_marker))}\n", encoding="utf-8"
    )
    subprocess.run(["sh", str(control_script)], check=True)
    observer_sensitive = probe_marker.exists()
    probe_marker.unlink(missing_ok=True)
    gate.evidence.control(
        "control-host-marker-observer-detects-execution",
        description="Running a planted-style marker script on the host leaves its marker file",
        passed=observer_sensitive,
        observed={"marker_created_by_deliberate_host_run": observer_sensitive},
        expected="the marker observer is sensitive: a deliberate execution leaves a marker",
        commands=["sh control.sh  # host, deliberate; its marker is removed afterwards"],
    )
    publish: dict[str, Any] = {}
    validation: dict[str, Any] = {}
    errors: list[str] = []
    try:
        head = intake_branch_bundle(
            repository_path=fixture.repo,
            workspace_path=workspace,
            bundle_path=attempt.out / "branch.bundle",
            head_branch=branch,
            base_revision=fixture.base_revision,
        )
        verified = verify_intake_branch(
            intake_path=intake_path_for(workspace),
            head_branch=branch,
            base_revision=fixture.base_revision,
        )
        publish["intake_head"] = head
        publish["verified_head"] = verified
        context = RunnerContext(
            selection=RunnerSelection.isolated(
                image=gate.runner_image.image_id,
                profile=gate.profile,
                docker_context=gate.docker_context,
            ),
            profile=gate.profile,
            receipt_root=str(fixture.receipts),
        )
        existing_receipts = set(fixture.receipts.glob("*.validation-receipt.json"))
        with verified_head_checkout(workspace_path=workspace, head_revision=verified) as checkout:
            validation["checkout"] = str(checkout)
            validation["checkout_is_validation_path"] = checkout == validation_path_for(workspace)
            validation["checkout_has_leftover"] = (checkout / "untracked-leftover.txt").exists()
            completed = OciValidationRunner(
                context,
                artifact_root=fixture.work / "validation-artifacts",
                time_budget_seconds=120,
            )(
                (
                    "python3",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_*.py",
                    "-v",
                ),
                checkout,
            )
            validation["exit_code"] = completed.returncode
            validation["receipt_linked_to_result"] = completed.runner_receipt is not None
            validation["output_tail"] = completed.stdout[-600:]
            found = re.search(r"GATE-VALIDATION (\{.*\})", completed.stdout)
            if found is not None:
                validation["in_container_results"] = json.loads(found.group(1))
        validation["checkout_removed_after"] = not validation_path_for(workspace).exists()
        new_receipts = sorted(
            set(fixture.receipts.glob("*.validation-receipt.json")) - existing_receipts
        )
        validation["receipts"] = []
        for path in new_receipts:
            receipt, document = read_receipt(path)
            validation["receipts"].append(
                {
                    "violations": list(violations_of(gate.profile, receipt)),
                    "harness": None if receipt is None else receipt.permissions.harness,
                    "bypass": None if receipt is None else receipt.permissions.unconditional_bypass,
                }
            )
            gate.raw_write(f"hooks.validation-{path.name}", document or {})
        validation["worker_workspace_has_leftover"] = (
            workspace / "untracked-leftover.txt"
        ).exists()
        publisher = GitBranchPublisher(
            repository_path=fixture.repo, source_path=intake_path_for(workspace)
        )
        published = publisher.publish(
            BranchPublicationRequest(
                repository_id="gate-fixture",
                branch=branch,
                head_revision=verified,
                expected_remote_revision=None,
                operation_id=OperationId(f"gate-publish-hooks-{gate.rid}"),
            )
        )
        publish["published_revision"] = published.revision
        publish["origin_refs"] = bare_refs(fixture.origin)
    except Exception as error:
        errors.append(f"{type(error).__name__}: {error}")
    executed = _executed_markers(fixture)
    engine_after = snapshot_tree(fixture.engine_dir)
    engine_changes = diff_snapshots(engine_before, engine_after)
    protected_changes = gate.protected_changes(protected_before, gate.protected_snapshot())
    # origin legitimately gains the published branch; everything else must be untouched
    protected_changes = [c for c in protected_changes if not c.startswith("origin.git:")]
    gate.evidence.assertion(
        "h-hooks-not-executed-on-host",
        passed=bool(
            clean_enough(attempt)
            and not errors
            and not executed
            and traps_set["planted_ops_performed"]
            and bool(traps_set["hooks_present_in_worker_gitdir"])
            and traps_set["planted_core_sshCommand_in_worker_config"]
            and publish.get("published_revision") == publish.get("verified_head")
            and observer_sensitive
        ),
        commands=[
            *gate.evidence_commands(attempt)[:1],
            "intake_branch_bundle(...)",
            "verify_intake_branch(...)",
            "verified_head_checkout(...) + OciValidationRunner(...)(python3 -m unittest ...)",
            "GitBranchPublisher.publish(...)",
        ],
        observed={
            "traps_planted_in_worker_workspace": traps_set,
            "git_config_keys_planted": sorted(git_config),
            "host_executions_observed_by_marker_files": executed,
            "host_steps": publish,
            "errors": errors,
        },
        expected="no marker file: nothing the worker planted ran on the host at any step",
        observer={"markers_dir_executed": executed},
        raw_artifacts=gate.raw_refs(attempt),
    )
    gate.evidence.assertion(
        "h-engine-install-unchanged",
        passed=not engine_changes and not protected_changes and not executed,
        commands=[
            "snapshot_tree(<installed engine dir>) before the candidate and after publication",
            "snapshot_tree(<every protected host tree>) before and after",
        ],
        observed={
            "engine_install_changes": engine_changes,
            "protected_tree_changes": protected_changes,
            "engine_install_digest_before": engine_before_digest(engine_before),
            "engine_install_files": len(engine_before),
        },
        expected="no change to the installed engine or any protected host tree",
        observer={
            "engine_install_digest_before": engine_before_digest(engine_before),
            "engine_install_digest_after": engine_before_digest(engine_after),
            "executed_markers": executed,
        },
    )
    results = validation.get("in_container_results", {})
    receipts = validation.get("receipts", [])
    gate.evidence.assertion(
        "h-validation-in-runner",
        passed=not errors
        and validation.get("checkout_is_validation_path") is True
        and validation.get("checkout_removed_after") is True
        and validation.get("checkout_has_leftover") is False
        and results.get("leftover_present") is False
        and results.get("cwd") == WORKSPACE_MOUNT
        and results.get("uid") == 65532
        and results.get("write-installed-engine-host-path") == "ENOENT"
        and results.get("write-host-marker") == "ENOENT"
        and results.get("write-opt-enginery") in ("EROFS", "EACCES")
        and results.get("write-site-packages") == "EROFS"
        and results.get("modified_tracked_file_content") == "VALUE = 1\n"
        and validation.get("worker_workspace_has_leftover") is True
        and validation.get("receipt_linked_to_result") is True
        and len(receipts) == 1
        and not receipts[0]["violations"]
        and receipts[0]["harness"] == "validation"
        and receipts[0]["bypass"] is False,
        commands=[
            "verified_head_checkout(workspace_path=<task workspace>, head_revision=<head>)",
            "OciValidationRunner(...)(python3 -m unittest discover -s tests, <verified checkout>)",
        ],
        observed=validation,
        expected=(
            "validation ran in a container against a clean checkout of the verified head: host "
            "paths invisible, image paths EROFS, uncommitted leftovers absent, receipt clean"
        ),
        observer={"validation_receipts": receipts},
    )
    erofs = gate.attempt(
        "erofs",
        "write",
        {
            "refused": [
                {"id": f"erofs:{action}:{path}", "action": action, "path": path}
                for path in (
                    "/opt/enginery/gate-planted.py",
                    "/usr/lib/python3/dist-packages/gate-planted.pth",
                    "/usr/local/lib/node_modules/gate-planted.js",
                    "/usr/local/bin/gate-planted",
                )
                for action in ("create",)
            ],
            "allowed": [],
            "plants": [],
            "watched_files": [],
        },
    )
    site = (erofs.obs or {}).get("site_packages_exist", {})
    site_ops = list(erofs.operations)
    gate.evidence.assertion(
        "h-image-paths-read-only",
        passed=_clean(erofs)
        and len(site_ops) == 4
        and all(op["ok"] and op["error"] == "EROFS" for op in site_ops),
        commands=gate.evidence_commands(erofs),
        observed={
            "operations": summarize_operations(site_ops),
            "python_site_packages_reported_by_interpreter": site,
        },
        expected="every write into /opt, site-packages, node_modules and /usr/local/bin is EROFS",
        observer=erofs.observer,
        raw_artifacts=gate.raw_refs(erofs),
    )
    gate.effects["host_hook_executions"] = len(executed)
    gate.effects["publications_by_operator"] = 2


def clean_enough(attempt: AttemptResult) -> bool:
    """The hostile worker ran under a clean receipt (host changes are judged separately)."""
    return attempt.exit_code == 0 and attempt.obs is not None and not attempt.violations


def engine_before_digest(snapshot: dict[str, str]) -> str:
    from factory_gate_support import tree_digest

    return tree_digest(snapshot)


# --- (i) fail-closed admission ---------------------------------------------------------------


def _weak_container(gate: Gate, label: str, flags: list[str], expect: str) -> dict[str, Any]:
    name = f"enginery-gate-{gate.rid}-weak-{label}"
    gate.track_container(name)
    command = ["create", "--name", name, "--label", f"{GATE_LABEL}={gate.rid}", *flags]
    command += [gate.runner_image.image_id, "sleep", "1"]
    completed = gate.engine_cli(*command, timeout=120)
    entry: dict[str, Any] = {
        "case": label,
        "command": gate.docker_command(*command),
        "created": completed.returncode == 0,
        "expected_violation": expect,
    }
    if completed.returncode != 0:
        entry["error"] = completed.stderr.strip()[-200:]
        entry["flagged"] = False
        return entry
    container = gate.engine.container_inspect(name)
    image = gate.engine.image_inspect(gate.runner_image.image_id)
    effective = effective_from_inspect(container, image)
    problems = profile_violations(gate.profile, effective, network=NO_NETWORK)
    entry["violations"] = list(problems)
    entry["engine_reported"] = effective.to_state()
    entry["flagged"] = any(expect in problem for problem in problems)
    gate.engine.remove(name, force=True)
    return entry


def step_admission(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None
    weak = [
        ("privileged", ["--privileged"], "privileged"),
        ("cap-add", ["--cap-add", "NET_ADMIN"], "adds capabilities"),
        (
            "writable-rootfs",
            ["--user", "65532:65532", "--cap-drop", "ALL"],
            "root filesystem is writable",
        ),
        (
            "no-limits",
            [
                "--read-only",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
            ],
            "memory limit is not the profile limit",
        ),
        (
            "default-bridge",
            ["--read-only", "--network", "bridge"],
            "is not attached only to the network",
        ),
        ("root-user", ["--read-only", "--user", "0:0"], "user is"),
        (
            "host-mount",
            ["--read-only", "-v", f"{fixture.config_dir}:/mnt/config:ro"],
            "unadmitted mount",
        ),
        (
            "stray-env",
            ["--read-only", "-e", "GH_TOKEN=gate-not-a-token"],
            "environment keys outside the allowlist",
        ),
        ("device", ["--read-only", "--device", "/dev/null:/dev/gate-null"], "host devices"),
    ]
    flagged = [_weak_container(gate, label, flags, expect) for label, flags, expect in weak]
    # Control: the container the product's own arguments create has no violation at all.
    from factory_gate_support import make_spec

    workspace = new_task_workspace(fixture, "admission-control")
    spec = make_spec(
        operation_id=f"gate:{gate.rid}:strong",
        image=gate.runner_image.image_id,
        profile=gate.profile,
        workspace=workspace,
        out=Path(str(workspace) + ".out"),
        receipt_path=fixture.receipts / "strong.json",
        time_budget_seconds=30,
        docker_context=gate.docker_context,
    )
    gate.track_container(spec.container_name)
    nonce = secrets.token_hex(16)
    arguments = create_arguments(spec, ["true"], nonce=nonce)
    # The worker joins the attempt's two capped volumes, which the store container creates.
    create_attempt_store(gate.engine, spec)
    try:
        created = gate.engine_cli(*arguments, timeout=120)
        strong: dict[str, Any] = {"created": created.returncode == 0}
        if created.returncode == 0:
            container = gate.engine.container_inspect(spec.container_name)
            image = gate.engine.image_inspect(gate.runner_image.image_id)
            names = store_names_for(spec.container_name)
            volumes = {
                volume: gate.engine.volume_inspect(volume)
                for volume in (names.workspace_volume, names.out_volume)
            }
            strong["violations"] = [
                *profile_violations(
                    gate.profile,
                    effective_from_inspect(container, image),
                    network=spec.attempt_network,
                ),
                *launch_violations(spec, container),
                *store_violations(spec, container, volumes),
            ]
            gate.engine.remove(spec.container_name, force=True)
    finally:
        remove_attempt_store(gate.engine, spec)
    gate.evidence.control(
        "control-product-created-container-has-no-violation",
        description="The container created from the product's own create_arguments is exact",
        passed=strong.get("created") is True and strong.get("violations") == [],
        observed=strong,
        expected="profile_violations and launch_violations are both empty",
        commands=[gate.docker_command(*arguments[:1], "<create_arguments(spec)>")],
    )
    gate.evidence.assertion(
        "i-profile-violations-flagged",
        passed=bool(flagged) and all(item["flagged"] for item in flagged),
        commands=[item["command"] for item in flagged],
        observed={"weaker_containers": flagged},
        expected="each weaker container yields the named profile violation",
        observer={item["case"]: item.get("engine_reported") for item in flagged},
    )
    admission = OciRunnerAdmission(
        engine_factory=lambda context: OciEngine(context=context), profile=gate.profile
    )
    commands = ["OciRunnerAdmission(profile=<gate profile>).admit(RunnerSelection.isolated(...))"]
    good = admission.admit(
        RunnerSelection.isolated(
            image=gate.runner_image.image_id,
            profile=gate.profile,
            docker_context=gate.docker_context,
        )
    )
    gate.evidence.control(
        "control-admission-admits-the-qualified-selection",
        description="Admission admits the pinned image under the current profile on this engine",
        passed=good.hold is None and good.engine is not None,
        observed={"hold": None if good.hold is None else good.hold.to_state()},
        expected="no hold",
        commands=commands,
    )
    missing_image = "sha256:" + secrets.token_hex(32)
    missing = admission.admit(
        RunnerSelection.isolated(
            image=missing_image, profile=gate.profile, docker_context=gate.docker_context
        )
    )
    absent_probe = gate.engine.run_cli(("image", "inspect", missing_image))
    gate.evidence.assertion(
        "i-admission-missing-image",
        passed=missing.hold is not None and missing.hold.reason is HoldReason.RUNNER_UNQUALIFIED,
        commands=commands,
        observed={"hold": None if missing.hold is None else missing.hold.to_state()},
        expected="hold: runner-unqualified (the run is held, not run in trusted-local mode)",
        observer={
            "docker_image_inspect_exit": absent_probe.returncode,
            "docker_image_inspect_stderr": absent_probe.stderr.strip()[-200:],
        },
    )
    bad_tag = f"enginery-gate-badlabel:{gate.rid}"
    bad_source = f"enginery-gate-{gate.rid}-badlabel-source"
    gate.track_container(bad_source)
    wrong_digest = f"sha256:{'0' * 64}"
    label = f"LABEL dev.enginery.recipe-digest={wrong_digest}"
    prepared = gate.engine_cli("create", "--name", bad_source, gate.runner_image.image_id, "true")
    commit = gate.engine_cli("commit", "--change", label, bad_source, bad_tag, timeout=300)
    gate.engine.remove(bad_source, force=True)
    bad_id = gate.engine.run_cli(
        ("image", "inspect", "--format", "{{.Id}}", bad_tag)
    ).stdout.strip()
    built = prepared.returncode == 0 and commit.returncode == 0 and bad_id.startswith("sha256:")
    if built:
        gate.created_tags.append(bad_tag)
    bad_labels = gate.engine.run_cli(
        ("image", "inspect", "--format", "{{json .Config.Labels}}", bad_tag)
    ).stdout.strip()[:600]
    wrong = (
        admission.admit(
            RunnerSelection.isolated(
                image=bad_id, profile=gate.profile, docker_context=gate.docker_context
            )
        )
        if built
        else None
    )
    gate.evidence.assertion(
        "i-admission-wrong-recipe",
        passed=wrong is not None
        and wrong.hold is not None
        and wrong.hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT,
        commands=[
            gate.docker_command("create", "--name", bad_source, gate.runner_image.image_id, "true"),
            gate.docker_command("commit", "--change", label, "<container>", bad_tag),
            *commands,
        ],
        observed={
            "built": built,
            "commit_stderr": commit.stderr.strip()[-200:],
            "image_id": bad_id,
            "hold": None if wrong is None or wrong.hold is None else wrong.hold.to_state(),
        },
        expected="hold: runner-fingerprint-drift (image not built from the current recipe)",
        observer={"engine_reported_recipe_label": bad_labels},
    )
    bogus = f"enginery-gate-no-such-context-{gate.rid}"
    bogus_probe = OciEngine(context=bogus).run_cli(("info",), timeout=60)
    unreachable = admission.admit(
        RunnerSelection.isolated(
            image=gate.runner_image.image_id, profile=gate.profile, docker_context=bogus
        )
    )
    gate.evidence.assertion(
        "i-admission-unreachable-engine",
        passed=unreachable.hold is not None
        and unreachable.hold.reason is HoldReason.RUNNER_UNQUALIFIED,
        commands=[
            "OciRunnerAdmission(...).admit(RunnerSelection.isolated(..., "
            f"docker_context={bogus!r}))"
        ],
        observed={
            "expected_outcome_of_this_sub_check": "held",
            "observed_outcome": "held" if unreachable.hold is not None else "admitted",
            "hold": None if unreachable.hold is None else unreachable.hold.to_state(),
            "scenario_verdict_note": (
                "an unreachable engine would hold the whole scenario; here the product's behaviour "
                "of holding is the thing asserted, on a deliberately bogus context"
            ),
        },
        expected="hold: runner-unqualified",
        observer={
            "docker_info_with_bogus_context_exit": bogus_probe.returncode,
            "docker_info_stderr": bogus_probe.stderr.strip()[-200:],
        },
    )
    drifted_profile = RunnerProfile(
        network=gate.profile.network, resources=ResourceLimits(memory_mib=2048)
    )
    drift = admission.admit(
        RunnerSelection.isolated(
            image=gate.runner_image.image_id,
            profile=drifted_profile,
            docker_context=gate.docker_context,
        )
    )
    gate.evidence.assertion(
        "i-admission-profile-drift",
        passed=drift.hold is not None and drift.hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT,
        commands=commands,
        observed={
            "bound_profile_digest": str(drifted_profile.digest),
            "current_profile_digest": str(gate.profile.digest),
            "hold": None if drift.hold is None else drift.hold.to_state(),
        },
        expected="hold: runner-fingerprint-drift",
        observer={"profile_digests_equal": drifted_profile.digest == gate.profile.digest},
    )
    _launcher_no_fallback(gate)


def _launcher_no_fallback(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None
    fallback_marker = fixture.markers / "executed-trusted-local-fallback"
    inner = ["sh", "-c", f"touch {shlex.quote(str(fallback_marker))}"]
    cases: list[tuple[str, dict[str, Any]]] = [
        ("missing-image", {"image": "sha256:" + secrets.token_hex(32)}),
        ("unreachable-engine", {"docker_context": f"enginery-gate-no-such-context-{gate.rid}"}),
        (
            "missing-relay",
            {"relay_container": f"enginery-gate-{gate.rid}-no-such-relay"},
        ),
    ]
    observed: list[dict[str, Any]] = []
    attempts: list[AttemptResult] = []
    for label, overrides in cases:
        attempt = gate.attempt(
            f"fallback-{label}",
            "none",
            {},
            stage=True,
            inner=inner,
            spec_overrides=overrides,
            time_budget=30,
        )
        attempts.append(attempt)
        observed.append(
            {
                "case": label,
                "launcher_exit_code": attempt.exit_code,
                "launcher_outcome_state": (attempt.outcome or {}).get("state"),
                "refusal": attempt.launcher_stderr.strip().splitlines()[-1][:240]
                if attempt.launcher_stderr.strip()
                else "",
                "container_created": not attempt.container_gone,
                "receipt_written": attempt.receipt is not None,
            }
        )
    ran_on_host = fallback_marker.exists()
    gate.evidence.assertion(
        "i-launcher-no-fallback",
        passed=all(
            item["launcher_exit_code"] == 125
            and item["launcher_outcome_state"] in ("refused", "faulted")
            for item in observed
        )
        and not any(item["container_created"] or item["receipt_written"] for item in observed)
        and not ran_on_host,
        commands=[a.command_line for a in attempts],
        observed={
            "cases": observed,
            "command_executed_on_host_by_fallback": ran_on_host,
        },
        expected="exit 125 with a named refusal, no container, no receipt, nothing run on the host",
        observer={"markers_dir_executed": _executed_markers(fixture)},
        raw_artifacts=[ref for a in attempts for ref in gate.raw_refs(a)],
    )


# --- final -----------------------------------------------------------------------------------


def _observed_hosted_inference(gate: Gate, stub_counts: dict[str, int]) -> dict[str, Any]:
    """Hosted-inference calls as observed, never as a constant.

    Counted from three independent observations: connections a worker completed to an internet
    address (the probe's own connect results), relay forwards the relay log shows to a real
    gateway beyond ``GET /v1/models``, and any forward the relay logged that the stub upstream
    did not receive (it left through some other route).
    """
    internet_connects = [
        {"attempt": attempt.name, "operation": op["id"]}
        for attempt in gate.attempts
        for op in attempt.operations
        if op["id"].startswith("tcp:internet-") and op.get("outcome") == "connected"
    ]
    real_forwards = int(gate.effects.get("real_gateway_inference_forwards", 0))
    logged = int(gate.effects.get("relay_forward_log_entries", 0))
    received = int(gate.effects.get("relay_upstream_requests_forwarded", 0))
    unaccounted = max(0, logged - received)
    return {
        "hosted_inference_calls": len(internet_connects) + real_forwards + unaccounted,
        "internet_connects_completed_by_workers": internet_connects,
        "real_gateway_forwards_beyond_models": real_forwards,
        "relay_logged_forwards": logged,
        "stub_upstream_received": received,
        "relay_forward_log_matches_stub": logged == received,
        "gateway_and_broker_listener_connections": sum(
            v for k, v in stub_counts.items() if k.startswith(("gateway-", "broker-"))
        ),
    }


def finalize(gate: Gate) -> None:
    """Record the assertions that can only be judged after cleanup."""
    fixture = gate.fixture
    assert fixture is not None and gate.stubs is not None
    receipts_checked = [
        {"attempt": a.name, "violations": list(a.violations)}
        for a in gate.attempts
        if a.receipt is not None
    ]
    bad = [item for item in receipts_checked if item["violations"]]
    expected_without_receipt = {
        "fallback-missing-image",
        "fallback-unreachable-engine",
        "fallback-missing-relay",
        "m2-refused",
    }
    missing_receipts = [
        a.name
        for a in gate.attempts
        if a.receipt is None and a.name not in expected_without_receipt
    ]
    gate.evidence.assertion(
        "z-profile-violations-all-receipts",
        passed=bool(receipts_checked) and not bad and not missing_receipts,
        commands=[
            "profile_violations(profile, receipt.effective) for every runner receipt written"
        ],
        observed={
            "receipts_checked": len(receipts_checked),
            "with_violations": bad,
            "attempts_without_receipt_unexpectedly": missing_receipts,
        },
        expected="every receipt reports an empty violation list",
        observer={"receipts": receipts_checked},
    )
    all_changes = {
        a.name: (a.observer or {}).get("host_protected_changes", [])
        for a in gate.attempts
        if (a.observer or {}).get("host_protected_changes")
    }
    gate.evidence.assertion(
        "z-canary-integrity",
        passed=not gate.leaks and not all_changes,
        commands=[
            "scan every container output, observation and launcher log for the canary values",
            "snapshot_tree() of every protected host tree before and after each attempt",
        ],
        observed={
            "canary_labels": sorted(c for c in fixture.canaries),
            "attempts_run": len(gate.attempts),
            "canary_values_found_in_container_output": gate.leaks,
            "protected_host_changes_during_attempts": all_changes,
        },
        expected="no canary in any output and no protected file changed during any attempt",
        observer={
            "attempt_host_changes": {
                a.name: (a.observer or {}).get("host_protected_changes") for a in gate.attempts
            },
        },
    )
    stray = gate.stray_containers()
    leftover_networks = [
        name
        for name in gate.engine_cli("network", "ls", "--format", "{{.Name}}").stdout.split()
        if name.startswith(f"enginery-gate-{gate.rid}")
    ]
    leftover_tags = [
        tag
        for tag in gate.created_tags
        if gate.engine_cli("image", "inspect", "--format", "{{.Id}}", tag).returncode == 0
    ]
    gate.evidence.assertion(
        "z-cleanup-no-stray-containers",
        passed=not stray
        and not leftover_networks
        and not leftover_tags
        and not gate.cleanup_report.get("errors")
        and not gate.cleanup_report.get("fixture_retained"),
        commands=[
            gate.docker_command("ps", "--all", "--format", "{{.Names}}"),
            gate.docker_command("network", "ls"),
            gate.docker_command("image", "rm", "<tags created by the gate>"),
        ],
        observed={
            "stray_enginery_containers": stray,
            "leftover_gate_networks": leftover_networks,
            "leftover_created_image_tags": leftover_tags,
            "cleanup": gate.cleanup_report,
        },
        expected="nothing left behind; pre-existing containers and images untouched",
        observer={"enginery_containers_now": sorted(set(iter_enginery_containers(gate.engine)))},
    )
    stub_counts = gate.stubs.counts()
    markers = _executed_markers(fixture) if fixture.markers.exists() else []
    hosted = _observed_hosted_inference(gate, stub_counts)
    gate.effects.update(
        {
            "hosted_inference_calls": hosted["hosted_inference_calls"],
            "gateway_stub_connections": sum(
                v for k, v in stub_counts.items() if k.startswith("gateway-")
            ),
            "broker_stub_connections": sum(
                v for k, v in stub_counts.items() if k.startswith("broker-")
            ),
            "control_listener_connections": sum(
                v for k, v in stub_counts.items() if k.startswith("control-")
            ),
            "protected_host_file_changes": sum(len(v) for v in all_changes.values()),
            "containers_created_by_gate": len({a.container_name for a in gate.attempts}),
        }
    )
    real_requests = gate.effects.get("real_gateway_models_requests", 0)
    gate.evidence.assertion(
        "z-no-hosted-inference",
        passed=hosted["hosted_inference_calls"] == 0
        and hosted["relay_forward_log_matches_stub"]
        and hosted["gateway_and_broker_listener_connections"] == 0
        and gate.effects.get("relay_denied_requests_reaching_upstream", 0) == 0
        and (gate.options.gateway_url is not None or real_requests == 0),
        commands=[
            "count what was observed: worker connections that reached the internet (the probe's "
            "own connect results), relay log forwards against the stub upstream's request log, "
            "gateway/broker listener connections, and relay forwards to a real gateway other "
            "than GET /v1/models",
        ],
        observed={
            **hosted,
            "stub_upstream_requests": gate.effects.get("relay_upstream_requests_forwarded"),
            "real_gateway_models_requests": real_requests,
            "executed_markers": markers,
        },
        expected="zero inference requests to any hosted model",
        observer={"stub_listener_connections": stub_counts},
    )


def run(gate: Gate) -> None:
    """Execute every step; a step that raises records its assertions as failed, never skipped."""

    def guarded(label: str, assertion_ids: list[str], action: Any) -> Any:
        try:
            return action()
        except GateHoldError:
            raise
        except Exception as error:
            reason = f"step {label} raised {type(error).__name__}: {error}"
            for assertion_id in assertion_ids:
                gate.evidence.fail_assertion(assertion_id, reason)
            gate.evidence.details.setdefault("step_errors", []).append(reason)
            return None

    setup = guarded("setup", [], lambda: step_setup(gate)) or {}
    controls = guarded("controls", [], lambda: step_controls(gate, setup)) or {"host_addresses": []}
    if controls.get("control") is None:
        gate.evidence.fail_assertion(
            "d-network-direct-refused", "the positive control could not run"
        )
    guarded("read", [k for k in REQUIRED if k.startswith("a-")], lambda: step_read(gate))
    guarded("write", [k for k in REQUIRED if k.startswith("b-")], lambda: step_write(gate))
    guarded("escape", [k for k in REQUIRED if k.startswith("c-escape")], lambda: step_escape(gate))
    guarded(
        "network",
        [k for k in REQUIRED if k.startswith("d-")],
        lambda: step_network(gate, setup, controls),
    )
    guarded("git", [k for k in REQUIRED if k.startswith("e-")], lambda: step_git(gate))
    relay_ids = [k for k in required_assertions(gate.options) if k.startswith("f-")]
    guarded("relay", relay_ids, lambda: step_relay(gate, setup))
    guarded("limits", [k for k in REQUIRED if k.startswith("g-")], lambda: step_limits(gate))
    guarded(
        "hooks",
        [
            "h-hooks-not-executed-on-host",
            "h-engine-install-unchanged",
            "h-validation-in-runner",
            "h-image-paths-read-only",
        ],
        lambda: step_hooks(gate),
    )
    import factory_gate_review as review

    guarded("review", list(review.REQUIRED), lambda: review.run(gate))
    guarded("admission", [k for k in REQUIRED if k.startswith("i-")], lambda: step_admission(gate))
    guarded(
        "container-profile",
        ["c-container-profile"],
        lambda: _record_container_profile(gate),
    )


def _record_container_profile(gate: Gate) -> None:
    probes = [a for a in gate.attempts if a.receipt is not None]
    flagged = {a.name: list(a.violations) for a in probes if a.violations}
    gate.evidence.assertion(
        "c-container-profile",
        passed=bool(probes) and not flagged,
        commands=[a.command_line for a in probes[:3]] + [f"... {len(probes)} attempts in total"],
        observed={
            "attempts_with_receipt": [a.name for a in probes],
            "violations": flagged,
            "example_effective_container": receipt_summary(probes[0].receipt) if probes else None,
        },
        expected="profile_violations() is empty for the engine-reported container of every attempt",
        observer={
            "source": "docker inspect read back by the launcher, stored in the runner receipt"
        },
        raw_artifacts=[a.receipt_ref for a in probes if a.receipt_ref],
    )


__all__ = ["REQUIRED", "finalize", "required_assertions", "run"]
