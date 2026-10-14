"""Boundary assertions that answer the review of the isolated runner (ids prefixed ``r-``).

Each assertion pairs a hostile attempt with a positive control (the same measurement on a case
where the unsafe thing is meant to happen, so a pass cannot be an artefact of a broken probe) and
an observation made on the *host*, outside the container: the host clone's object store, the
launcher's own outcome file, the engine's network and container lists, the bare origin's refs, a
stub upstream's request log, the host marker directory. What the worker itself reported is
recorded alongside but never decides a verdict on its own.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import secrets
import stat
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

from enginery.adapters.git_publication import GitBranchPublisher
from enginery.adapters.oci.launch import (
    container_name_for,
    create_arguments,
    effective_from_inspect,
    launch_violations,
    store_names_for,
    store_violations,
)
from enginery.adapters.oci.relay import RelayManager
from enginery.adapters.oci.validation import OciValidationRunner
from enginery.adapters.runner_launch import (
    LAUNCHER_MODULE,
    read_attempt_result,
    read_launch_outcome,
    require_exited,
    runner_receipt_path,
)
from enginery.application.branch_publication import (
    DEFAULT_PROTECTED_PATH_PATTERNS,
    BranchPublicationRequest,
    ProtectedPathPolicy,
)
from enginery.domain.errors import (
    EngineryError,
    HumanActionRequiredError,
    WorkerFailureError,
)
from enginery.domain.ids import OperationId
from enginery.domain.runner import (
    DISK_BOUND_KERNEL_VOLUME,
    NO_NETWORK,
    RELAY_HOSTNAME,
    RELAY_PORT,
    ResourceLimits,
    RunnerContext,
    RunnerProfile,
    RunnerSelection,
    expected_network,
    profile_violations,
)
from enginery.engine.recovery import RecoveryAssessment, assess_workspace_quiescence
from enginery.engine.task_repository import (
    intake_branch_bundle,
    intake_path_for,
    materialize_task_local_repository,
    validation_path_for,
    verify_intake_branch,
)
from enginery.engine.workspace import WORKSPACE_MODE_TASK_LOCAL_CLONE
from enginery.ledger.artifact_store import ArtifactStore
from factory_gate_context import Gate, receipt_summary
from factory_gate_support import (
    GATE_LABEL,
    AttemptResult,
    UpstreamStub,
    bare_refs,
    create_attempt_store,
    git,
    make_spec,
    new_task_workspace,
    remove_attempt_store,
)

REQUIRED: dict[str, str] = {
    "r-m1-no-host-objects-in-workspace": (
        "Canaries planted in the host repository as an unreachable blob, a stash, an unrelated "
        "branch and a tag never appear in the task workspace (object scan inside the container "
        "and on the host clone; a base-reachable object is found by the same scan)"
    ),
    "r-m2-symlink-result-refused": (
        "A result file the worker replaced with a symlink to a host canary is refused by the "
        "host collection (a naive read follows the link and returns the canary)"
    ),
    "r-m2-fifo-result-refused": (
        "A result file the worker replaced with a FIFO is refused by the host collection without "
        "blocking"
    ),
    "r-m2-stale-result-replay-refused": (
        "A result left by an earlier attempt, or replayed by the worker, is refused: it does not "
        "carry this launch's nonce; the launcher removed a result present before the start"
    ),
    "r-m2-exit-125-spoof-classified": (
        "A worker exiting 125 (or 124) is classified from the launcher's own outcome as an exited "
        "worker, while a genuine launcher refusal is classified as a refusal"
    ),
    "r-m3-container-log-bounded": (
        "A worker spamming stdout and stderr keeps the container log under the configured cap "
        "(an engine container without the cap keeps all of it)"
    ),
    "r-m3-disk-enospc-bytes": (
        "A worker writing past the workspace cap or past the result cap fails with ENOSPC inside "
        "the worker; no more than the cap is allocated and the launcher records an ordinary exit"
    ),
    "r-m3-disk-enospc-inodes": (
        "A worker creating entries past an inode cap fails with ENOSPC inside the worker and the "
        "entry count never exceeds the cap"
    ),
    "r-m3-disk-parallel-fallocate-capped": (
        "A burst of 8 parallel fallocate processes into a small workspace is capped by the "
        "kernel: the large allocations fail and at most the cap is allocated (statvfs inside the "
        "container and the bytes read back on the host)"
    ),
    "r-m3-disk-receipt-and-readback": (
        "The receipt records the kernel-capped-volume mechanism with the profile's byte and "
        "inode numbers; a symlink or FIFO planted in the result volume never reaches the host"
    ),
    "r-m3-memory-fill-exit-137": (
        "A worker filling a tmpfs volume past the memory limit ends in exit 137 recorded as an "
        "ordinary exit, not a launcher kill"
    ),
    "r-m3-git-bomb-refused-at-intake": (
        "A bundle whose tree holds more entries than the intake cap is refused by host "
        "verification for exactly that reason (the tree-cap refusal) before any host checkout; "
        "a small bundle from the same path is accepted"
    ),
    "r-m4-protected-path-held-then-published": (
        "A candidate editing .github/workflows/x.yml is held (changed paths read by the host from "
        "its verified repository), nothing is pushed to the bare origin, and the revision is "
        "published only after the explicit approval; an ordinary candidate is not held"
    ),
    "r-m5-validation-and-none-mode-cannot-reach-relay": (
        "While the relay is up, a validation container and a model_access=none worker cannot "
        "reach it (by name or address) and the stub upstream sees no request from them; a "
        "model-access worker reaches it"
    ),
    "r-m5-concurrent-workers-isolated-networks": (
        "Two concurrent model-access workers on per-attempt networks cannot reach each other "
        "(a container on the same network can), and every per-attempt network is removed "
        "afterwards"
    ),
    "r-m4-hooks-disabled-in-image": (
        "In the real image the worker environment carries the command-scoped hook-disabling git "
        "configuration: a planted pre-commit hook is not run by git commit, and the same hook "
        "runs when that configuration is absent"
    ),
    "r-b1-fsmonitor-not-run-on-host": (
        "A core.fsmonitor the worker planted in its clone never runs on the host through "
        "recovery, cancel and failure handling (git run in the clone executes it)"
    ),
    "r-m7-weaker-container-flagged": (
        "Containers created weaker (seccomp, apparmor, label, systempaths, cgroup namespace, "
        "extra privilege) are flagged by profile_violations or launch_violations; the product's "
        "own container is not"
    ),
}


# --- helpers ---------------------------------------------------------------------------------


def _assert(
    gate: Gate,
    assertion_id: str,
    *,
    passed: bool,
    observed: Any,
    expected: str,
    commands: list[str],
    observer: Any = None,
    attempts: tuple[AttemptResult, ...] = (),
) -> None:
    raw = [ref for attempt in attempts for ref in gate.raw_refs(attempt)]
    gate.evidence.assertion(
        assertion_id,
        passed=bool(passed),
        commands=commands,
        observed=observed,
        expected=expected,
        observer=observer or {},
        raw_artifacts=raw,
    )


def _attempt_commands(gate: Gate, *attempts: AttemptResult) -> list[str]:
    return [gate.evidence_commands(attempt)[0] for attempt in attempts]


def _launched_clean(attempt: AttemptResult) -> bool:
    """The attempt ran under the launcher, recorded a clean receipt and the container is gone."""
    return (
        attempt.receipt is not None
        and not attempt.violations
        and attempt.container_gone
        and not (attempt.observer or {}).get("host_protected_changes")
    )


def _outcome_state(attempt: AttemptResult) -> str | None:
    state = (attempt.outcome or {}).get("state")
    return state if isinstance(state, str) else None


def _runner_context(gate: Gate, profile: RunnerProfile | None = None) -> RunnerContext:
    fixture = gate.fixture
    assert fixture is not None
    profile = profile or gate.profile
    return RunnerContext(
        selection=RunnerSelection.isolated(
            image=gate.runner_image.image_id, profile=profile, docker_context=gate.docker_context
        ),
        profile=profile,
        receipt_root=str(fixture.receipts),
    )


def _collect(
    gate: Gate, attempt: AttemptResult, *, profile: RunnerProfile | None = None
) -> dict[str, Any]:
    """The product's own collection of one attempt's result (outcome, bounded read, nonce)."""
    runner = _runner_context(gate, profile)
    task = types.SimpleNamespace(operation_id=attempt.operation_id)
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            text = read_attempt_result(
                runner,
                task,  # type: ignore[arg-type]
                attempt.out / "result.json",
                what="gate worker",
            )
            box.update(accepted=True, result_bytes=len(text))
        except EngineryError as error:
            box.update(
                accepted=False,
                error_type=type(error).__name__,
                message=str(error)[:240],
                details={k: str(v)[:200] for k, v in dict(error.details or {}).items()},
            )
        except Exception as error:  # a hang or crash of the collection is itself a failure
            box.update(accepted=None, error_type=type(error).__name__, message=str(error)[:240])

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(30)
    if thread.is_alive():
        return {"accepted": None, "error_type": "collection-hang"}
    return box


def _classify_outcome(gate: Gate, attempt: AttemptResult) -> dict[str, Any]:
    """The product's ``read_launch_outcome`` + ``require_exited`` for one attempt."""
    runner = _runner_context(gate)
    verdict: dict[str, Any] = {}
    try:
        outcome = read_launch_outcome(
            runner_receipt_path(runner, attempt.operation_id),
            operation_id=attempt.operation_id,
            what="gate worker",
        )
        verdict["outcome_state"] = outcome.state.value
        verdict["outcome_exit_code"] = outcome.exit_code
        try:
            verdict["required_exit_code"] = require_exited(outcome, what="gate worker")
            verdict["classified_as"] = "exited"
        except HumanActionRequiredError as error:
            verdict["classified_as"] = "held"
            verdict["message"] = str(error)[:200]
        except WorkerFailureError as error:
            verdict["classified_as"] = "refused-or-failed"
            verdict["message"] = str(error)[:200]
    except EngineryError as error:
        verdict["classified_as"] = "no-outcome"
        verdict["message"] = str(error)[:200]
    return verdict


def _make_repo(gate: Gate, name: str, files: dict[str, str]) -> tuple[Path, str]:
    root = gate.workroot / "review" / name
    root.mkdir(parents=True)
    git(["init", "-q", "--initial-branch=main"], cwd=root)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(["add", "-A"], cwd=root)
    git(["commit", "-q", "-m", "base"], cwd=root)
    return root, git(["rev-parse", "HEAD"], cwd=root).stdout.strip()


def _workspace_of(gate: Gate, repo: Path, base: str, name: str) -> Path:
    assert gate.fixture is not None
    workspace = gate.fixture.work / name / "ws"
    materialize_task_local_repository(
        repository_path=repo, workspace_path=workspace, base_revision=base
    )
    return workspace


