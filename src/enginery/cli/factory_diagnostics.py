"""``enginery factory doctor``: lazy, read-only prerequisite diagnostics.

Only what the project's compiled selection needs is checked: git always; the GitHub CLI and
its read-only ``auth status`` for the GitHub-backed Stage 1 workflow; the selected harness
executable through its existing host probe against the packaged catalog; and a container
engine plus the pinned image only for an ``isolated-oci`` run. Every missing item is a hold
that names the exact prerequisite and the exact command that resolves it; nothing is ever
reported as ready on a guess, and doctor never creates, installs, signs in or changes
anything. The compiled configuration is the same object ``explain`` and ``build-request`` use.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from enginery.adapters.claude_code import probe_installed_host as probe_installed_claude
from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.omp import probe_installed_host as probe_installed_omp
from enginery.application.effective_config import CompiledConfig
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.project_policy import BUILTIN_CAPABILITIES
from enginery.capabilities.install import load_owned_manifest
from enginery.capabilities.serialization import is_legacy_lock, lock_from_json, read_lock_document
from enginery.cli.factory_setup import (
    INSTALL_ROOT_RELATIVE,
    LOCKFILE_RELATIVE,
    ProjectConfiguration,
)
from enginery.domain.errors import EngineryError, MissingPrerequisiteError
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.execution_selection import HoldReason
from enginery.domain.runner import RunnerProfile

_TIMEOUT_SECONDS = 30

type Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
type HostProbe = Callable[[str], InstalledHost | None]


def _run(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )


@dataclass(frozen=True, slots=True)
class Check:
    """One prerequisite: ``ok``, ``hold`` (with the command that resolves it), or not needed."""

    name: str
    status: str
    detail: str
    resolve: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "resolve": self.resolve,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    project: Path
    checks: tuple[Check, ...]
    network: tuple[str, ...]
    configuration_digest: str | None

    @property
    def holds(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if check.status == "hold")

    @property
    def status(self) -> str:
        return "hold" if self.holds else "ok"

    def to_json(self) -> dict[str, object]:
        return {
            "project": str(self.project),
            "status": self.status,
            "effective_configuration_digest": self.configuration_digest,
            "checks": [check.to_json() for check in self.checks],
            "holds": [check.to_json() for check in self.holds],
            "network_access": list(self.network),
            "mutations": [],
        }


def _executable(name: str) -> str | None:
    return shutil.which(name)


def _check_git(run: Runner) -> Check:
    if _executable("git") is None:
        return Check(
            "git",
            "hold",
            "git is not on PATH; every Stage 1 run needs it",
            "install git and make sure `git --version` runs (https://git-scm.com/downloads)",
        )
    try:
        completed = run(("git", "--version"))
    except (OSError, subprocess.TimeoutExpired) as error:
        return Check(
            "git", "hold", f"`git --version` could not run: {error}", "repair the git install"
        )
    if completed.returncode != 0:
        return Check(
            "git",
            "hold",
            f"`git --version` exited {completed.returncode}",
            "repair the git install",
        )
    first = completed.stdout.strip().splitlines()[:1]
    return Check("git", "ok", first[0] if first else "git runs")


def _check_github(executable: str, run: Runner) -> list[Check]:
    if _executable(executable) is None:
        return [
            Check(
                "github-cli",
                "hold",
                f"the GitHub CLI `{executable}` is not on PATH; Stage 1 reads issues and "
                "opens pull requests through it",
                "install the GitHub CLI (https://cli.github.com), or point [executables] github "
                "at it in .enginery/project.toml",
            )
        ]
    checks = [Check("github-cli", "ok", f"`{executable}` is on PATH")]
    try:
        status = run((executable, "auth", "status"))
    except (OSError, subprocess.TimeoutExpired) as error:
        checks.append(
            Check(
                "github-auth",
                "hold",
                f"`{executable} auth status` could not run: {error}",
                f"{executable} auth login",
            )
        )
        return checks
    if status.returncode == 0:
        checks.append(Check("github-auth", "ok", "a GitHub CLI session is signed in"))
    else:
        checks.append(
            Check(
                "github-auth",
                "hold",
                f"`{executable} auth status` reports no signed-in session; doctor only detects "
                "authentication and never creates or changes it",
                f"{executable} auth login",
            )
        )
    return checks


def _default_probe(harness: str, executable: str) -> InstalledHost | None:
    if harness == "claude-code":
        return probe_installed_claude(executable)
    return probe_installed_omp(executable)


def _check_harness(compiled: CompiledConfig, probe: HostProbe | None) -> Check:
    harness = compiled.selection.harness
    executable = compiled.config.text("harness_executable")
    resolve = (
        f"install the {harness} CLI and sign in, or point [executables] harness at it "
        "in .enginery/project.toml"
    )
    if compiled.runner.isolated_mode:
        return Check(
            f"harness-{harness}",
            "not-needed",
            "an isolated run executes the pinned image's harness, never the host binary",
        )
    if _executable(executable) is None:
        return Check(f"harness-{harness}", "hold", f"`{executable}` is not on PATH", resolve)
    installed = (probe or (lambda name: _default_probe(harness, name)))(executable)
    host = load_execution_catalog().host(harness)
    try:
        host.require_installed(installed)
    except MissingPrerequisiteError as error:
        return Check(f"harness-{harness}", "hold", str(error), resolve)
    assert installed is not None
    return Check(
        f"harness-{harness}",
        "ok",
        f"`{executable}` identifies as {installed.executable_version} "
        f"({installed.protocol_fingerprint}), accepted by the packaged catalog",
    )


def trust_mode_resolution(policy_path: Path) -> str:
    """Give an actionable trust-mode choice without overwriting an existing policy."""
    if policy_path.exists():
        return (
            f"edit {policy_path} (or the user preferences file): add [runner] with "
            'mode = "trusted-local" or mode = "isolated-oci"'
        )
    return "enginery factory init --runner <trusted-local|isolated-oci> --apply"


def _check_runner(compiled: CompiledConfig) -> Check:
    decision = OciRunnerAdmission(profile=RunnerProfile()).admit(compiled.runner)
    if decision.hold is None:
        return Check("container-runner", "ok", "the engine and the pinned image are admitted")
    message = decision.hold.message
    if "relay" in message:
        resolve = (
            "enginery runner build-relay-image, then enginery runner relay up "
            "--image <relay image id>, then enginery runner relay status"
        )
    elif decision.hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT or "image" in message:
        resolve = "enginery runner build-image, then `enginery runner status --image <id>`"
    else:
        resolve = (
            "install, start and qualify the container engine; choose --docker-context "
            "or set [runner] docker_context, then enginery runner status --image <id>"
        )
    return Check(
        "container-runner",
        "hold",
        decision.hold.message,
        resolve,
    )


def _locked_versions(project: Path) -> Mapping[str, str]:
    path = project / LOCKFILE_RELATIVE
    try:
        document = read_lock_document(path)
        if is_legacy_lock(document):
            return {}
        return {entry.name: entry.version for entry in lock_from_json(document).entries}
    except (EngineryError, OSError):
        return {}


def _check_capabilities(project: Path, compiled: CompiledConfig) -> list[Check]:
    names = [
        name
        for name in compiled.config.strings("permitted_capabilities")
        if name not in BUILTIN_CAPABILITIES
    ]
    if not names:
        return []
    manifest = load_owned_manifest(project / INSTALL_ROOT_RELATIVE)
    versions = _locked_versions(project)
    checks: list[Check] = []
    for name in names:
        if manifest is not None and manifest.package(name) is not None:
            checks.append(Check(f"capability-{name}", "ok", f"{name} is installed"))
            continue
        request = f"{name}@{versions.get(name, '<version>')}"
        sources = "--project-source <REPOSITORY>@<COMMIT> --project-subdir <DIR>"
        checks.append(
            Check(
                f"capability-{name}",
                "hold",
                f"capability {name} is permitted but not installed under {INSTALL_ROOT_RELATIVE}; "
                "an install that needs approval stays held until an owner approves the exact "
                "closure and lock digest",
                f"enginery capability preview {request} {sources} && "
                f"enginery capability install {request} {sources}",
            )
        )
    return checks


def run_doctor(
    configuration: ProjectConfiguration,
    *,
    run: Runner = _run,
    probe: HostProbe | None = None,
) -> DoctorReport:
    """Check the prerequisites the compiled selection needs; read-only."""
    compiled = configuration.compiled
    if compiled is None:
        return DoctorReport(
            project=configuration.project,
            checks=(
                Check(
                    "trust-mode",
                    "hold",
                    "no layer declares a trust mode (trusted-local or isolated-oci), so the "
                    "prerequisites cannot be derived",
                    trust_mode_resolution(configuration.project / ".enginery" / "project.toml"),
                ),
            ),
            network=(),
            configuration_digest=None,
        )
    checks = [_check_git(run), *_check_github(compiled.config.text("github_executable"), run)]
    checks.append(_check_harness(compiled, probe))
    if compiled.runner.isolated_mode:
        checks.append(_check_runner(compiled))
    checks.extend(_check_capabilities(configuration.project, compiled))
    return DoctorReport(
        project=configuration.project,
        checks=tuple(checks),
        network=(
            f"`{compiled.config.text('github_executable')} auth status` may contact the GitHub API",
        ),
        configuration_digest=str(compiled.config.digest),
    )


__all__ = ["Check", "DoctorReport", "run_doctor", "trust_mode_resolution"]
