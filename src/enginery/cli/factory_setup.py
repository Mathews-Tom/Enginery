"""``enginery init``: preview and apply guided project setup.

Setup resolves explicit choices into a project policy file and nothing else. A plan is
computed first and is the only thing ``--apply`` performs: it lists every file setup would
create (path, tracked or private, action), every capability install it leaves to the
operator, every network access and every authentication requirement. The default is the
preview, which writes nothing.

* The project policy (``<project>/.enginery/project.toml``) is created, never overwritten:
  when the file exists and differs from what init would write it is user-owned, and init
  reports a conflict and writes nothing at all.
* The private ownership record (see :mod:`enginery.cli.setup_files`) lists the files setup
  created so ``enginery uninstall`` removes only what is still exactly as setup wrote it.
* Capabilities are never installed by init. For each selected capability that is not
  already installed it returns an actionable hold naming the exact ``enginery capability``
  commands; approvals stay digest-bound through the policy registry.
* Credentials, OS services (launchd, systemd, cron), MCP servers and global configuration are
  never touched: authentication is only detected read-only, by ``enginery preflight``.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from enginery.adapters.git_reader import git_bytes
from enginery.application.behavioral_checks import BehavioralCheck
from enginery.application.effective_config import CompiledConfig, compile_effective_config
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.layered_config import (
    ConfigError,
    LayerDocument,
    parse_layer_document,
    run_layer,
)
from enginery.application.project_policy import (
    BUILTIN_CAPABILITIES,
    SetupChoices,
    render_project_policy,
)
from enginery.capabilities.install import load_owned_manifest
from enginery.capabilities.manifest import validate_name, validate_version
from enginery.cli.config_binding import checks_at_base
from enginery.cli.config_locations import (
    PROJECT_POLICY_RELATIVE,
    private_state_directory,
    read_project_layer,
    read_user_layer,
    record_absent_sources,
    require_plain_path,
)
from enginery.cli.setup_files import (
    OWNERSHIP_FILE,
    OwnedFile,
    OwnershipRecord,
    create_directory,
    create_exclusive,
    load_ownership,
    read_regular,
    refuse_links,
    require_private_path,
    sha256_bytes,
    write_private_atomically,
)
from enginery.cli.stage1_request import _probe_host
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError
from enginery.domain.execution_selection import SelectionLayer

PROJECT_POLICY_ROLE = "project-policy"
DIRECTORY_ROLE = "directory"
OWNERSHIP_ROLE = "ownership-record"
POLICY_DIRECTORY_RELATIVE = Path(".enginery")
INSTALL_ROOT_RELATIVE = Path(".enginery") / "installed"
LOCKFILE_RELATIVE = Path(".enginery") / "capabilities.lock.json"


@dataclass(frozen=True, slots=True)
class ProjectConfiguration:
    """The configuration of one project as every setup command sees it: one compiler output."""

    project: Path
    project_layer: LayerDocument | None
    user_layer: LayerDocument | None
    compiled: CompiledConfig | None
    undeclared: ConfigError | None
    harness_probe_failure: str | None = None


def resolve_project(path: Path | None) -> Path:
    """The project directory: an existing directory, resolved to its real path."""
    project = (Path.cwd() if path is None else path).resolve()
    if not project.is_dir():
        raise InvalidInputError(
            "the project must be an existing directory", details={"project": str(project)}
        )
    return project


def load_project_configuration(
    project: Path, env: Mapping[str, str] | None = None
) -> ProjectConfiguration:
    """Read the project and user layers and compile them; the one resolution path.

    A project that declares no trust mode in any layer is reported as ``undeclared`` rather
    than raised, so doctor and explain can say what to do. Every other refusal is raised.
    """
    project_layer = read_project_layer(project)
    user_layer = read_user_layer(env)
    harness_probe_failure: str | None = None
    checks: tuple[BehavioralCheck, ...]
    try:
        base = git_bytes(project, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except InvalidInputError:
        # Setup also diagnoses directories that have not yet become repositories.
        checks = ()
    else:
        checks = checks_at_base(project, base)
    try:
        compiled = compile_effective_config(
            project=project_layer,
            user=user_layer,
            run=run_layer({}),
            probe_host=_probe_host,
            behavioral_checks=checks,
        )
    except MissingPrerequisiteError as error:
        harness_probe_failure = str(error)
        compiled = compile_effective_config(
            project=project_layer,
            user=user_layer,
            run=run_layer({}),
            probe_host=None,
            behavioral_checks=checks,
        )
    except ConfigError as error:
        if error.key == "runner.mode" and error.file == "<all layers>":
            return ProjectConfiguration(project, project_layer, user_layer, None, error)
        raise
    return ProjectConfiguration(
        project,
        project_layer,
        user_layer,
        record_absent_sources(compiled, project, env),
        None,
        harness_probe_failure,
    )


@dataclass(frozen=True, slots=True)
class FileAction:
    """One file setup would create or leave alone."""

    path: Path
    role: str
    tracking: str  # "tracked" (in the repository) or "private" (machine state)
    action: str  # "create", "update", "unchanged" or "conflict"
    sha256: str
    detail: str
    payload: bytes

    def to_json(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "role": self.role,
            "tracking": self.tracking,
            "action": self.action,
            "sha256": self.sha256,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class CapabilityHold:
    """A selected capability that is not installed, and the exact commands that fix it."""

    name: str
    version: str | None
    detail: str
    commands: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "prerequisite": f"capability {self.name}",
            "detail": self.detail,
            "commands": list(self.commands),
        }


@dataclass(frozen=True, slots=True)
class SetupPlan:
    """Everything ``init`` would do, in the exact form ``--apply`` performs."""

    project: Path
    files: tuple[FileAction, ...]
    installs: tuple[CapabilityHold, ...]
    network: tuple[str, ...]
    auth: tuple[Mapping[str, str], ...]
    configuration_digest: str
    ownership_record: OwnershipRecord | None
    harness_probe_failure: str | None = None
    harness_probe_resolve: str | None = None

    @property
    def conflicts(self) -> tuple[FileAction, ...]:
        return tuple(item for item in self.files if item.action == "conflict")

    def to_json(self, *, applied: bool, performed: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "mode": "apply" if applied else "preview",
            "project": str(self.project),
            "files": [item.to_json() for item in self.files],
            "installs": [
                {
                    "capability": item.name,
                    "version": item.version,
                    "performed_by_init": False,
                    "detail": item.detail,
                    "commands": list(item.commands),
                }
                for item in self.installs
            ],
            "network_access": list(self.network),
            "auth_requirements": [dict(item) for item in self.auth],
            "effective_configuration_digest": self.configuration_digest,
            "holds": [item.to_json() for item in self.installs]
            + (
                [
                    {
                        "name": "harness",
                        "detail": self.harness_probe_failure,
                        "resolve": self.harness_probe_resolve,
                    }
                ]
                if self.harness_probe_failure is not None
                else []
            ),
            "conflicts": [str(item.path) for item in self.conflicts],
            "performed": list(performed),
            "status": (
                "conflict"
                if self.conflicts
                else "hold"
                if self.installs or self.harness_probe_failure
                else "ok"
            ),
        }


def _capability_commands(name: str, version: str | None) -> tuple[str, ...]:
    request = f"{name}@{version or '<version>'}"
    sources = "--project-source <REPOSITORY>@<COMMIT> --project-subdir <DIR>"
    return (
        f"enginery capability preview {request} {sources}",
        f"enginery capability install {request} {sources}",
    )


def capability_holds(
    project: Path, names: tuple[str, ...], versions: Mapping[str, str]
) -> tuple[CapabilityHold, ...]:
    """The selected capabilities that are not installed in the project's install root."""
    manifest = load_owned_manifest(project / INSTALL_ROOT_RELATIVE)
    holds: list[CapabilityHold] = []
    for name in names:
        if name in BUILTIN_CAPABILITIES:
            continue
        installed = None if manifest is None else manifest.package(name)
        if installed is not None:
            continue
        version = versions.get(name)
        holds.append(
            CapabilityHold(
                name=name,
                version=version,
                detail=(
                    f"capability {name} is selected but not installed under "
                    f"{INSTALL_ROOT_RELATIVE}; setup never installs capabilities, and an "
                    "install that needs approval stays held until an owner approves the "
                    "exact closure and lock digest"
                ),
                commands=_capability_commands(name, version),
            )
        )
    return tuple(holds)