def _engine_json(gate: Gate, *arguments: str) -> Any:
    completed = gate.engine_cli(*arguments)
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except ValueError:
        return None


def _container_ip(gate: Gate, container: str, network: str) -> str | None:
    document = _engine_json(gate, "inspect", container)
    try:
        return str(document[0]["NetworkSettings"]["Networks"][network]["IPAddress"]) or None
    except (KeyError, IndexError, TypeError):
        return None


def _network_members(gate: Gate, network: str) -> list[str] | None:
    info = gate.engine.network_info(network)
    return None if info is None else list(info.containers)


# --- M1: the host object store never reaches the workspace -----------------------------------


def step_m1(gate: Gate) -> None:
    secret = f"gatecanary-objects-{secrets.token_hex(8)}"
    repo, base = _make_repo(gate, "m1-repo", {"README": "base file\n"})
    control = git(["rev-parse", f"{base}:README"], cwd=repo).stdout.strip()
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=f"{secret}-unreachable\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / "README").write_text(f"{secret}-stash\n", encoding="utf-8")
    git(["stash", "push", "-q", "-m", f"{secret}-stash"], cwd=repo)
    stash_commit = git(["rev-parse", "refs/stash"], cwd=repo).stdout.strip()
    stash_blob = git(["rev-parse", "refs/stash:README"], cwd=repo).stdout.strip()
    git(["checkout", "-q", "-b", "unrelated-canary"], cwd=repo)
    (repo / "unrelated.txt").write_text(f"{secret}-branch\n", encoding="utf-8")
    git(["add", "unrelated.txt"], cwd=repo)
    git(["commit", "-q", "-m", f"{secret}-branch"], cwd=repo)
    branch_commit = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    branch_blob = git(["rev-parse", "HEAD:unrelated.txt"], cwd=repo).stdout.strip()
    git(["tag", "-a", "canary-tag", "-m", f"{secret}-tag", branch_commit], cwd=repo)
    tag_object = git(["rev-parse", "refs/tags/canary-tag"], cwd=repo).stdout.strip()
    git(["checkout", "-q", "main"], cwd=repo)
    canaries = {
        "unreachable_blob": blob,
        "stash_commit": stash_commit,
        "stash_blob": stash_blob,
        "unrelated_branch_commit": branch_commit,
        "unrelated_branch_blob": branch_blob,
        "tag_object": tag_object,
    }
    in_source = {
        label: git(["cat-file", "-e", oid], cwd=repo, check=False).returncode == 0
        for label, oid in canaries.items()
    }
    reachable = {
        line.split()[0]
        for line in git(["rev-list", "--objects", base], cwd=repo).stdout.splitlines()
        if line.strip()
    }
    workspace = _workspace_of(gate, repo, base, "m1-objects")
    git_dir = workspace / ".git"

    def host_has(oid: str) -> bool:
        return git(["--git-dir", str(git_dir), "cat-file", "-e", oid], check=False).returncode == 0

    host_present = {label: host_has(oid) for label, oid in canaries.items()}
    host_control = host_has(control)
    host_objects = {
        line.split()[0]
        for line in git(
            ["--git-dir", str(git_dir), "cat-file", "--batch-all-objects", "--batch-check"]
        ).stdout.splitlines()
        if line.strip()
    }
    host_refs = git(
        ["--git-dir", str(git_dir), "for-each-ref", "--format=%(refname)"]
    ).stdout.split()
    alternates = (git_dir / "objects" / "info" / "alternates").exists()
    attempt = gate.attempt(
        "m1-objects",
        "objects",
        {"oids": {**canaries, "control_base_blob": control}},
        workspace=workspace,
    )
    obs = attempt.obs or {}
    inside = obs.get("oids_present", {})
    inside_objects = set(obs.get("object_ids", []))
    unreachable_report = " ".join(obs.get("unreachable_report", []))
    passed = (
        all(in_source.values())  # control: the planting worked
        and host_control
        and inside.get("control_base_blob") is True  # control: the scans find a base object
        and not any(host_present.values())
        and not any(inside.get(label) for label in canaries)
        and host_objects <= reachable
        and inside_objects <= reachable
        and not alternates
        and not obs.get("alternates_present")
        and obs.get("tags") == []
        and obs.get("stash_list") == ""
        and not any("canary" in ref or "stash" in ref for ref in obs.get("refs", []))
        and not any("canary" in ref or "stash" in ref for ref in host_refs)
        and not any(oid in unreachable_report for oid in canaries.values())
        and _launched_clean(attempt)
    )
    _assert(
        gate,
        "r-m1-no-host-objects-in-workspace",
        passed=passed,
        commands=[
            "git hash-object -w / stash push / branch+commit / tag -a  (in the host repo)",
            "materialize_task_local_repository(repository_path=<host repo>, workspace_path=<ws>, "
            "base_revision=<base oid>)",
            "git --git-dir <ws>/.git cat-file -e <oid>   (host)",
            *_attempt_commands(gate, attempt),
        ],
        observed={
            "canaries_in_host_repo_control": in_source,
            "canaries_in_host_clone": host_present,
            "canaries_in_container_clone": {label: inside.get(label) for label in canaries},
            "base_object_found_on_host_clone": host_control,
            "base_object_found_in_container": inside.get("control_base_blob"),
            "host_clone_object_count": len(host_objects),
            "host_clone_objects_outside_base_reachable_set": sorted(host_objects - reachable)[:10],
            "container_clone_objects_outside_base_reachable_set": sorted(
                inside_objects - reachable
            )[:10],
            "alternates_file_on_host_clone": alternates,
            "container_refs": obs.get("refs"),
            "host_clone_refs": host_refs,
            "container_tags": obs.get("tags"),
            "container_stash_list": obs.get("stash_list"),
            "worker_receipt": receipt_summary(attempt.receipt),
        },
        expected=(
            "no canary object in the workspace on the host or in the container, only objects "
            "reachable from the base, no alternates, no stash, tag or extra branch"
        ),
        observer={
            "source": "host: git --git-dir <ws>/.git cat-file on the clone; container: the probe's "
            "own git cat-file/fsck (in-container)",
        },
        attempts=(attempt,),
    )


# --- M2: result collection ---------------------------------------------------------------------


_FORGE_CANARY_FILE = "/out/gate-forge-canary.txt"


def _forged_attempt(gate: Gate, name: str, kind: str, legit: AttemptResult) -> AttemptResult:
    """A worker that plants a hostile ``result.json`` in its own result volume and kills the
    supervisor, so nothing overwrites it. Nothing is staged on the host: the result directory
    on the host starts empty and only what the launcher chooses to copy out can appear in it."""
    assert gate.fixture is not None
    workspace = new_task_workspace(gate.fixture, name)
    stale = (legit.out / "result.json").read_text(encoding="utf-8")
    out = Path(str(workspace) + ".out")
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(stale, encoding="utf-8")  # present before the launch
    params: dict[str, Any] = {
        "kind": kind,
        "target": _FORGE_CANARY_FILE,
        "canary": _forge_canary(gate),
        "stale_result": stale,
    }
    return gate.attempt(
        name, "forge", params, workspace=workspace, product_receipt_path=True, time_budget=60
    )


def _forge_canary(gate: Gate) -> str:
    return f"gate-forge-canary-{gate.rid}"


def _tree_contains(root: Path, needle: str) -> bool:
    """Whether any regular file under ``root`` holds ``needle`` (never following a link)."""
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if needle.encode() in path.read_bytes():
                return True
        except OSError:
            continue
    return False


