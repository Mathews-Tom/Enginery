"""Shared run context of the factory gate: engine, images, fixture, attempts, cleanup, identities.

``Gate`` is what a scenario drives. ``prepare_gate`` turns "an engine, two images and a
permission to run containers" into a ``Gate`` or raises ``GateHoldError`` naming what is missing:
a missing prerequisite holds the scenario, it never lets a step be skipped.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from enginery.adapters.oci.engine import EngineCommandError, OciEngine
from enginery.adapters.oci.image import (
    build_image,
    build_relay_image,
    default_relay_tag,
    default_tag,
    recipe_digest,
    relay_recipe_digest,
    verify_image,
    verify_relay_image,
)
from enginery.adapters.oci.launch import store_names_for
from enginery.adapters.oci.launcher import run_launcher
from enginery.domain.errors import (
    EngineryError,
    MissingPrerequisiteError,
    OperationTimeoutError,
    StaleEvidenceError,
)
from enginery.domain.runner import (
    EngineIdentity,
    ImageIdentity,
    ResourceLimits,
    RunnerProfile,
    RunnerReceipt,
    outcome_path_for,
)
from factory_gate_evidence import EvidenceBuilder
from factory_gate_support import (
    GATE_LABEL,
    OBS_FILE_IN_WORKSPACE,
    PROBE_IN_CONTAINER,
    AttemptResult,
    ContainerObserver,
    HostFixture,
    Scrubber,
    StubNetwork,
    UpstreamStub,
    build_fixture,
    diff_snapshots,
    display_command,
    iter_enginery_containers,
    launch_command,
    launcher_environment,
    list_engine_names,
    make_spec,
    new_task_workspace,
    probe_seed_files,
    read_json,
    read_probe_observation,
    read_receipt,
    run_with_deadline,
    snapshot_tree,
    violations_of,
)

WORKER_DEADLINE_SLACK_SECONDS = 150


class GateHoldError(Exception):
    """The engine, an image or a permission the scenario needs is missing: hold, never pass."""


@dataclass(frozen=True, slots=True)
class GateOptions:
    scenario: str
    evidence_out: Path
    harness: str | None = None
    live: bool = False
    confirm_single_session: bool = False
    docker_context: str | None = None
    runner_image: str | None = None
    relay_image: str | None = None
    gateway_url: str | None = None
    gateway_token_file: Path | None = None
    model: str | None = None
    session_deadline_seconds: int = 900
    custody_receipt: Path | None = None
    subject_commit: str | None = None

    @property
    def mode(self) -> str:
        return "live" if self.live else "local"

    @property
    def raw_dir(self) -> Path:
        return self.evidence_out.with_name(self.evidence_out.stem + ".raw")


def subject_identity(subject_commit: str | None = None) -> dict[str, Any]:
    """The enginery build under test: version plus a git revision or an installed-files digest."""
    import enginery

    here = Path(enginery.__file__).resolve()
    subject: dict[str, Any] = {"version": enginery.__version__, "package_path": str(here.parent)}
    root = next((parent for parent in here.parents if (parent / ".git").exists()), None)
    if root is not None:
        env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        git_sha = head.stdout.strip() or None
        subject["source"] = {
            "kind": "git",
            "git_sha": git_sha,
            "dirty": bool(status.stdout.strip()),
        }
        subject["commit_sha"] = git_sha
        if subject_commit is not None:
            subject["declared_commit_sha"] = subject_commit
            subject["declared_commit_matches"] = subject_commit == git_sha
        return subject
    try:
        distribution = metadata.distribution("enginery")
        record = distribution.read_text("RECORD") or ""
        subject["source"] = {
            "kind": "installed-wheel",
            "record_digest": hashlib.sha256(record.encode()).hexdigest(),
            "record_entries": len([line for line in record.splitlines() if line.strip()]),
        }
    except metadata.PackageNotFoundError:
        subject["source"] = {"kind": "unknown"}
    # An installed wheel carries no git metadata, so nothing here can verify which commit it was
    # built from: ``commit_sha`` stays null (unverified) and the operator's claim
    # (--subject-commit) is recorded, as a claim, under ``declared_commit_sha`` beside the wheel
    # digest.
    subject["commit_sha"] = None
    if subject_commit is not None:
        subject["declared_commit_sha"] = subject_commit
        subject["declared_commit_verified"] = False
    return subject


def host_identity() -> dict[str, Any]:
    identity: dict[str, Any] = {
        "hostname": platform.node(),
        "os": platform.platform(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
    try:
        from enginery.engine.supervisor import probe_host

        probed = probe_host()
        identity.update(machine_id=probed.machine_id, boot_id=probed.boot_id)
    except Exception as error:
        identity["host_probe_error"] = type(error).__name__
    return identity


def evaluator_identity(required: dict[str, str]) -> dict[str, Any]:
    baseline = resources.files("enginery.config").joinpath("acceptance-baseline-v1.toml")
    digest = hashlib.sha256(baseline.read_bytes()).hexdigest()
    return {
        "acceptance_baseline_version": "1",
        "acceptance_baseline_digest": f"sha256:{digest}",
        "required_assertions_digest": "sha256:"
        + hashlib.sha256(json.dumps(required, sort_keys=True).encode()).hexdigest(),
    }


def _image_id_by_tag(engine: OciEngine, tag: str) -> str | None:
    completed = engine.run_cli(("image", "inspect", "--format", "{{.Id}}", tag), timeout=60)
    identifier = completed.stdout.strip()
    return identifier if completed.returncode == 0 and identifier.startswith("sha256:") else None


def _resolve_image(
    engine: OciEngine,
    *,
    explicit: str | None,
    verify: Callable[[OciEngine, str], ImageIdentity],
    build: Callable[..., ImageIdentity],
    tag: str,
    noun: str,
    created_tags: list[str],
    notes: list[str],
) -> ImageIdentity:
    try:
        if explicit is not None:
            notes.append(f"{noun}: pinned by --{noun}-image {explicit}")
            return verify(engine, explicit)
        existing = _image_id_by_tag(engine, tag)
        if existing is not None:
            with contextlib.suppress(StaleEvidenceError, MissingPrerequisiteError):
                identity = verify(engine, existing)
                notes.append(f"{noun}: reused the existing image tagged {tag}")
                return identity
        notes.append(f"{noun}: built from the packaged recipe as {tag}")
        identity = build(engine, tag=tag)
        if existing is None:
            created_tags.append(tag)
        return identity
    except StaleEvidenceError as error:
        rebuild = "relay-" if noun == "relay" else ""
        raise GateHoldError(
            f"the {noun} image was not built from the recipe this installation ships "
            f"(rebuild it with 'enginery runner build-{rebuild}image'): "
            f"{error}"
        ) from error
    except (MissingPrerequisiteError, OperationTimeoutError, EngineCommandError) as error:
        raise GateHoldError(f"the {noun} image is not usable on this engine: {error}") from error


@dataclass(frozen=True, slots=True)
class StrayObjects:
    """What this gate caused that the engine still holds, read from the engine's own listings.

    ``listing_errors`` are listings the engine refused: a stray check that could not ask the engine
    is not a clean one.
    """

    containers: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    listing_errors: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.containers or self.volumes or self.networks or self.listing_errors)

    def to_state(self) -> dict[str, Any]:
        return {
            "stray_enginery_containers": list(self.containers),
            "stray_volumes": list(self.volumes),
            "leftover_gate_networks": list(self.networks),
            "listing_errors": list(self.listing_errors),
        }


@dataclass(slots=True)
class Gate:
    """One scenario run's shared state."""

    options: GateOptions
    engine: OciEngine
    evidence: EvidenceBuilder
    scrubber: Scrubber
    engine_identity: EngineIdentity
    runner_image: ImageIdentity
    relay_image: ImageIdentity | None
    workroot: Path
    rid: str = field(default_factory=lambda: secrets.token_hex(4))
    fixture: HostFixture | None = None
    stubs: StubNetwork | None = None
    upstream: UpstreamStub | None = None
    profile: RunnerProfile = field(default_factory=RunnerProfile)
    limits_profile: RunnerProfile = field(default_factory=RunnerProfile)
    attempts: list[AttemptResult] = field(default_factory=list)
    leaks: list[dict[str, Any]] = field(default_factory=list)
    effects: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    created_tags: list[str] = field(default_factory=list)
    _containers: list[str] = field(default_factory=list)
    _networks: list[str] = field(default_factory=list)
    _attempt_networks: set[str] = field(default_factory=set)
    _validation_containers: set[str] = field(default_factory=set)
    _cleanups: list[Callable[[], None]] = field(default_factory=list)
    _preexisting_containers: set[str] = field(default_factory=set)
    cleanup_report: dict[str, Any] = field(default_factory=dict)

    # --- names -----------------------------------------------------------------------------

    @property
    def internal_network(self) -> str:
        return f"enginery-gate-{self.rid}-internal"

    @property
    def egress_network(self) -> str:
        return f"enginery-gate-{self.rid}-egress"

    @property
    def relay_container(self) -> str:
        return f"enginery-gate-{self.rid}-relay"

    @property
    def docker_context(self) -> str | None:
        return self.options.docker_context

    # --- bookkeeping -----------------------------------------------------------------------

    def raw_write(self, name: str, content: Any) -> str:
        """Write one raw artifact (scrubbed) next to the evidence; return its relative path."""
        directory = self.options.raw_dir
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            text = content.decode("utf-8", "replace")
        elif isinstance(content, str):
            text = content
        else:
            text = json.dumps(self.scrubber.data(content), indent=2, sort_keys=True, default=str)
        target.write_text(self.scrubber.text(text), encoding="utf-8")
        return f"{directory.name}/{name}"

    def track_container(self, name: str) -> None:
        self._containers.append(name)

    def track_network(self, name: str, *, per_attempt: bool = False) -> None:
        """Track a network for removal; ``per_attempt`` marks one the launcher removes itself."""
        if name == "none":
            return
        if name not in self._networks:
            self._networks.append(name)
        if per_attempt:
            self._attempt_networks.add(name)

    def store_objects(self) -> tuple[list[str], list[str]]:
        """The store containers and volumes the launcher creates for every tracked container.

        Their names derive from the worker's, so the gate need not see the launcher create them; a
        tracked container that is no worker (the relay, a control) simply has none.
        """
        if self.fixture is not None:
            self.track_validation_receipts(self.fixture.receipts)
        holders: list[str] = []
        volumes: list[str] = []
        for name in dict.fromkeys(self._containers):
            names = store_names_for(name)
            holders.append(names.holder)
            volumes += [names.workspace_volume, names.out_volume]
        return holders, volumes

    def track_validation_receipts(self, receipts: Path) -> None:
        """Track the container of every validation receipt the product launcher published.

        The product's validation runner starts its own container (and store) during the gate; the
        gate never sees it created, so the receipt it publishes is the record of its name.
        """
        for path in sorted(receipts.glob("*.validation-receipt.json")):
            receipt, _document = read_receipt(path)
            if receipt is None:
                continue
            if receipt.container_name not in self._containers:
                self.track_container(receipt.container_name)
            self._validation_containers.add(receipt.container_name)

    def on_cleanup(self, action: Callable[[], None]) -> None:
        self._cleanups.append(action)

    def protected_roots(self) -> list[Path]:
        fixture = self.fixture
        assert fixture is not None
        return [
            fixture.ledger_path.parent,
            fixture.artifacts_dir,
            fixture.config_dir,
            fixture.engine_dir,
            fixture.holdouts_dir,
            fixture.home_dir,
            fixture.origin,
            fixture.repo,
        ]

    def protected_snapshot(self) -> dict[str, dict[str, str]]:
        return {str(root): snapshot_tree(root) for root in self.protected_roots()}

    def protected_changes(
        self, before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]
    ) -> list[str]:
        changes: list[str] = []
        for root in sorted(set(before) | set(after)):
            changes += [
                f"{Path(root).name}: {change}"
                for change in diff_snapshots(before.get(root, {}), after.get(root, {}))
            ]
        return changes

    def engine_cli(self, *arguments: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return self.engine.run_cli(arguments, timeout=timeout)

    def docker_command(self, *arguments: str) -> str:
        prefix = "docker" + (f" --context {self.docker_context}" if self.docker_context else "")
        return prefix + " " + " ".join(arguments)

    # --- attempts --------------------------------------------------------------------------

    def attempt(
        self,
        name: str,
        mode: str,
        params: dict[str, Any],
        *,
        profile: RunnerProfile | None = None,
        workspace: Path | None = None,
        time_budget: int = 600,
        bundle_branch: str | None = None,
        env: Sequence[tuple[str, str]] = (),
        observe: bool = False,
        inner: Sequence[str] | None = None,
        stage: bool = True,
        spec_overrides: dict[str, Any] | None = None,
        in_process_slack: int | None = None,
        relay: bool = False,
        product_receipt_path: bool = False,
    ) -> AttemptResult:
        """Run one probe mode in a real hardened container through the real launcher."""
        fixture = self.fixture
        assert fixture is not None
        profile = profile or self.profile
        workspace = workspace or new_task_workspace(fixture, name)
        out = Path(str(workspace) + ".out")
        out.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):  # only this attempt's own copy may be read
            (workspace / OBS_FILE_IN_WORKSPACE).unlink()
        operation_id = f"gate:{self.rid}:{name}"
        # ``product_receipt_path``: the receipt (and its launcher outcome) where the product's own
        # collection looks for them, ``<receipt root>/<operation id>.runner-receipt.json``.
        receipt_path = fixture.receipts / (
            f"{operation_id}.runner-receipt.json"
            if product_receipt_path
            else f"{name}.runner-receipt.json"
        )
        spec = make_spec(
            operation_id=operation_id,
            image=self.runner_image.image_id,
            profile=profile,
            workspace=workspace,
            out=out,
            receipt_path=receipt_path,
            time_budget_seconds=time_budget,
            docker_context=self.docker_context,
            env=env,
            seed_files=probe_seed_files(params) if stage else (),
            bundle_branch=bundle_branch,
            bundle_base=fixture.base_revision if bundle_branch else None,
        )
        if relay:
            spec = dataclasses.replace(spec, relay_container=self.relay_container)
        if spec_overrides:
            spec = dataclasses.replace(spec, **spec_overrides)
        inner_command = list(inner) if inner is not None else ["python3", PROBE_IN_CONTAINER, mode]
        spec_state = spec.to_state()
        spec_state["seed_files"] = [  # the probe source is not repeated in every raw record
            [path, f"<{len(content)} characters>"] for path, content in spec.seed_files
        ]
        spec_ref = self.raw_write(f"{name}.launch-spec.json", spec_state)
        self.track_container(spec.container_name)
        self.track_network(spec.attempt_network, per_attempt=True)
        observer = ContainerObserver(self.engine, spec.container_name) if observe else None
        if observer is not None:
            observer.start()
        before = self.protected_snapshot()
        started = time.monotonic()
        deadline = (
            time_budget + profile.resources.stop_grace_seconds + WORKER_DEADLINE_SLACK_SECONDS
        )
        if in_process_slack is not None:
            # The launcher function itself, so its hard-deadline slack can be set below the
            # worker's own kill grace: the path where the launcher, not the worker, ends it.
            captured = io.StringIO()
            code = run_launcher(
                spec,
                inner_command,
                engine=OciEngine(context=spec.docker_context),
                stderr=captured,
                deadline_slack_seconds=in_process_slack,
            )
            completed = subprocess.CompletedProcess([], code, "", captured.getvalue())
            timed_out = False
        else:
            completed, timed_out = run_with_deadline(
                launch_command(spec, inner_command),
                env=launcher_environment(),
                deadline_seconds=deadline,
            )
        duration = time.monotonic() - started
        observed = observer.stop() if observer is not None else None
        after = self.protected_snapshot()
        changes = self.protected_changes(before, after)
        receipt, receipt_document = read_receipt(receipt_path)
        receipt_ref = None
        if receipt_document is not None:
            receipt_ref = self.raw_write(f"{name}.runner-receipt.json", receipt_document)
        outcome = read_json(Path(outcome_path_for(str(receipt_path))))
        if outcome is not None:
            self.raw_write(f"{name}.launch-outcome.json", outcome)
        result = read_json(out / "result.json")
        obs = read_probe_observation(result, workspace)
        if result is not None:
            self.raw_write(f"{name}.result.json", result)
        if obs is not None:
            self.raw_write(f"{name}.obs.json", obs)
        self.raw_write(f"{name}.launcher-stderr.txt", completed.stderr)
        leaked = self.scrubber.contains(
            json.dumps(result or {}) + json.dumps(obs or {}) + completed.stdout + completed.stderr
        )
        if leaked:
            self.leaks.append({"attempt": name, "labels": leaked})
        gone = not self.engine.container_exists(spec.container_name)
        attempt = AttemptResult(
            name=name,
            mode=mode,
            exit_code=completed.returncode,
            duration_seconds=round(duration, 2),
            launcher_stderr=completed.stderr,
            container_name=spec.container_name,
            operation_id=operation_id,
            command_line=display_command(spec_ref, inner_command),
            spec_ref=spec_ref,
            receipt=receipt,
            receipt_ref=receipt_ref,
            violations=violations_of(profile, receipt, relay=spec.relay_container is not None),
            outcome=outcome,
            attempt_network=spec.attempt_network,
            result=result,
            obs=obs,
            workspace=workspace,
            out=out,
            container_gone=gone,
            observer=observed,
            timed_out=timed_out,
        )
        attempt.observer = {**(observed or {}), "host_protected_changes": changes}
        self.attempts.append(attempt)
        return attempt

    def evidence_commands(self, attempt: AttemptResult) -> list[str]:
        """The launcher command line and every in-container attempt the probe made."""
        commands = [attempt.command_line]
        commands += [f"[in container] {op['command']}" for op in attempt.operations]
        return commands

    def raw_refs(self, attempt: AttemptResult) -> list[str]:
        refs = [attempt.spec_ref, f"{self.options.raw_dir.name}/{attempt.name}.launcher-stderr.txt"]
        if attempt.receipt_ref:
            refs.append(attempt.receipt_ref)
        if attempt.obs is not None:
            refs.append(f"{self.options.raw_dir.name}/{attempt.name}.obs.json")
        if attempt.result is not None:
            refs.append(f"{self.options.raw_dir.name}/{attempt.name}.result.json")
        return refs

    # --- identities ------------------------------------------------------------------------

    def identities(self, required: dict[str, str]) -> dict[str, Any]:
        runner_labels = {
            "runner_image_id": self.runner_image.image_id,
            "runner_recipe_digest_image": self.runner_image.recipe_digest,
            "runner_recipe_digest_packaged": recipe_digest(),
            "harness_versions": dict(self.runner_image.harness_versions),
            "relay_image_id": None if self.relay_image is None else self.relay_image.image_id,
            "relay_recipe_digest_image": (
                None if self.relay_image is None else self.relay_image.recipe_digest
            ),
            "relay_recipe_digest_packaged": relay_recipe_digest(),
        }
        return {
            "subject": subject_identity(self.options.subject_commit),
            "build": {**runner_labels, "image_notes": list(self.notes)},
            "config": {
                "gate_profile_digest": str(self.profile.digest),
                "limits_profile_digest": str(self.limits_profile.digest),
                "product_default_profile_digest": str(RunnerProfile().digest),
                "docker_context": self.docker_context,
                "gateway_url_given": self.options.gateway_url is not None,
            },
            "host": host_identity(),
            "engine": self.engine_identity.to_state(),
            "image": self.runner_image.to_state(),
            "profile": self.profile.to_state(),
            "evaluator": evaluator_identity(required),
        }

    # --- cleanup ---------------------------------------------------------------------------

    def cleanup(self) -> None:
        report: dict[str, Any] = {
            "containers_removed": [],
            "volumes_removed": [],
            "networks_removed": [],
            "errors": [],
        }
        for action in reversed(self._cleanups):
            try:
                action()
            except Exception as error:
                report["errors"].append(f"cleanup action failed: {type(error).__name__}: {error}")
        holders, volumes = self.store_objects()
        # What the product launcher removes itself on every path; finding any of it here is a leak
        # of the launcher's (or of a probe's own store), however quietly the gate removes it.
        launcher_owned = {
            *holders,
            *volumes,
            *self._attempt_networks,
            *self._validation_containers,
        }
        self._remove_present(
            report,
            "container",
            [*self._containers, *holders],
            launcher_owned,
            list_arguments=("ps", "--all"),
            template="{{.Names}}",
            remove_arguments=("rm", "--volumes", "--force"),
        )
        self._remove_present(
            report,
            "volume",
            volumes,
            launcher_owned,
            list_arguments=("volume", "ls"),
            template="{{.Name}}",
            remove_arguments=("volume", "rm"),
        )
        self._remove_present(
            report,
            "network",
            self._networks,
            launcher_owned,
            list_arguments=("network", "ls"),
            template="{{.Name}}",
            remove_arguments=("network", "rm"),
        )
        report["image_tags_removed"] = []
        for tag in self.created_tags:
            try:
                completed = self.engine.run_cli(("image", "rm", tag), timeout=120)
            except EngineryError as error:
                report["errors"].append(f"image tag {tag}: {error}")
                continue
            if completed.returncode == 0:
                report["image_tags_removed"].append(tag)
            else:
                report["errors"].append(f"image tag {tag}: {completed.stderr.strip()[-200:]}")
        if self.stubs is not None:
            self.stubs.stop()
        if self.upstream is not None:
            self.upstream.stop()
        if self.fixture is not None:
            from enginery.engine.task_repository import remove_task_local_workspace

            retained: list[str] = []
            for workspace in sorted(self.fixture.work.glob("*/ws")):
                retained += [
                    str(path) for path in remove_task_local_workspace(workspace_path=workspace)
                ]
            shutil.rmtree(self.workroot, ignore_errors=True)
            if self.workroot.exists():
                retained.append(str(self.workroot))
            report["fixture_retained"] = retained
        self.cleanup_report = report

    def _remove_present(
        self,
        report: dict[str, Any],
        kind: str,
        names: Sequence[str],
        launcher_owned: set[str],
        *,
        list_arguments: tuple[str, ...],
        template: str,
        remove_arguments: tuple[str, ...],
    ) -> None:
        """Remove each of ``names`` the engine lists; one the launcher owns is also reported.

        The engine is listed once instead of asked about every name, so an object that is absent
        costs nothing, and an engine that cannot be listed is an error rather than "nothing left".
        One failing removal never stops the next one: a hung or refused command must not leave the
        later networks behind.
        """
        try:
            present = set(list_engine_names(self.engine, list_arguments, template))
        except EngineryError as error:
            report["errors"].append(f"{kind}s: {error}")
            return
        for name in dict.fromkeys(names):
            if name not in present:
                continue
            try:
                completed = self.engine.run_cli((*remove_arguments, name), timeout=120)
            except EngineryError as error:
                report["errors"].append(f"{kind} {name}: {error}")
                continue
            stderr = completed.stderr.lower()
            if completed.returncode != 0:
                if not any(
                    text in stderr for text in ("no such", "not found", "already in progress")
                ):
                    report["errors"].append(f"{kind} {name}: {completed.stderr.strip()[-200:]}")
                continue
            report[f"{kind}s_removed"].append(name)
            if name in launcher_owned:
                report["errors"].append(
                    f"{kind} {name} was left behind by the launcher; the gate removed it"
                )

    def stray_objects(self) -> StrayObjects:
        """Containers, volumes and networks of this gate the engine still holds.

        Only what this gate created counts: it tracks every container and network it starts and
        names the rest with its run id, so another operator's or a concurrent run's objects are not
        strays. A store container belongs to its worker and the two capped volumes to the store.
        """
        problems: list[str] = []

        def listed(arguments: tuple[str, ...], template: str) -> set[str]:
            try:
                return set(list_engine_names(self.engine, arguments, template))
            except EngineryError as error:
                problems.append(str(error))
                return set()

        holders, volumes = self.store_objects()
        containers = {*self._containers, *holders}
        containers_now = listed(("ps", "--all"), "{{.Names}}") - self._preexisting_containers
        gate_prefix = f"enginery-gate-{self.rid}"
        return StrayObjects(
            containers=tuple(
                sorted(
                    name
                    for name in containers_now
                    if name.startswith("enginery-") and (name in containers or self.rid in name)
                )
            ),
            volumes=tuple(
                sorted(
                    name for name in listed(("volume", "ls"), "{{.Name}}") if name in set(volumes)
                )
            ),
            networks=tuple(
                sorted(
                    name
                    for name in listed(("network", "ls"), "{{.Name}}")
                    if name in self._networks or name.startswith(gate_prefix)
                )
            ),
            listing_errors=tuple(problems),
        )