def runner_resolution_command(compiled: CompiledConfig, command: str) -> str:
    """Name a runner repair against the same engine as the compiled configuration."""
    result = f"enginery runner {command}"
    context = compiled.runner.docker_context
    if context is not None:
        result += f" --docker-context {shlex.quote(context)}"
    return result


def _auth_requirements(compiled: CompiledConfig) -> tuple[Mapping[str, str], ...]:
    config = compiled.config
    requirements: list[Mapping[str, str]] = [
        {
            "name": "github",
            "detail": "an authenticated GitHub CLI session; detected read-only with "
            f"`{config.text('github_executable')} auth status`, never created or changed by setup",
            "resolve": f"{config.text('github_executable')} auth login",
        },
        {
            "name": "harness",
            "detail": f"an authenticated {compiled.selection.harness} session; the harness "
            "resolves its own login and setup never reads, stores or changes it",
            "resolve": f"sign in with the {compiled.selection.harness} CLI",
        },
    ]
    if compiled.runner.isolated_mode:
        requirements.append(
            {
                "name": "container-engine",
                "detail": "a qualified container engine and the pinned runner image",
                "resolve": runner_resolution_command(compiled, "build-image"),
            }
        )
    return tuple(requirements)


def harness_probe_resolution(compiled: CompiledConfig) -> str:
    """Resolve a native-control host hold without changing project-owned configuration."""
    harness = compiled.selection.harness
    expectation = load_execution_catalog().host(harness).version.expectation
    return (
        f"install a {harness} harness version matching {expectation}; set harness in "
        "[executables] in $XDG_CONFIG_HOME/enginery/user.toml "
        "(or pass --harness-executable for the run)"
    )