def _host_plant(path: Path, kind: str, target: Path | None = None) -> None:
    """The host-side control: the same hostile file the worker planted, on the host path."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    if kind == "symlink":
        assert target is not None
        os.symlink(target, path)
    else:
        os.mkfifo(path)


def step_m2(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None
    legit = gate.attempt("m2-legit", "session", {}, product_receipt_path=True)
    legit_collect = _collect(gate, legit)
    canary = fixture.canaries["artifact"]
    commands_collect = [
        "read_attempt_result(runner, task, <out>/result.json)  # the product's collection",
        "read_launch_outcome(...) + require_exited(...)",
    ]

    # symlink -------------------------------------------------------------------------------
    symlink = _forged_attempt(gate, "m2-symlink", "symlink", legit)
    worker_canary = _forge_canary(gate)
    # The host path held a stale result before the launch: nothing is left there afterwards, so
    # the launcher removed it and the worker's link was not copied in its place.
    reached_host = os.path.lexists(symlink.out / "result.json")
    canary_on_host = _tree_contains(symlink.out, worker_canary) or _tree_contains(
        symlink.workspace, worker_canary
    )
    launcher_refused_link = "is not a regular file" in symlink.launcher_stderr
    # Control: the same link on the host path is real (a naive read follows it to the canary)
    # and the product's own bounded read still refuses it.
    link_path = symlink.out / "result.json"
    _host_plant(link_path, "symlink", canary.path)
    link_is_symlink = link_path.is_symlink()
    try:
        naive = link_path.read_text(encoding="utf-8")
    except OSError:
        naive = ""
    naive_follows = canary.value in naive
    collected = _collect(gate, symlink)
    leaked = canary.value in json.dumps(collected)
    link_path.unlink()
    _assert(
        gate,
        "r-m2-symlink-result-refused",
        passed=legit_collect.get("accepted") is True
        and (symlink.obs or {}).get("kind") == "symlink"
        and not reached_host
        and not canary_on_host
        and launcher_refused_link
        and link_is_symlink
        and naive_follows
        and collected.get("accepted") is False
        and not leaked
        and _outcome_state(symlink) == "exited",
        commands=[*_attempt_commands(gate, symlink), *commands_collect],
        observed={
            "worker_planted": (symlink.obs or {}).get("kind"),
            "result_path_exists_on_host_after_the_attempt": reached_host,
            "worker_canary_found_on_host": canary_on_host,
            "launcher_logged_not_a_regular_file": launcher_refused_link,
            "control_host_path_is_symlink": link_is_symlink,
            "control_naive_open_follows_to_canary": naive_follows,
            "control_collection_of_the_host_symlink": collected,
            "canary_in_collection_output": leaked,
            "control_legit_attempt_collection": legit_collect,
            "launcher_outcome": symlink.outcome,
        },
        expected=(
            "a symlink the worker planted as its result never reaches the host (the launcher "
            "copies regular files only) and no canary content is read; the product's bounded "
            "read also refuses a symlink on the host path"
        ),
        observer={
            "source": "host: lstat of the result path, a tree search, the launcher's own log, "
            "and the product's read_attempt_result on a host symlink (control)"
        },
        attempts=(legit, symlink),
    )

    # FIFO ----------------------------------------------------------------------------------
    fifo = _forged_attempt(gate, "m2-fifo", "fifo", legit)
    fifo_reached_host = os.path.lexists(fifo.out / "result.json")
    fifo_launcher_refused = "is not a regular file" in fifo.launcher_stderr
    fifo_path = fifo.out / "result.json"
    _host_plant(fifo_path, "fifo")
    try:
        is_fifo = stat.S_ISFIFO(fifo_path.lstat().st_mode)
    except OSError:
        is_fifo = False
    started = time.monotonic()
    collected = _collect(gate, fifo)
    elapsed = time.monotonic() - started
    fifo_path.unlink()
    _assert(
        gate,
        "r-m2-fifo-result-refused",
        passed=(fifo.obs or {}).get("kind") == "fifo"
        and not fifo_reached_host
        and fifo_launcher_refused
        and not fifo.timed_out
        and is_fifo
        and collected.get("accepted") is False
        and collected.get("error_type") != "collection-hang"
        and elapsed < 20
        and _outcome_state(fifo) == "exited",
        commands=[*_attempt_commands(gate, fifo), *commands_collect],
        observed={
            "worker_planted": (fifo.obs or {}).get("kind"),
            "result_path_exists_on_host_after_the_attempt": fifo_reached_host,
            "launcher_logged_not_a_regular_file": fifo_launcher_refused,
            "launcher_returned_without_blocking": not fifo.timed_out,
            "control_host_path_is_fifo": is_fifo,
            "control_collection_of_the_host_fifo": collected,
            "collection_seconds": round(elapsed, 2),
            "launcher_outcome": fifo.outcome,
        },
        expected=(
            "a FIFO the worker planted as its result never reaches the host and does not block "
            "the launcher; the product's bounded read also refuses a FIFO promptly"
        ),
        observer={
            "source": "host: lstat of the result path, the launcher's own log and the product's "
            "bounded read of a host FIFO (control)"
        },
        attempts=(fifo,),
    )

    # replay --------------------------------------------------------------------------------
    replay = _forged_attempt(gate, "m2-replay", "replay", legit)
    old_nonce = _nonce_of(legit.out / "result.json")
    replayed_nonce = _nonce_of(replay.out / "result.json")
    launch_nonce = (replay.outcome or {}).get("nonce")
    collected = _collect(gate, replay)
    _assert(
        gate,
        "r-m2-stale-result-replay-refused",
        passed=old_nonce is not None
        and old_nonce == replayed_nonce
        and launch_nonce is not None
        and old_nonce != launch_nonce
        and (replay.obs or {}).get("result_existed_at_start") is False
        and collected.get("accepted") is False
        and "not bound" in str(collected.get("message", ""))
        and legit_collect.get("accepted") is True,
        commands=[*_attempt_commands(gate, replay), *commands_collect],
        observed={
            "stale_result_nonce_matches_earlier_attempt": old_nonce == replayed_nonce,
            "replayed_nonce_differs_from_launch_nonce": old_nonce != launch_nonce,
            "result_present_in_container_at_start": (replay.obs or {}).get(
                "result_existed_at_start"
            ),
            "worker_could_read_supervisor_nonce": (replay.obs or {}).get(
                "supervisor_nonce_readable_from_worker"
            ),
            "collection": collected,
            "control_legit_attempt_collection": legit_collect,
        },
        expected=(
            "the replayed result carries another launch's nonce and is refused; a result "
            "present before the start was removed by the launcher"
        ),
        observer={"source": "host: nonces read from the files and the launcher outcome file"},
        attempts=(legit, replay),
    )

    # exit 125 / 124 spoof ------------------------------------------------------------------
    spoof125 = gate.attempt(
        "m2-spoof125",
        "none",
        {},
        inner=["sh", "-c", "exit 125"],
        product_receipt_path=True,
        time_budget=30,
    )
    spoof124 = gate.attempt(
        "m2-spoof124",
        "none",
        {},
        inner=["sh", "-c", "exit 124"],
        product_receipt_path=True,
        time_budget=30,
    )
    refused = gate.attempt(
        "m2-refused",
        "none",
        {},
        inner=["true"],
        spec_overrides={"image": "sha256:" + secrets.token_hex(32)},
        product_receipt_path=True,
        time_budget=30,
    )
    verdicts = {
        "worker_exit_125": _classify_outcome(gate, spoof125),
        "worker_exit_124": _classify_outcome(gate, spoof124),
        "genuine_launcher_refusal": _classify_outcome(gate, refused),
    }
    _assert(
        gate,
        "r-m2-exit-125-spoof-classified",
        passed=spoof125.exit_code == 125
        and verdicts["worker_exit_125"].get("classified_as") == "exited"
        and verdicts["worker_exit_125"].get("required_exit_code") == 125
        and spoof124.exit_code == 124
        and verdicts["worker_exit_124"].get("classified_as") == "exited"
        and verdicts["worker_exit_124"].get("required_exit_code") == 124
        and refused.exit_code == 125
        and verdicts["genuine_launcher_refusal"].get("classified_as") == "refused-or-failed"
        and verdicts["genuine_launcher_refusal"].get("outcome_state") in ("refused", "faulted"),
        commands=[
            *_attempt_commands(gate, spoof125, spoof124, refused),
            "read_launch_outcome(...) + require_exited(...)",
        ],
        observed={
            "launcher_process_exit_codes": {
                "worker_exit_125": spoof125.exit_code,
                "worker_exit_124": spoof124.exit_code,
                "genuine_launcher_refusal": refused.exit_code,
            },
            "classified_from_the_launcher_outcome": verdicts,
        },
        expected=(
            "identical launcher exit status 125, yet the worker's exit is classified as an "
            "exited container and only the genuine refusal as a refusal"
        ),
        observer={"launcher_outcomes": {"125": spoof125.outcome, "124": spoof124.outcome}},
        attempts=(spoof125, spoof124, refused),
    )


def _nonce_of(path: Path) -> str | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    nonce = document.get("nonce") if isinstance(document, dict) else None
    return nonce if isinstance(nonce, str) else None


# --- M3: log, disk and git-bomb bounds ----------------------------------------------------------

_LOG_SPAM_MIB = 48
_LOG_CAP_SLACK_BYTES = 4 * 1024 * 1024


def _log_sampler(gate: Gate, container: str, stop: threading.Event, samples: list[int]) -> None:
    while not stop.is_set():
        completed = gate.engine_cli("logs", container, timeout=60)
        if completed.returncode == 0:
            samples.append(len(completed.stdout) + len(completed.stderr))
        stop.wait(0.4)


def step_m3_log(gate: Gate) -> None:
    name = "m3-log"
    operation_id = f"gate:{gate.rid}:{name}"
    container = container_name_for(operation_id)
    samples: list[int] = []
    stop = threading.Event()
    sampler = threading.Thread(target=_log_sampler, args=(gate, container, stop, samples))
    sampler.start()
    try:
        attempt = gate.attempt(name, "spam", {"mib": _LOG_SPAM_MIB}, time_budget=300)
    finally:
        stop.set()
        sampler.join(60)
    peak = max(samples, default=0)
    # Control: an engine container created without the cap keeps every byte it wrote.
    control_name = f"enginery-gate-{gate.rid}-logctl"
    gate.track_container(control_name)
    control_mib = 8
    created = gate.engine_cli(
        "run",
        "-d",
        "--name",
        control_name,
        "--label",
        f"{GATE_LABEL}={gate.rid}",
        "--network",
        "none",
        gate.runner_image.image_id,
        "python3",
        "-c",
        f"import sys\nfor _ in range({control_mib * 1024}): print('x' * 1023)",
        timeout=120,
    )
    control_bytes = -1
    if created.returncode == 0:
        gate.engine_cli("wait", control_name, timeout=120)
        logs = gate.engine_cli("logs", control_name, timeout=120)
        control_bytes = len(logs.stdout) + len(logs.stderr)
    gate.engine.remove(control_name, force=True)
    bounds = None if attempt.receipt is None else attempt.receipt.bounds
    cap = (
        None
        if bounds is None
        else {
            "driver": bounds.log_driver,
            "max_size": bounds.log_max_size,
            "max_file": bounds.log_max_file,
        }
    )
    _assert(
        gate,
        "r-m3-container-log-bounded",
        passed=_launched_clean(attempt)
        and attempt.exit_code == 0
        and len(samples) >= 3
        and peak > 256 * 1024
        and peak <= _LOG_CAP_SLACK_BYTES
        and control_bytes >= control_mib * 1024 * 1000
        and cap is not None
        and len(attempt.launcher_stderr) < 1024 * 1024,
        commands=[
            *_attempt_commands(gate, attempt),
            f"docker logs {container}  # sampled every 0.4s while the worker ran",
            "docker run -d (no log options) python3 -c <8 MiB of output>; docker logs",
        ],
        observed={
            "worker_wrote_mib_to_stdout_and_stderr_each": _LOG_SPAM_MIB,
            "host_log_samples": len(samples),
            "peak_container_log_bytes_seen_by_the_engine": peak,
            "allowed_bytes_with_slack": _LOG_CAP_SLACK_BYTES,
            "configured_log_cap_in_receipt": cap,
            "control_container_without_cap_log_bytes": control_bytes,
            "launcher_stderr_bytes": len(attempt.launcher_stderr),
        },
        expected="the engine-side container log never exceeds the configured cap",
        observer={"source": "host: docker logs read from the engine while the worker ran"},
        attempts=(attempt,),
    )


_MIB = 1024 * 1024
_SMALL_WORKSPACE_MIB = 16
_SMALL_OUT_MIB = 8
_SMALL_WORKSPACE_INODES = 4000
_SMALL_OUT_INODES = 64
_BURST_PROCESSES = 8
_BURST_BIG_MIB = 64
_BURST_CHUNK_MIB = 2


def _small_profile(gate: Gate) -> RunnerProfile:
    """The gate profile with small capped volumes, so the caps are reached in seconds."""
    return RunnerProfile(
        network=gate.internal_network,
        resources=ResourceLimits(
            memory_mib=256,
            cpus=1.0,
            pids=64,
            tmp_mib=32,
            home_mib=16,
            stop_grace_seconds=2,
            workspace_mib=_SMALL_WORKSPACE_MIB,
            out_mib=_SMALL_OUT_MIB,
            workspace_inodes=_SMALL_WORKSPACE_INODES,
            out_inodes=_SMALL_OUT_INODES,
        ),
    )


def _host_tree(root: Path) -> dict[str, int]:
    """Regular-file bytes and entries of a tree the launcher read back to the host."""
    total = entries = 0
    if not root.exists():
        return {"bytes": 0, "entries": 0}
    for path in root.rglob("*"):
        entries += 1
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return {"bytes": total, "entries": entries}


def _ran_to_completion(attempt: AttemptResult, *, exit_code: int = 0) -> bool:
    """The worker exited on its own (the launcher's outcome says ``exited``) and nothing killed
    it: no deadline, no launcher signal."""
    return (
        _outcome_state(attempt) == "exited"
        and (attempt.outcome or {}).get("exit_code") == exit_code
        and attempt.exit_code == exit_code
        and not attempt.timed_out
        and attempt.container_gone
    )


def _kernel_cap_bounds(attempt: AttemptResult, profile: RunnerProfile) -> dict[str, Any]:
    """What the receipt's resource bounds record, beside the numbers the profile asked for."""
    limits = profile.resources
    bounds = None if attempt.receipt is None else attempt.receipt.bounds
    recorded = None if bounds is None else bounds.to_state()
    return {
        "recorded": recorded,
        "expected": {
            "disk_mechanism": DISK_BOUND_KERNEL_VOLUME,
            "workspace_bytes": limits.workspace_mib * _MIB,
            "out_bytes": limits.out_mib * _MIB,
            "workspace_inodes": limits.workspace_inodes,
            "out_inodes": limits.out_inodes,
        },
        "matches": bounds is not None
        and bounds.disk_mechanism == DISK_BOUND_KERNEL_VOLUME
        and bounds.workspace_bytes == limits.workspace_mib * _MIB
        and bounds.out_bytes == limits.out_mib * _MIB
        and bounds.workspace_inodes == limits.workspace_inodes
        and bounds.out_inodes == limits.out_inodes
        and bounds.poll_seconds is None,
    }


def step_m3_disk(gate: Gate) -> None:
    """The disk bounds are the kernel's: a worker that outgrows a capped volume gets ``ENOSPC``.

    Small capped volumes (16 MiB workspace, 8 MiB result, 4000 / 64 inodes) are exercised on
    whichever engine the gate runs on. A worker writes past each cap, creates entries past each
    inode cap and runs a parallel ``fallocate`` burst; in every case the worker itself sees the
    refusal, the launcher kills nothing (outcome ``exited``), the volume statistics read inside
    the container stay within the cap, and the tree the launcher reads back to the host does too.
    """
    profile = _small_profile(gate)
    cap_bytes = _SMALL_WORKSPACE_MIB * _MIB
    out_cap_bytes = _SMALL_OUT_MIB * _MIB

    ws_bytes = gate.attempt(
        "m3-disk-ws-bytes",
        "disk",
        {"kind": "bytes", "where": "/workspace", "max_mib": 4 * _SMALL_WORKSPACE_MIB},
        profile=profile,
        time_budget=120,
    )
    out_bytes = gate.attempt(
        "m3-disk-out-bytes",
        "disk",
        {
            "kind": "bytes",
            "where": "/out",
            "max_mib": 4 * _SMALL_OUT_MIB,
            "chunk_kib": 256,
            "plant_links": True,
            "cleanup": True,
        },
        profile=profile,
        product_receipt_path=True,
        time_budget=120,
    )
    out_bytes_collect = _collect(gate, out_bytes, profile=profile)
    ws_entries = gate.attempt(
        "m3-disk-ws-entries",
        "disk",
        {"kind": "entries", "where": "/workspace", "max_entries": 5 * _SMALL_WORKSPACE_INODES},
        profile=profile,
        time_budget=120,
    )
    out_entries = gate.attempt(
        "m3-disk-out-entries",
        "disk",
        {
            "kind": "entries",
            "where": "/out",
            "max_entries": 5 * _SMALL_OUT_INODES,
            "cleanup": True,
        },
        profile=profile,
        time_budget=120,
    )
    burst = gate.attempt(
        "m3-disk-burst",
        "disk",
        {
            "kind": "burst",
            "where": "/workspace",
            "procs": _BURST_PROCESSES,
            "big_mib": _BURST_BIG_MIB,
            "chunk_mib": _BURST_CHUNK_MIB,
            "max_chunks": 4 * _SMALL_WORKSPACE_MIB // _BURST_CHUNK_MIB,
        },
        profile=profile,
        time_budget=180,
    )
    memory_profile = RunnerProfile(
        network=gate.internal_network,
        resources=ResourceLimits(
            memory_mib=128,
            cpus=1.0,
            pids=64,
            tmp_mib=32,
            home_mib=16,
            stop_grace_seconds=2,
            workspace_mib=512,
            out_mib=16,
        ),
    )
    memory = gate.attempt(
        "m3-disk-memory-fill",
        "disk",
        {"kind": "bytes", "where": "/workspace", "max_mib": 400, "chunk_kib": 1024},
        profile=memory_profile,
        time_budget=120,
    )

    def seen(attempt: AttemptResult, key: str) -> dict[str, Any]:
        value = (attempt.obs or {}).get(key)
        return value if isinstance(value, dict) else {}

    # --- bytes: ENOSPC inside the worker, within both caps, nothing killed -------------------
    ws_fill, ws_after = seen(ws_bytes, "fill"), seen(ws_bytes, "volume_after")
    out_fill, out_after = seen(out_bytes, "fill"), seen(out_bytes, "volume_after")
    ws_host = _host_tree(ws_bytes.workspace / "gate-fill")
    out_listing = sorted(path.name for path in out_bytes.out.iterdir())
    _assert(
        gate,
        "r-m3-disk-enospc-bytes",
        passed=_launched_clean(ws_bytes)
        and _launched_clean(out_bytes)
        and _ran_to_completion(ws_bytes)
        and _ran_to_completion(out_bytes)
        and ws_fill.get("failure") == "ENOSPC"
        and out_fill.get("failure") == "ENOSPC"
        and ws_after.get("total_bytes") == cap_bytes
        and out_after.get("total_bytes") == out_cap_bytes
        and 0 < ws_fill.get("bytes_written", 0) <= cap_bytes
        and 0 < out_fill.get("bytes_written", 0) <= out_cap_bytes
        and ws_after.get("used_bytes", cap_bytes + 1) <= cap_bytes
        and out_after.get("used_bytes", out_cap_bytes + 1) <= out_cap_bytes
        and ws_host["bytes"] == seen(ws_bytes, "fill_tree").get("apparent_bytes")
        and ws_host["bytes"] <= cap_bytes
        and out_bytes_collect.get("accepted") is True,
        commands=[
            *_attempt_commands(gate, ws_bytes, out_bytes),
            "[in container] write 512 KiB files into /workspace/gate-fill until the first error",
            "[in container] write 256 KiB files into /out/gate-fill until the first error",
            "[in container] os.statvfs(/workspace) ; os.statvfs(/out)",
            "read_attempt_result(...)  # the result is still written after the worker freed /out",
        ],
        observed={
            "workspace": {
                "cap_bytes": cap_bytes,
                "worker_error": ws_fill.get("failure"),
                "bytes_written_by_worker": ws_fill.get("bytes_written"),
                "volume_after_in_container": ws_after,
                "bytes_read_back_on_host": ws_host,
                "launcher_outcome": ws_bytes.outcome,
            },
            "result_volume": {
                "cap_bytes": out_cap_bytes,
                "worker_error": out_fill.get("failure"),
                "bytes_written_by_worker": out_fill.get("bytes_written"),
                "volume_after_in_container": out_after,
                "result_collected_by_the_product": out_bytes_collect,
                "launcher_outcome": out_bytes.outcome,
            },
            "launcher_killed_nothing": _ran_to_completion(ws_bytes)
            and _ran_to_completion(out_bytes),
        },
        expected=(
            "a write past the workspace cap and past the result cap fails with ENOSPC inside the "
            "worker, no more than the cap is allocated (volume statistics in the container and "
            "the bytes read back on the host), and the launcher records an ordinary exit"
        ),
        observer={
            "source": "container: statvfs of the capped volumes and the worker's own errno; "
            "host: the launcher outcome file and the workspace read back"
        },
        attempts=(ws_bytes, out_bytes),
    )

    # --- inodes: ENOSPC for an entry burst, entries within the inode cap ----------------------
    ws_entry_fill, ws_entry_after = seen(ws_entries, "fill"), seen(ws_entries, "volume_after")
    out_entry_fill, out_entry_after = seen(out_entries, "fill"), seen(out_entries, "volume_after")
    ws_entry_host = _host_tree(ws_entries.workspace)
    _assert(
        gate,
        "r-m3-disk-enospc-inodes",
        passed=_launched_clean(ws_entries)
        and _launched_clean(out_entries)
        and _ran_to_completion(ws_entries)
        and _ran_to_completion(out_entries)
        and ws_entry_fill.get("failure") == "ENOSPC"
        and out_entry_fill.get("failure") == "ENOSPC"
        and ws_entry_after.get("inodes_total") == _SMALL_WORKSPACE_INODES
        and out_entry_after.get("inodes_total") == _SMALL_OUT_INODES
        and 0 < ws_entry_fill.get("entries_created", 0) <= _SMALL_WORKSPACE_INODES
        and 0 < out_entry_fill.get("entries_created", 0) <= _SMALL_OUT_INODES
        and ws_entry_after.get("inodes_used", _SMALL_WORKSPACE_INODES + 1)
        <= _SMALL_WORKSPACE_INODES
        and out_entry_after.get("inodes_used", _SMALL_OUT_INODES + 1) <= _SMALL_OUT_INODES
        and ws_entry_host["entries"] <= _SMALL_WORKSPACE_INODES,
        commands=[
            *_attempt_commands(gate, ws_entries, out_entries),
            "[in container] create empty files in /workspace/gate-fill until the first error",
            "[in container] create empty files in /out/gate-fill until the first error",
            "[in container] os.statvfs(...).f_files / f_ffree",
        ],
        observed={
            "workspace": {
                "inode_cap": _SMALL_WORKSPACE_INODES,
                "worker_error": ws_entry_fill.get("failure"),
                "entries_created_by_worker": ws_entry_fill.get("entries_created"),
                "volume_after_in_container": ws_entry_after,
                "entries_read_back_on_host": ws_entry_host["entries"],
            },
            "result_volume": {
                "inode_cap": _SMALL_OUT_INODES,
                "worker_error": out_entry_fill.get("failure"),
                "entries_created_by_worker": out_entry_fill.get("entries_created"),
                "volume_after_in_container": out_entry_after,
            },
            "launcher_outcomes": [ws_entries.outcome, out_entries.outcome],
        },
        expected=(
            "an entry burst past each inode cap fails with ENOSPC inside the worker, the entry "
            "count never exceeds the cap, and the launcher records an ordinary exit"
        ),
        observer={
            "source": "container: statvfs inode counts and the worker's own errno; host: the "
            "launcher outcome file and the workspace read back"
        },
        attempts=(ws_entries, out_entries),
    )

    # --- a parallel fallocate burst is capped ---------------------------------------------------
    reports = list((burst.obs or {}).get("fill", {}).get("reports", []))
    burst_after = seen(burst, "volume_after")
    burst_tree = seen(burst, "fill_tree")
    burst_host = _host_tree(burst.workspace / "gate-fill")
    big_all_failed = len(reports) == _BURST_PROCESSES and all(
        (report.get("big_fallocate") or {}).get("exit") not in (0, None) for report in reports
    )
    chunked_total = sum(report.get("chunks_allocated", 0) for report in reports) * _BURST_CHUNK_MIB
    all_refused = all(report.get("failure") == "ENOSPC" for report in reports)
    _assert(
        gate,
        "r-m3-disk-parallel-fallocate-capped",
        passed=_launched_clean(burst)
        and _ran_to_completion(burst)
        and big_all_failed
        and all_refused
        and chunked_total * _MIB <= cap_bytes
        and burst_after.get("total_bytes") == cap_bytes
        and burst_after.get("used_bytes", cap_bytes + 1) <= cap_bytes
        and burst_tree.get("allocated_bytes", cap_bytes + 1) <= cap_bytes
        and 0 < burst_host["bytes"] <= cap_bytes
        and burst_host["bytes"] == burst_tree.get("apparent_bytes"),
        commands=[
            *_attempt_commands(gate, burst),
            f"[in container] {_BURST_PROCESSES} processes, each: fallocate -l {_BURST_BIG_MIB}M "
            f"<file> ; then posix_fallocate {_BURST_CHUNK_MIB} MiB files until the first error",
            "[in container] os.statvfs(/workspace) after the burst",
        ],
        observed={
            "workspace_cap_bytes": cap_bytes,
            "processes": len(reports),
            "large_fallocate_exits": [
                (report.get("big_fallocate") or {}).get("exit") for report in reports
            ],
            "large_fallocate_messages": sorted(
                {(report.get("big_fallocate") or {}).get("stderr", "") for report in reports}
            ),
            "chunked_allocations_mib_total": chunked_total,
            "worker_errors": sorted({str(report.get("failure")) for report in reports}),
            "volume_after_in_container": burst_after,
            "allocated_bytes_in_container": burst_tree.get("allocated_bytes"),
            "bytes_read_back_on_host": burst_host,
            "launcher_outcome": burst.outcome,
        },
        expected=(
            "every large fallocate of a parallel burst is refused, the chunked allocations end "
            "with ENOSPC, and no more than the workspace cap is allocated (statvfs and the "
            "allocated blocks inside the container, and the bytes read back on the host)"
        ),
        observer={
            "source": "container: statvfs and allocated blocks and each process's own errno; "
            "host: the workspace read back"
        },
        attempts=(burst,),
    )

    # --- receipt bounds and what may come back to the host ------------------------------------
    bounds = {a.name: _kernel_cap_bounds(a, profile) for a in (ws_bytes, out_bytes, burst)}
    out_listing_ok = out_listing == ["result.json"]
    _assert(
        gate,
        "r-m3-disk-receipt-and-readback",
        passed=all(entry["matches"] for entry in bounds.values())
        and out_listing_ok
        and not (out_bytes.out / "gate-link").is_symlink()
        and not os.path.lexists(out_bytes.out / "gate-fifo")
        and not (ws_bytes.workspace / "gate-fill").is_symlink()
        and all(
            not path.is_symlink() and not stat.S_ISFIFO(path.lstat().st_mode)
            for path in ws_bytes.workspace.rglob("*")
        ),
        commands=[
            *_attempt_commands(gate, ws_bytes, out_bytes, burst),
            "[in container] ln -s /etc/passwd /out/gate-link ; mkfifo /out/gate-fifo",
            "RunnerReceipt.bounds  # disk mechanism and the byte and inode caps, as read back",
        ],
        observed={
            "receipt_bounds": bounds,
            "result_directory_on_host_after_the_attempt": out_listing,
            "planted_symlink_reached_host": (out_bytes.out / "gate-link").is_symlink(),
            "planted_fifo_reached_host": os.path.lexists(out_bytes.out / "gate-fifo"),
        },
        expected=(
            "each receipt records kernel-capped-volume with exactly the profile's byte and inode "
            "numbers and no poll interval; a symlink and a FIFO planted in the result volume "
            "never reach the host, which holds only the regular result file"
        ),
        observer={
            "source": "host: the runner receipt and the result directory listing; the receipt's "
            "bounds are what the launcher read back from the engine's created volumes"
        },
        attempts=(ws_bytes, out_bytes, burst),
    )

    # --- filling a tmpfs volume with memory ends in an ordinary exit 137 -----------------------
    memory_obs = memory.obs
    _assert(
        gate,
        "r-m3-memory-fill-exit-137",
        passed=_launched_clean(memory)
        and _ran_to_completion(memory, exit_code=137)
        and memory_obs is None,
        commands=[
            *_attempt_commands(gate, memory),
            "[in container] write 1 MiB files into /workspace/gate-fill (512 MiB volume) "
            "under a 128 MiB memory limit",
        ],
        observed={
            "memory_limit_mib": 128,
            "workspace_cap_mib": 512,
            "launcher_outcome": memory.outcome,
            "launcher_exit_code": memory.exit_code,
            "worker_reported_completion": memory_obs is not None,
            "container_removed": memory.container_gone,
        },
        expected=(
            "a worker that fills its tmpfs volume until the memory limit is reached is killed by "
            "the kernel and the launcher records an ordinary exit (137), not a launcher kill; it "
            "never completes"
        ),
        observer={"source": "host: the launcher outcome file (state exited, exit code 137)"},
        attempts=(memory,),
    )


_BOMB_PATHS = 205_000


def _intake(gate: Gate, attempt: AttemptResult, branch: str) -> dict[str, Any]:
    fixture = gate.fixture
    assert fixture is not None
    result: dict[str, Any] = {}
    started = time.monotonic()
    stage = "intake_branch_bundle"
    try:
        result["intake_head"] = intake_branch_bundle(
            repository_path=fixture.repo,
            workspace_path=attempt.workspace,
            bundle_path=attempt.out / "branch.bundle",
            head_branch=branch,
            base_revision=fixture.base_revision,
        )
        stage = "verify_intake_branch"
        result["verified_head"] = verify_intake_branch(
            intake_path=intake_path_for(attempt.workspace),
            head_branch=branch,
            base_revision=fixture.base_revision,
        )
        result["accepted"] = True
    except EngineryError as error:
        result.update(
            accepted=False,
            refused_in=stage,
            error_type=type(error).__name__,
            message=str(error)[:240],
        )
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


_TREE_CAP_REFUSAL = "worker branch tree exceeds the entry-count or size limit"
"""The product's own refusal text for a tree past the intake cap (``_bound_tree``): the bomb must
be refused for that reason, not merely refused."""


def step_m3_bomb(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None
    small_branch = f"enginery/gate-bomb-small-{gate.rid}"
    bomb_branch = f"enginery/gate-bomb-{gate.rid}"
    small = gate.attempt(
        "m3-bomb-small",
        "bomb",
        {"branch": small_branch, "paths": 3},
        bundle_branch=small_branch,
        time_budget=300,
    )
    bomb = gate.attempt(
        "m3-bomb",
        "bomb",
        {"branch": bomb_branch, "paths": _BOMB_PATHS, "dirs": 100},
        bundle_branch=bomb_branch,
        time_budget=600,
    )
    small_result = _intake(gate, small, small_branch)
    bomb_bundle = bomb.out / "branch.bundle"
    bomb_result = _intake(gate, bomb, bomb_branch)
    leftovers = {
        label: sorted(path.name for path in attempt.workspace.parent.iterdir())
        for label, attempt in (("small", small), ("bomb", bomb))
    }
    no_checkout = not validation_path_for(bomb.workspace).exists() and not any(
        name.endswith(".validate") for name in leftovers["bomb"]
    )
    _assert(
        gate,
        "r-m3-git-bomb-refused-at-intake",
        passed=_launched_clean(small)
        and _launched_clean(bomb)
        and small_result.get("accepted") is True
        and bomb_bundle.is_file()
        and bomb_result.get("accepted") is False
        and _TREE_CAP_REFUSAL in str(bomb_result.get("message", ""))
        and bomb_result.get("seconds", 999) < 120
        and no_checkout
        and (bomb.obs or {}).get("fast_import_exit") == 0,
        commands=[
            *_attempt_commands(gate, small, bomb),
            "intake_branch_bundle(...) ; verify_intake_branch(...)  # the product's host intake",
        ],
        observed={
            "bomb_tree_entries_committed_by_worker": _BOMB_PATHS,
            "bomb_bundle_bytes": bomb_bundle.stat().st_size if bomb_bundle.is_file() else None,
            "control_small_bundle": small_result,
            "bomb_bundle": bomb_result,
            "host_checkout_made_for_the_bomb": not no_checkout,
            "host_directory_entries": leftovers,
        },
        expected=(
            "the bomb is refused during host verification with the tree-cap reason, in seconds, "
            "with no host checkout; the small bundle from the same path is accepted"
        ),
        observer={"source": "host: the product's intake and verify result and the directory list"},
        attempts=(small, bomb),
    )


# --- M4: protected paths are held, then published on approval ---------------------------------


def _candidate(
    gate: Gate, label: str, files: list[dict[str, Any]]
) -> tuple[AttemptResult, str, str]:
    fixture = gate.fixture
    assert fixture is not None
    branch = f"enginery/gate-m4-{label}-{gate.rid}"
    attempt = gate.attempt(
        f"m4-{label}",
        "plant",
        {"branch": branch, "committed": files, "plants": [], "git_config": {}, "leftovers": []},
        bundle_branch=branch,
        time_budget=120,
    )
    intake_branch_bundle(
        repository_path=fixture.repo,
        workspace_path=attempt.workspace,
        bundle_path=attempt.out / "branch.bundle",
        head_branch=branch,
        base_revision=fixture.base_revision,
    )
    verified = verify_intake_branch(
        intake_path=intake_path_for(attempt.workspace),
        head_branch=branch,
        base_revision=fixture.base_revision,
    )
    return attempt, branch, verified


def step_m4(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None
    policy = ProtectedPathPolicy(())
    protected, protected_branch, protected_head = _candidate(
        gate, "protected", [{"path": ".github/workflows/x.yml", "content": "name: gate\n"}]
    )
    ordinary, ordinary_branch, ordinary_head = _candidate(
        gate, "ordinary", [{"path": "src/ok.py", "content": "VALUE = 1\n"}]
    )
    refs_start = bare_refs(fixture.origin)

    def decide(attempt: AttemptResult, head: str) -> dict[str, Any]:
        publisher = GitBranchPublisher(
            repository_path=fixture.repo, source_path=intake_path_for(attempt.workspace)
        )
        changed = publisher.changed_paths(base_revision=fixture.base_revision, head_revision=head)
        matched = policy.matched(changed)
        return {"publisher": publisher, "changed": list(changed), "matched": list(matched)}

    hold = decide(protected, protected_head)
    free = decide(ordinary, ordinary_head)
    # The hold: nothing is published while a protected path is unapproved.
    refs_after_hold = bare_refs(fixture.origin)
    # The ordinary candidate publishes without a decision.
    publisher_free: GitBranchPublisher = free["publisher"]
    publisher_free.publish(
        BranchPublicationRequest(
            repository_id="gate-fixture",
            branch=ordinary_branch,
            head_revision=ordinary_head,
            expected_remote_revision=None,
            operation_id=OperationId(f"gate-m4-ordinary-{gate.rid}"),
        )
    )
    refs_after_ordinary = bare_refs(fixture.origin)
    # The explicit operator approval, then the publication.
    publisher_held: GitBranchPublisher = hold["publisher"]
    published = publisher_held.publish(
        BranchPublicationRequest(
            repository_id="gate-fixture",
            branch=protected_branch,
            head_revision=protected_head,
            expected_remote_revision=None,
            operation_id=OperationId(f"gate-m4-protected-{gate.rid}"),
        )
    )
    refs_after_approval = bare_refs(fixture.origin)
    origin_object = git(
        ["--git-dir", str(fixture.origin), "rev-parse", f"refs/heads/{protected_branch}"]
    ).stdout.strip()
    _assert(
        gate,
        "r-m4-protected-path-held-then-published",
        passed=".github/**" in DEFAULT_PROTECTED_PATH_PATTERNS
        and ".github/workflows/x.yml" in hold["changed"]
        and hold["matched"] == [".github/workflows/x.yml"]
        and free["matched"] == []
        and refs_after_hold == refs_start
        and refs_after_ordinary == {**refs_start, f"refs/heads/{ordinary_branch}": ordinary_head}
        and refs_after_approval
        == {**refs_after_ordinary, f"refs/heads/{protected_branch}": protected_head}
        and published.revision == protected_head
        and origin_object == protected_head
        and _launched_clean(protected)
        and _launched_clean(ordinary),
        commands=[
            *_attempt_commands(gate, protected, ordinary),
            "GitBranchPublisher(...).changed_paths(base, head); ProtectedPathPolicy().matched(...)",
            "git ls-remote <bare origin>  # before the decision, after the hold, after approval",
            "GitBranchPublisher(...).publish(...)  # after the explicit approval",
        ],
        observed={
            "default_protected_patterns": list(DEFAULT_PROTECTED_PATH_PATTERNS),
            "protected_candidate_changed_paths": hold["changed"],
            "protected_candidate_matched": hold["matched"],
            "ordinary_candidate_changed_paths": free["changed"],
            "ordinary_candidate_matched": free["matched"],
            "origin_refs_unchanged_while_held": refs_after_hold == refs_start,
            "origin_refs_after_ordinary_publication": refs_after_ordinary,
            "origin_refs_after_approval": refs_after_approval,
            "published_revision": published.revision,
            "verified_head": protected_head,
        },
        expected=(
            "the .github change is held with nothing pushed, the ordinary change is not held, "
            "and the approved revision is the only addition to the bare origin"
        ),
        observer={
            "source": "host: the bare origin's refs; changed paths read from the host intake repo",
            "stage1_path": "the policy and publisher functions the Stage 1 hold calls; the "
            "ledgered human-wait wrapper around them is covered by the Stage 1 tests",
        },
        attempts=(protected, ordinary),
    )


# --- M5: the relay is reachable only by model-access workers on their own network --------------

_REACH_SCRIPT = """
import errno, json, socket, sys
out = {}
for name, host, port in json.loads(sys.argv[1]):
    try:
        socket.create_connection((host, port), timeout=3).close()
        out[name] = "connected"
    except socket.gaierror as error:
        out[name] = "refused:resolve" if error.errno in (
            socket.EAI_NONAME, socket.EAI_AGAIN, socket.EAI_FAIL) else "inconclusive:gai"
    except TimeoutError:
        out[name] = "inconclusive:timeout"
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "?")
        explicit = ("ENETUNREACH", "EHOSTUNREACH", "ECONNREFUSED", "ENETDOWN", "EADDRNOTAVAIL")
        out[name] = "refused:" + code if code in explicit else "inconclusive:" + code
print("GATE-REACH " + json.dumps(out))
"""


@contextlib.contextmanager
def _relay_session(gate: Gate) -> Any:
    from factory_gate_boundary import _relay_up

    assert gate.relay_image is not None
    token = f"gate-fake-gateway-{secrets.token_hex(16)}"
    gate.scrubber.register("review-fake-gateway-token", token)
    directory = gate.workroot / f"relay-review-{secrets.token_hex(3)}"
    directory.mkdir(mode=0o700)
    token_file = directory / "gateway.token"
    token_file.write_text(token + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    stub = UpstreamStub(expected_bearer=token)
    stub.start()
    manager = RelayManager(
        gate.engine,
        profile=gate.profile,
        egress_network=gate.egress_network,
        container_name=gate.relay_container,
    )
    gate.track_container(gate.relay_container)
    gate.track_network(gate.egress_network)
    try:
        upstream, notes = _relay_up(
            gate,
            manager,
            upstream_candidates=[f"http://host.docker.internal:{stub.port}"],
            token_file=token_file,
            probe_stub=stub,
        )
        if upstream is None:
            raise RuntimeError(f"the review relay did not start: {notes}")
        yield manager, stub
    finally:
        manager.down()
        stub.stop()


def _reach_results(output: str) -> dict[str, str]:
    for line in output.splitlines():
        if line.startswith("GATE-REACH "):
            try:
                value = json.loads(line.removeprefix("GATE-REACH "))
            except ValueError:
                return {}
            return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}
    return {}


def step_m5_relay(gate: Gate) -> None:
    from factory_gate_boundary import _case, _relay_log

    fixture = gate.fixture
    assert fixture is not None
    with _relay_session(gate) as (manager, stub):
        relay_ip = _container_ip(gate, gate.relay_container, gate.egress_network)
        targets = [
            ("relay-by-name", RELAY_HOSTNAME, RELAY_PORT),
            ("relay-container-name", gate.relay_container, RELAY_PORT),
            ("upstream-stub-by-host-name", "host.docker.internal", stub.port),
        ]
        if relay_ip:
            targets.append(("relay-by-egress-address", relay_ip, RELAY_PORT))
        before_requests = len(stub.snapshot(include_probes=True))
        before_log = len(_relay_log(gate, gate.relay_container))
        none_worker = gate.attempt(
            "m5-none-mode",
            "network",
            {
                "targets": [{"id": n, "host": h, "port": p} for n, h, p in targets],
                "gateway_ports": [],
                "dns_names": [RELAY_HOSTNAME],
                "udp_dns_servers": [],
                "http_posts": [],
                "timeout": 3.0,
            },
        )
        # A validation container, launched by the product's runner, on a checkout of the base.
        context = _runner_context(gate)
        workspace = new_task_workspace(fixture, "m5-validation")
        validation = OciValidationRunner(
            context, artifact_root=fixture.work / "m5-validation-artifacts", time_budget_seconds=90
        )(
            (
                "python3",
                "-c",
                _REACH_SCRIPT,
                json.dumps([[n, h, p] for n, h, p in targets]),
            ),
            workspace,
        )
        none_ops = {
            op["id"].removeprefix("tcp:"): op
            for op in none_worker.operations
            if op["id"].startswith("tcp:")
        }
        validation_results = _reach_results(validation.stdout)
        upstream_after_denied = len(stub.snapshot(include_probes=True)) - before_requests
        log_after_denied = len(_relay_log(gate, gate.relay_container)) - before_log
        # Control: a model-access worker reaches the relay and is forwarded exactly once.
        control_cases = [
            _case("C1-healthz", "GET", "/healthz", forward=False, statuses=(200,)),
            _case("C2-models", "GET", "/v1/models", forward=True, statuses=(200,)),
        ]
        before_control = len(stub.snapshot())
        control = gate.attempt(
            "m5-control-model-access",
            "relay",
            {
                "relay_url": f"http://{RELAY_HOSTNAME}:{RELAY_PORT}",
                "cases": control_cases,
                "env_keys": [],
            },
            relay=True,
        )
        control_forwarded = len(stub.snapshot()) - before_control
        control_results = {r["id"]: r for r in (control.obs or {}).get("results", [])}
        status = manager.status()
    # The network the validation container really had is read from its published receipt (the
    # product's own record of the engine's inspect), never assumed.
    validation_receipt: dict[str, Any] | None = None
    if validation.runner_receipt is not None:
        published = json.loads(
            ArtifactStore(fixture.work / "m5-validation-artifacts").read_bytes(
                validation.runner_receipt.receipt_digest
            )
        )
        effective = published.get("effective", {})
        validation_receipt = {
            "networks": effective.get("networks"),
            "network_mode": effective.get("network_mode"),
        }
    validation_network_none = validation_receipt is not None and validation_receipt == {
        "networks": [NO_NETWORK],
        "network_mode": NO_NETWORK,
    }
    names = [n for n, _h, _p in targets]
    passed = (
        _launched_clean(none_worker)
        and none_worker.receipt is not None
        and none_worker.receipt.effective.networks == (NO_NETWORK,)
        and all(none_ops.get(n, {}).get("ok") for n in names)
        and not (
            none_worker.operations
            and any(o["outcome"] == "inconclusive" for o in none_worker.operations)
        )
        and all(validation_results.get(n, "").startswith("refused") for n in names)
        and validation.returncode == 0
        and validation_network_none
        and upstream_after_denied == 0
        and log_after_denied == 0
        and control_results.get("C1-healthz", {}).get("status") == 200
        and control_results.get("C2-models", {}).get("status") == 200
        and control_forwarded == 1
        and status.healthy
        and _launched_clean(control)
    )
    _assert(
        gate,
        "r-m5-validation-and-none-mode-cannot-reach-relay",
        passed=passed,
        commands=[
            *_attempt_commands(gate, none_worker, control),
            "OciValidationRunner(...)(python3 -c <connect to the relay by name and address>)",
            "docker inspect <relay>  # its address on the egress network",
        ],
        observed={
            "relay_address_on_egress_network": relay_ip,
            "none_mode_worker_connections": {
                n: {"outcome": o["outcome"], "error": o["error"]} for n, o in none_ops.items()
            },
            "none_mode_worker_networks": None
            if none_worker.receipt is None
            else list(none_worker.receipt.effective.networks),
            "validation_container_connections": validation_results,
            "validation_container_recorded_network": validation_receipt,
            "stub_upstream_requests_from_denied_containers": upstream_after_denied,
            "relay_log_entries_from_denied_containers": log_after_denied,
            "control_model_access_worker": {
                "healthz": control_results.get("C1-healthz", {}).get("status"),
                "models": control_results.get("C2-models", {}).get("status"),
                "stub_upstream_requests": control_forwarded,
            },
            "relay_running_throughout": status.healthy,
        },
        expected=(
            "no connection from the validation container or the none-mode worker reaches the "
            "relay or its upstream; the model-access worker reaches the relay and is forwarded"
        ),
        observer={"source": "host: the stub upstream's request log and the relay's own log"},
        attempts=(none_worker, control),
    )


def step_m5_peers(gate: Gate) -> None:
    from factory_gate_boundary import _bridge_gateway  # noqa: F401

    fixture = gate.fixture
    assert fixture is not None
    with _relay_session(gate) as (manager, _stub):
        workspace_a = new_task_workspace(fixture, "m5-peer-a")
        workspace_b = new_task_workspace(fixture, "m5-peer-b")
        operation_a = f"gate:{gate.rid}:m5-peer-a"
        operation_b = f"gate:{gate.rid}:m5-peer-b"
        network_a = expected_network(gate.profile, operation_a, relay=True)
        network_b = expected_network(gate.profile, operation_b, relay=True)
        container_a = container_name_for(operation_a)
        container_b = container_name_for(operation_b)
        port = 18765
        result_a: dict[str, AttemptResult] = {}

        def run_a() -> None:
            result_a["attempt"] = gate.attempt(
                "m5-peer-a",
                "listen",
                {"port": port, "seconds": 90},
                workspace=workspace_a,
                relay=True,
                time_budget=120,
            )

        members: dict[str, list[frozenset[str]]] = {network_a: [], network_b: []}
        peers_ip: dict[str, str | None] = {}
        stop = threading.Event()

        def sample() -> None:
            while not stop.is_set():
                for network in (network_a, network_b):
                    current = _network_members(gate, network)
                    if current is not None:
                        seen = frozenset(current)
                        if seen not in members[network]:
                            members[network].append(seen)
                for container, network in ((container_a, network_a), (container_b, network_b)):
                    address = _container_ip(gate, container, network)
                    if address:
                        peers_ip[container] = address
                stop.wait(0.3)

        sampler = threading.Thread(target=sample)
        sampler.start()
        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            logs = gate.engine_cli("logs", container_a, timeout=30)
            if "GATE_READY" in logs.stdout + logs.stderr:
                break
            time.sleep(0.3)
        ip_a = peers_ip.get(container_a) or _container_ip(gate, container_a, network_a)
        control_connect = None
        if ip_a:
            control_connect = gate.engine_cli(
                "run",
                "--rm",
                "--network",
                network_a,
                "--label",
                f"{GATE_LABEL}={gate.rid}",
                gate.runner_image.image_id,
                "python3",
                "-c",
                f"import socket; socket.create_connection(({ip_a!r}, {port}), timeout=5).close()",
                timeout=90,
            )
        peer_b = gate.attempt(
            "m5-peer-b",
            "network",
            {
                "targets": [
                    {"id": "peer-a-by-address", "host": ip_a or "0.0.0.0", "port": port},
                    {"id": "peer-a-by-name", "host": container_a, "port": port},
                ],
                "gateway_ports": [],
                "dns_names": [container_a],
                "udp_dns_servers": [],
                "http_posts": [],
                "timeout": 3.0,
                "hold": 4,
            },
            workspace=workspace_b,
            relay=True,
        )
        if ip_a:  # ends A's listen early: a peer on A's own network sends ``stop``
            gate.engine_cli(
                "run",
                "--rm",
                "--network",
                network_a,
                "--label",
                f"{GATE_LABEL}={gate.rid}",
                gate.runner_image.image_id,
                "python3",
                "-c",
                f"import socket; s = socket.create_connection(({ip_a!r}, {port}), timeout=5); "
                "s.sendall(b'stop'); s.close()",
                timeout=90,
            )
        thread_a.join(120)
        stop.set()
        sampler.join(30)
        attempt_a = result_a.get("attempt")
        ops_b = {
            op["id"].removeprefix("tcp:"): op
            for op in peer_b.operations
            if op["id"].startswith(("tcp:peer", "dns:"))
        }
        accepted = (attempt_a.obs or {}).get("accepted_peers", []) if attempt_a else []
        ip_b = peers_ip.get(container_b)
        control_ip = None
        networks_left = [
            n for n in (network_a, network_b) if gate.engine.network_info(n) is not None
        ]
        listed_left = [
            n for n in gate.engine.list_attempt_networks() if n in (network_a, network_b)
        ]
        relay_status = manager.status()
    allowed_b = {container_b, gate.relay_container}
    allowed_a = {container_a, gate.relay_container}
    membership_ok = bool(members[network_b]) and all(
        seen <= allowed_b for seen in members[network_b]
    )
    # The control container (named by the engine) may join network A; nothing else may.
    a_extra = {
        name
        for seen in members[network_a]
        for name in seen
        if name not in allowed_a and not name.startswith("enginery-gate-") and name != ""
    }
    del control_ip
    passed = (
        attempt_a is not None
        and _launched_clean(attempt_a)
        and _launched_clean(peer_b)
        and ip_a is not None
        and control_connect is not None
        and control_connect.returncode == 0
        and bool(accepted)
        and ip_b is not None
        and ip_b not in accepted
        and ops_b
        and all(op["ok"] for op in ops_b.values())
        and membership_ok
        and not networks_left
        and not listed_left
        and network_a != network_b
        and relay_status.healthy is not None
    )
    _assert(
        gate,
        "r-m5-concurrent-workers-isolated-networks",
        passed=bool(passed),
        commands=[
            *_attempt_commands(gate, peer_b),
            "docker network inspect <attempt network>  # sampled every 0.3s while both ran",
            "docker run --rm --network <A's network> python3 -c <connect to A>  # control",
            "docker network ls --filter label=<attempt network label>  # after both ended",
        ],
        observed={
            "network_a": network_a,
            "network_b": network_b,
            "peer_a_address": ip_a,
            "peer_b_address": ip_b,
            "same_network_control_container_reached_peer_a": None
            if control_connect is None
            else control_connect.returncode == 0,
            "peer_a_accepted_connections_from": accepted,
            "peer_b_attempts_against_peer_a": {
                n: {"outcome": o["outcome"], "error": o["error"]} for n, o in ops_b.items()
            },
            "network_b_member_sets_seen": [sorted(seen) for seen in members[network_b]],
            "network_a_member_sets_seen": [sorted(seen) for seen in members[network_a]],
            "network_a_unexpected_members": sorted(a_extra),
            "attempt_networks_still_present_afterwards": networks_left,
        },
        expected=(
            "worker B cannot reach worker A (a container on A's network can), B's network only "
            "ever holds B and the relay, and both per-attempt networks are gone afterwards"
        ),
        observer={"source": "host: engine network membership sampled while the workers ran"},
        attempts=tuple(a for a in (attempt_a, peer_b) if a is not None),
    )


# --- m4 / B1: hooks and fsmonitor ----------------------------------------------------------------


def step_hooks(gate: Gate) -> None:
    attempt = gate.attempt("m4-hooks", "hookcommit", {}, time_budget=120)
    log = attempt.workspace / ".git" / "gate-hook-ran.log"
    lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    obs = attempt.obs or {}
    keys = obs.get("git_config_env_keys", [])
    _assert(
        gate,
        "r-m4-hooks-disabled-in-image",
        passed=_launched_clean(attempt)
        and attempt.exit_code == 0
        and {"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"} <= set(keys)
        and obs.get("hooks_path_env_value") == "/dev/null"
        and obs.get("first_commit_exit") == 0
        and obs.get("hook_runs_after_commit_with_worker_environment") == 0
        and obs.get("second_commit_exit") == 0
        and obs.get("hook_runs_after_commit_without_command_scoped_config") == 1
        and lines == ["ran"],
        commands=[
            *_attempt_commands(gate, attempt),
            "[in container] git commit (worker environment) ; git commit (GIT_CONFIG_* removed)",
        ],
        observed={
            "git_config_environment_keys_in_container": keys,
            "hooks_path_value": obs.get("hooks_path_env_value"),
            "hook_runs_after_commit_with_worker_environment": obs.get(
                "hook_runs_after_commit_with_worker_environment"
            ),
            "hook_runs_after_commit_without_the_configuration": obs.get(
                "hook_runs_after_commit_without_command_scoped_config"
            ),
            "host_visible_hook_log_lines": lines,
        },
        expected=(
            "the planted pre-commit hook does not run for the first commit and runs once for "
            "the control commit; the log the hook wrote in the workspace, read by the host, has "
            "exactly the one control line"
        ),
        observer={"source": "host: <workspace>/.git/gate-hook-ran.log written by the hook itself"},
        attempts=(attempt,),
    )


def step_fsmonitor(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None
    trap = fixture.markers / "fsmonitor-trap.sh"
    marker = fixture.markers / "fsmonitor-ran"
    trap.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 1\n", encoding="utf-8")
    trap.chmod(0o755)

    def executed() -> int:
        return len(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else 0

    attempt = gate.attempt(
        "b1-fsmonitor",
        "plant",
        {
            "branch": f"enginery/gate-b1-{gate.rid}",
            "committed": [{"path": "b1.txt", "content": "b1\n"}],
            "plants": [],
            "git_config": {"core.fsmonitor": str(trap)},
            "leftovers": [],
        },
        product_receipt_path=True,
    )
    workspace = attempt.workspace
    config_text = (workspace / ".git" / "config").read_text(encoding="utf-8")
    planted = str(trap) in config_text
    before = executed()
    # Control: git run in the clone on the host executes the planted program.
    subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
        check=False,
        capture_output=True,
        timeout=60,
    )
    control_runs = executed() - before
    marker.unlink(missing_ok=True)
    quiescence = assess_workspace_quiescence(
        workspace, workspace_mode=WORKSPACE_MODE_TASK_LOCAL_CLONE
    )
    after_assessment = executed()
    runtime = _runtime_recovery(gate, trap, marker)
    after_runtime = executed()
    _assert(
        gate,
        "r-b1-fsmonitor-not-run-on-host",
        passed=_launched_clean(attempt)
        and planted
        and control_runs >= 1
        and quiescence == RecoveryAssessment(True, "process_absent_workspace_quiescent")
        and after_assessment == 0
        and runtime.get("ok") is True
        and after_runtime == 0,
        commands=[
            *_attempt_commands(gate, attempt),
            "git status --porcelain  # control, run in the planted clone on the host",
            "assess_workspace_quiescence(<clone>, task-local-clone)",
            "CoordinatorRuntime: fail_exited_worker / cancel_node on a worker whose clone has the "
            "planted core.fsmonitor",
        ],
        observed={
            "core_fsmonitor_planted_in_clone_config": planted,
            "control_git_status_executions": control_runs,
            "quiescence_assessment": dataclasses.asdict(quiescence),
            "host_executions_after_quiescence_assessment": after_assessment,
            "runtime_recovery": runtime,
            "host_executions_after_runtime_recovery": after_runtime,
        },
        expected=(
            "the planted program runs when git is run in the clone (control) and never during "
            "recovery, failure handling or cancel"
        ),
        observer={"source": "host: the marker file the planted program appends to"},
        attempts=(attempt,),
    )


def _runtime_recovery(gate: Gate, trap: Path, marker: Path) -> dict[str, Any]:
    """Failure and cancel handling by the real coordinator runtime for a real container worker."""
    from datetime import UTC, datetime, timedelta

    from enginery.engine.runtime import CoordinatorRuntime, FixtureDispatch
    from enginery.engine.scheduler import SchedulingLimits
    from enginery.engine.workspace import WORKSPACE_MODE_TASK_LOCAL_CLONE as MODE
    from enginery.ledger.service import LedgerService

    fixture = gate.fixture
    assert fixture is not None
    now = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    window = timedelta(seconds=60)
    limits = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
    report: dict[str, Any] = {"ok": False}
    ledger_path = gate.workroot / "review" / f"runtime-{secrets.token_hex(3)}.db"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    plant = (
        "import subprocess,sys,time\n"
        f"subprocess.run(['git','config','core.fsmonitor',{str(trap)!r}],cwd='/workspace',check=True)\n"
        "open('/proc/1/fd/1','w').write('GATE_PLANTED\\n')\n"
        "time.sleep(float(sys.argv[1]))\n"
    )
    wrapper = (
        "import os,sys\n"
        "spec=sys.argv[1]\n"
        "import json\n"
        "os.makedirs(json.loads(spec)['out_path'],exist_ok=True)\n"
        "os.chmod(json.loads(spec)['out_path'],0o700)\n"
        "os.execv(sys.executable,[sys.executable,'-m',sys.argv[2],'--spec',spec,'--']+sys.argv[3:])\n"
    )
    ledger = LedgerService.open(ledger_path)
    try:
        for label, sleep_seconds in (
            # The failing worker lingers a few seconds so the observer can see its marker in the
            # container log before the launcher removes the container (a slower engine, such as
            # a VM, would otherwise lose the race and report a plant that did happen as missing).
            ("failure", "6"),
            ("cancel", "120"),
        ):
            run_id = f"b1-{label}"
            workspace_path = fixture.work / f"b1-runtime-{label}" / "ws"
            operation_id = f"gate:{gate.rid}:b1-runtime-{label}"
            spec = make_spec(
                operation_id=operation_id,
                image=gate.runner_image.image_id,
                profile=gate.profile,
                workspace=workspace_path,
                out=Path(str(workspace_path) + ".out"),
                receipt_path=fixture.receipts / f"{operation_id}.runner-receipt.json",
                time_budget_seconds=150,
                docker_context=gate.docker_context,
            )
            gate.track_container(spec.container_name)
            command = (
                sys.executable,
                "-c",
                wrapper,
                json.dumps(spec.to_state(), sort_keys=True, separators=(",", ":")),
                LAUNCHER_MODULE,
                "python3",
                "-c",
                plant,
                sleep_seconds,
            )
            runtime = CoordinatorRuntime(
                ledger,
                owner="coordinator",
                container_gone=lambda op: not gate.engine.container_exists(container_name_for(op)),
            )
            dispatched = runtime.tick(
                now=now,
                heartbeat_window=window,
                lease_window=timedelta(seconds=600),
                limits=limits,
                requests=(
                    FixtureDispatch(
                        run_id=run_id,
                        node_id="implement",
                        attempt_id=f"attempt-{label}",
                        repository_id=f"gate-fixture-{label}",
                        repository_path=fixture.repo,
                        workspace_path=workspace_path,
                        base_revision=fixture.base_revision,
                        command=command,
                        expected_attempt_version=0,
                        operation_id=operation_id,
                        retain_workspace=True,
                        workspace_mode=MODE,
                    ),
                ),
            ).dispatched
            if len(dispatched) != 1:
                report[label] = "the runtime did not dispatch the worker"
                return report
            deadline = time.monotonic() + 120
            was_planted = False
            while time.monotonic() < deadline and not was_planted:
                logs = gate.engine_cli("logs", spec.container_name, timeout=30)
                was_planted = "GATE_PLANTED" in logs.stdout + logs.stderr
                if not was_planted:
                    time.sleep(0.3)
            if label == "failure":
                while time.monotonic() < deadline and gate.engine.container_exists(
                    spec.container_name
                ):
                    time.sleep(0.3)
                time.sleep(1)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(dispatched[0].identity.pid, 0)
                runtime.fail_exited_worker(
                    run_id=run_id,
                    node_id="implement",
                    now=now + timedelta(seconds=2),
                    heartbeat_window=window,
                )
            else:
                epoch = runtime.claim_epoch(now=now + timedelta(seconds=1), heartbeat_window=window)
                runtime.cancel_node(
                    run_id=run_id,
                    node_id="implement",
                    epoch=epoch.epoch,
                    now=now + timedelta(seconds=2),
                )
                time.sleep(2)
            node = ledger.read_projection(
                aggregate_type="runtime_node", aggregate_id=f"{run_id}:implement"
            )
            report[label] = {
                "node_status": None if node is None else node.state.get("status"),
                "fsmonitor_planted_in_container": was_planted,
                "container_gone": not gate.engine.container_exists(spec.container_name),
                "marker_present": marker.exists(),
            }
        report["ok"] = (
            report["failure"]["node_status"] == "failed"
            and report["cancel"]["node_status"] == "cancelled"
            and report["failure"]["fsmonitor_planted_in_container"]
            and report["cancel"]["fsmonitor_planted_in_container"]
            and report["failure"]["container_gone"]
            and report["cancel"]["container_gone"]
        )
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        ledger.close()
    return report


# --- m7: containers created weaker are flagged --------------------------------------------------


def step_weaker(gate: Gate) -> None:
    fixture = gate.fixture
    assert fixture is not None
    workspace = new_task_workspace(fixture, "m7-weaker")
    out = Path(str(workspace) + ".out")
    out.mkdir(parents=True, exist_ok=True)
    weakenings: list[tuple[str, list[str], str]] = [
        ("seccomp-unconfined", ["--security-opt", "seccomp=unconfined"], "seccomp=unconfined"),
        ("apparmor-unconfined", ["--security-opt", "apparmor=unconfined"], "apparmor=unconfined"),
        ("label-disable", ["--security-opt", "label=disable"], "label=disable"),
        ("systempaths-unconfined", ["--security-opt", "systempaths=unconfined"], "not masked"),
        ("host-cgroupns", ["--cgroupns", "host"], "cgroup"),
    ]
    cases: list[dict[str, Any]] = []

    def inspect_case(label: str, extra: list[str], expect: str | None) -> dict[str, Any]:
        case_spec = make_spec(
            operation_id=f"gate:{gate.rid}:m7-{label}",
            image=gate.runner_image.image_id,
            profile=gate.profile,
            workspace=workspace,
            out=out,
            receipt_path=fixture.receipts / f"m7-{label}.json",
            time_budget_seconds=30,
            docker_context=gate.docker_context,
        )
        name = case_spec.container_name
        gate.track_container(name)
        arguments = list(create_arguments(case_spec, ["true"], nonce=secrets.token_hex(16)))
        # ``create`` ends with <image> <command...>; the extra flags go before the image.
        index = arguments.index(case_spec.image)
        arguments[index:index] = extra
        entry: dict[str, Any] = {"case": label, "extra_flags": extra, "expected_mention": expect}
        # The worker joins the attempt's two capped volumes, which the store container creates.
        create_attempt_store(gate.engine, case_spec)
        try:
            created = gate.engine_cli(*arguments, timeout=120)
            entry["created"] = created.returncode == 0
            if created.returncode != 0:
                entry["error"] = created.stderr.strip()[-200:]
                return entry
            container = gate.engine.container_inspect(name)
            image = gate.engine.image_inspect(gate.runner_image.image_id)
            names = store_names_for(case_spec.container_name)
            volumes = {
                volume: gate.engine.volume_inspect(volume)
                for volume in (names.workspace_volume, names.out_volume)
            }
            problems = [
                *profile_violations(
                    gate.profile,
                    effective_from_inspect(container, image),
                    network=case_spec.attempt_network,
                ),
                *launch_violations(case_spec, container),
                *store_violations(case_spec, container, volumes),
            ]
            entry["violations"] = problems
            gate.engine.remove(name, force=True)
            return entry
        finally:
            remove_attempt_store(gate.engine, case_spec)

    control = inspect_case("control", [], None)
    for label, extra, expect in weakenings:
        cases.append(inspect_case(label, extra, expect))
    flagged = [
        case.get("created") is True
        and any(case["expected_mention"].lower() in v.lower() for v in case.get("violations", []))
        for case in cases
    ]
    _assert(
        gate,
        "r-m7-weaker-container-flagged",
        passed=control.get("created") is True and control.get("violations") == [] and all(flagged),
        commands=[
            gate.docker_command("create", "<create_arguments(spec)> + <one weaker flag>"),
            "profile_violations(profile, effective, network=...); launch_violations(spec, inspect)",
        ],
        observed={"control_product_container": control, "weaker_containers": cases},
        expected="every weaker container yields a violation naming the weakness; the control none",
        observer={"source": "host: docker inspect of each container read back by the checks"},
    )


# --- driver --------------------------------------------------------------------------------


def run(gate: Gate) -> None:
    """Run every review step; a step that raises fails its own assertions, never skips them."""
    steps: list[tuple[str, list[str], Callable[[Gate], None]]] = [
        ("m1", ["r-m1-no-host-objects-in-workspace"], step_m1),
        (
            "m2",
            [
                "r-m2-symlink-result-refused",
                "r-m2-fifo-result-refused",
                "r-m2-stale-result-replay-refused",
                "r-m2-exit-125-spoof-classified",
            ],
            step_m2,
        ),
        ("m3-log", ["r-m3-container-log-bounded"], step_m3_log),
        (
            "m3-disk",
            [
                "r-m3-disk-enospc-bytes",
                "r-m3-disk-enospc-inodes",
                "r-m3-disk-parallel-fallocate-capped",
                "r-m3-disk-receipt-and-readback",
                "r-m3-memory-fill-exit-137",
            ],
            step_m3_disk,
        ),
        ("m3-bomb", ["r-m3-git-bomb-refused-at-intake"], step_m3_bomb),
        ("m4", ["r-m4-protected-path-held-then-published"], step_m4),
        ("m5-relay", ["r-m5-validation-and-none-mode-cannot-reach-relay"], step_m5_relay),
        ("m5-peers", ["r-m5-concurrent-workers-isolated-networks"], step_m5_peers),
        ("hooks", ["r-m4-hooks-disabled-in-image"], step_hooks),
        ("fsmonitor", ["r-b1-fsmonitor-not-run-on-host"], step_fsmonitor),
        ("weaker", ["r-m7-weaker-container-flagged"], step_weaker),
    ]
    for label, assertion_ids, action in steps:
        try:
            action(gate)
        except Exception as error:
            reason = f"review step {label} raised {type(error).__name__}: {error}"
            for assertion_id in assertion_ids:
                if assertion_id not in gate.evidence.recorded:
                    gate.evidence.fail_assertion(assertion_id, reason)
            gate.evidence.details.setdefault("step_errors", []).append(reason)


__all__ = ["REQUIRED", "run"]