def prepare_gate(
    options: GateOptions,
    evidence: EvidenceBuilder,
    scrubber: Scrubber,
    engine_factory: Callable[[str | None], OciEngine],
    *,
    need_relay: bool,
) -> Gate:
    """Qualify the engine and images and build the host fixture, or raise ``GateHoldError``."""
    engine = engine_factory(options.docker_context)
    try:
        identity = engine.qualify()
    except (MissingPrerequisiteError, OperationTimeoutError) as error:
        raise GateHoldError(f"no qualified container engine is available: {error}") from error
    notes: list[str] = []
    created: list[str] = []
    try:
        runner_image = _resolve_image(
            engine,
            explicit=options.runner_image,
            verify=verify_image,
            build=build_image,
            tag=default_tag(),
            noun="runner",
            created_tags=created,
            notes=notes,
        )
        relay_image: ImageIdentity | None = None
        if need_relay:
            relay_image = _resolve_image(
                engine,
                explicit=options.relay_image,
                verify=verify_relay_image,
                build=build_relay_image,
                tag=default_relay_tag(),
                noun="relay",
                created_tags=created,
                notes=notes,
            )
    except GateHoldError:
        for tag in created:
            engine.run_cli(("image", "rm", tag), timeout=120)
        raise
    try:
        before = set(iter_enginery_containers(engine))
    except EngineryError as error:
        raise GateHoldError(f"the engine cannot list containers: {error}") from error
    try:
        workroot = Path(os.path.realpath(_make_workroot()))
    except OSError as error:
        raise GateHoldError(f"the gate cannot create its working directory: {error}") from error
    gate = Gate(
        options=options,
        engine=engine,
        evidence=evidence,
        scrubber=scrubber,
        engine_identity=identity,
        runner_image=runner_image,
        relay_image=relay_image,
        workroot=workroot,
        created_tags=created,
        notes=notes,
    )
    gate._preexisting_containers = before
    evidence.raw_writer = gate.raw_write
    gate.profile = RunnerProfile(network=gate.internal_network)
    gate.limits_profile = RunnerProfile(
        network=gate.internal_network,
        resources=ResourceLimits(
            memory_mib=128, cpus=0.5, pids=32, tmp_mib=32, home_mib=16, stop_grace_seconds=2
        ),
    )
    try:
        gate.fixture = build_fixture(workroot / "host")
    except Exception as error:
        gate.cleanup()
        raise GateHoldError(f"the gate could not build its host fixture: {error}") from error
    for label, canary in gate.fixture.canaries.items():
        scrubber.register(f"canary:{label}", canary.value)
    gate.stubs = StubNetwork()
    gate.stubs.start()
    return gate