def plan_init(
    project: Path,
    choices: SetupChoices,
    *,
    capability_versions: Mapping[str, str],
    env: Mapping[str, str] | None = None,
) -> SetupPlan:
    """Preview setup without writes.

    With a model or effort configured under trusted-local, run only the selected harness's
    read-only version probe, the same probe build-request runs.
    """
    for name in choices.capabilities:
        validate_name(name)
    for name, version in capability_versions.items():
        validate_name(name)
        validate_version(version)
    rendered = render_project_policy(choices)
    policy_path = project / PROJECT_POLICY_RELATIVE
    # The rendered policy must be a valid layer and must compile with the user's layer, so a
    # choice the compiler would refuse is refused here, before any file is touched.
    document = parse_layer_document(
        SelectionLayer.PROJECT, rendered, source=str(policy_path), reject_credentials=True
    )
    user_layer = read_user_layer(env)
    harness_probe_failure: str | None = None
    try:
        compiled = compile_effective_config(
            project=document, user=user_layer, run=run_layer({}), probe_host=_probe_host
        )
    except MissingPrerequisiteError as error:
        harness_probe_failure = str(error)
        compiled = compile_effective_config(
            project=document, user=user_layer, run=run_layer({}), probe_host=None
        )
    compiled = record_absent_sources(compiled, project, env)

    require_plain_path(project, PROJECT_POLICY_RELATIVE, what="the project policy path")
    refuse_links(project, PROJECT_POLICY_RELATIVE, what="the project policy path")
    state_directory = private_state_directory(project, env)
    ownership_path = state_directory / OWNERSHIP_FILE
    require_private_path(ownership_path, what="the setup ownership record")
    record = load_ownership(ownership_path, project=project)
    digest = sha256_bytes(rendered)
    existing = read_regular(policy_path, what="the project policy file")
    if existing is None:
        policy = FileAction(
            policy_path,
            PROJECT_POLICY_ROLE,
            "tracked",
            "create",
            digest,
            "created by setup and recorded as setup-owned",
            rendered,
        )
    elif existing == rendered:
        owned = record is not None and record.owns(str(policy_path)) is not None
        policy = FileAction(
            policy_path,
            PROJECT_POLICY_ROLE,
            "tracked",
            "unchanged",
            digest,
            "already exactly what init would write"
            + (" (setup-owned)" if owned else " (user-owned: setup did not create it)"),
            rendered,
        )
    else:
        policy = FileAction(
            policy_path,
            PROJECT_POLICY_ROLE,
            "tracked",
            "conflict",
            sha256_bytes(existing),
            "exists and differs from what init would write; it is user-owned and is never "
            "overwritten (edit it yourself, or remove it and run init again)",
            rendered,
        )
    files = [policy]
    updated: OwnershipRecord | None = record
    if policy.action == "create":
        base = record or OwnershipRecord(project=str(project), files=())
        policy_directory = project / POLICY_DIRECTORY_RELATIVE
        if not os.path.lexists(policy_directory):
            files.insert(
                0,
                FileAction(
                    policy_directory,
                    DIRECTORY_ROLE,
                    "tracked",
                    "create",
                    "",
                    "directory created by setup; removed by uninstall only while it is empty",
                    b"",
                ),
            )
            base = base.with_file(OwnedFile(str(policy_directory), "", DIRECTORY_ROLE))
        updated = base.with_file(OwnedFile(str(policy_path), digest, PROJECT_POLICY_ROLE))
        files.append(
            FileAction(
                ownership_path,
                OWNERSHIP_ROLE,
                "private",
                "create" if record is None else "update",
                sha256_bytes(updated.to_bytes()),
                "private record of the files setup created, so uninstall removes only those",
                updated.to_bytes(),
            )
        )
    elif record is not None:
        files.append(
            FileAction(
                ownership_path,
                OWNERSHIP_ROLE,
                "private",
                "unchanged",
                sha256_bytes(record.to_bytes()),
                "already records what setup created",
                record.to_bytes(),
            )
        )
    return SetupPlan(
        project=project,
        files=tuple(files),
        installs=capability_holds(project, choices.capabilities, capability_versions),
        network=(),
        auth=_auth_requirements(compiled),
        configuration_digest=str(compiled.config.digest),
        ownership_record=updated,
        harness_probe_failure=harness_probe_failure,
        harness_probe_resolve=(
            harness_probe_resolution(compiled) if harness_probe_failure is not None else None
        ),
    )