def _make_workroot() -> str:
    import tempfile

    # The fixture's absolute host paths must not name anything a worker container can write
    # (its tmpfs /tmp and home, the workspace and result mounts): a write to such a path would
    # succeed inside the container and be mistaken for a host write. When the platform's temp
    # directory is one of those names (Linux), use a directory under the operator's home.
    base = Path(tempfile.gettempdir())
    container_writable = ("/tmp", "/var/tmp", "/home/worker", "/workspace", "/out", "/dev/shm")
    if str(base).startswith(container_writable):
        base = Path.home() / ".cache" / "enginery-gate"
        base.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="enginery-gate-", dir=base)


def receipt_summary(receipt: RunnerReceipt | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    effective = receipt.effective
    return {
        "profile_digest": str(receipt.profile_digest),
        "image_id": receipt.image.image_id,
        "engine": receipt.engine.kind,
        "user": effective.user,
        "cap_drop": list(effective.cap_drop),
        "cap_add": list(effective.cap_add),
        "security_options": list(effective.security_options),
        "privileged": effective.privileged,
        "read_only_rootfs": effective.read_only_rootfs,
        "memory_bytes": effective.memory_bytes,
        "memory_swap_bytes": effective.memory_swap_bytes,
        "nano_cpus": effective.nano_cpus,
        "pids_limit": effective.pids_limit,
        "network_mode": effective.network_mode,
        "networks": list(effective.networks),
        "mounts": [list(mount) for mount in effective.mounts],
        "devices": list(effective.devices),
    }


def summarize_operations(operations: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": op["id"],
            "expected": op["expected"],
            "outcome": op["outcome"],
            "error": op.get("error"),
            "ok": op["ok"],
        }
        for op in operations
    ]


def failing_operations(operations: Sequence[dict[str, Any]], prefix: str = "") -> list[str]:
    return [op["id"] for op in operations if op["id"].startswith(prefix) and not op["ok"]]


__all__ = [
    "GATE_LABEL",
    "Gate",
    "GateHoldError",
    "GateOptions",
    "UpstreamStub",
    "failing_operations",
    "host_identity",
    "prepare_gate",
    "receipt_summary",
    "subject_identity",
    "summarize_operations",
]


if __name__ == "__main__":  # pragma: no cover - this module is imported, not run
    sys.exit("factory_gate_context is a library; run run_factory_gate.py")