def apply_plan(plan: SetupPlan) -> tuple[str, ...]:
    """Persist ownership before tracked files and roll it back on a tracked-write failure."""
    if plan.conflicts or plan.harness_probe_failure is not None:
        return ()
    performed: list[str] = []
    records = [
        item
        for item in plan.files
        if item.tracking == "private" and item.action in ("create", "update")
    ]
    prior: dict[Path, bytes | None] = {}
    missing_state_directories: list[Path] = []
    for item in records:
        require_private_path(item.path, what="the setup ownership record")
        prior[item.path] = read_regular(item.path, what="the setup ownership record")
        directory = item.path.parent
        while not directory.exists():
            missing_state_directories.append(directory)
            directory = directory.parent
        write_private_atomically(item.path, item.payload, what="the setup ownership record")
        performed.append(str(item.path))
    created: list[Path] = []
    policy_directory_existed = (plan.project / PROJECT_POLICY_RELATIVE.parent).exists()
    try:
        for item in plan.files:
            if item.tracking != "tracked" or item.action != "create":
                continue
            if item.role == DIRECTORY_ROLE:
                create_directory(
                    plan.project, POLICY_DIRECTORY_RELATIVE, what="the policy directory"
                )
            else:
                created.append(
                    create_exclusive(
                        plan.project,
                        PROJECT_POLICY_RELATIVE,
                        item.payload,
                        what="the project policy",
                    )
                )
            performed.append(str(item.path))
    except BaseException:
        for path in created:
            path.unlink()
        policy_directory = plan.project / PROJECT_POLICY_RELATIVE.parent
        if (
            not policy_directory_existed
            and policy_directory.is_dir()
            and not policy_directory.is_symlink()
            and not any(policy_directory.iterdir())
        ):
            policy_directory.rmdir()
        for path, payload in prior.items():
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                write_private_atomically(path, payload, what="the setup ownership record")
        for directory in missing_state_directories:
            directory.rmdir()
        raise
    return tuple(performed)


__all__ = [
    "DIRECTORY_ROLE",
    "INSTALL_ROOT_RELATIVE",
    "LOCKFILE_RELATIVE",
    "OWNERSHIP_ROLE",
    "POLICY_DIRECTORY_RELATIVE",
    "PROJECT_POLICY_ROLE",
    "CapabilityHold",
    "FileAction",
    "ProjectConfiguration",
    "SetupPlan",
    "apply_plan",
    "capability_holds",
    "harness_probe_resolution",
    "load_project_configuration",
    "plan_init",
    "resolve_project",
]
