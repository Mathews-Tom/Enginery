"""Helpers for tests that run the real harness binaries against a stub model server.

These tests are the only place a real ``claude`` or ``omp`` process is started. They never use
a real credential or reach a hosted endpoint: the harness is pointed at a stub with a throw-away
HOME and configuration directory and a scrubbed environment.

The proofs must exercise the harness version the execution catalog tested (never merely a
version inside its accepted range). When the binary on
``PATH`` is another version (harnesses auto-upgrade), ``omp`` is run instead inside a throw-away
container of the pinned worker image, which is the binary the isolated runner actually uses.
The container carries the production hardening flags; only the test adds ``--add-host`` so the
container reaches the stub, which then listens on every interface for the length of the test.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from enginery.adapters.claude_code import probe_installed_host as probe_claude
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.image import default_tag
from enginery.adapters.oci.launch import hardening_arguments, probe_arguments
from enginery.adapters.omp import probe_installed_host as probe_omp
from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.errors import EngineryError, MissingPrerequisiteError
from enginery.domain.execution_catalog import HostCapability, InstalledHost
from enginery.domain.runner import WORKSPACE_MOUNT, RunnerProfile
from tests.support.loopback_model import Flavor, LoopbackModelServer, ToolRequest

_Probe = Callable[..., InstalledHost | None]
_PROBES: dict[str, tuple[str, _Probe]] = {
    "claude-code": ("claude", probe_claude),
    "omp": ("omp", probe_omp),
}
_IMAGE_CAPABLE = frozenset({"omp"})
"""Harnesses whose proofs are wired to run in the worker image (claude runs on the host)."""

CONTAINER_STUB_HOST = "host.docker.internal"
"""How a Docker Desktop container reaches the test's stub on the host."""

_CONTAINER_TIMEOUT_SECONDS = 240.0
_PROBE_TIMEOUT_SECONDS = 120.0

Target = Literal["host", "image", "skip"]


@dataclass(frozen=True, slots=True)
class HostFacts:
    """What was observed about the harness binary on ``PATH``."""

    found: bool
    installed_version: str | None
    matches_catalog: bool

    def detail(self, executable: str, catalog_version: str) -> str:
        if not self.found:
            return f"no {executable} binary is installed on PATH"
        if self.installed_version is None:
            return f"the {executable} binary on PATH did not identify itself"
        if not self.matches_catalog:
            return (
                f"the {executable} binary on PATH is {self.installed_version}, "
                f"not the catalog's tested {catalog_version}"
            )
        return f"the {executable} binary on PATH is the catalog's tested {catalog_version}"


@dataclass(frozen=True, slots=True)
class ImageFacts:
    """What was observed about the pinned worker image's harness binary."""

    usable: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Selection:
    """Where the real-binary proof runs, or why it cannot."""

    target: Target
    reason: str


def select_target(
    executable: str,
    catalog_version: str,
    host: HostFacts,
    image: ImageFacts | None,
) -> Selection:
    """Choose the binary a proof runs against; a skip names both the host and the image.

    The host binary is used only when it is exactly the catalog's tested version. Otherwise the
    pinned image's binary is used if it is usable. ``image`` is None for a harness whose proofs
    are not wired to run in the image.
    """
    host_detail = host.detail(executable, catalog_version)
    if host.found and host.matches_catalog:
        return Selection("host", host_detail)
    if image is None:
        return Selection(
            "skip",
            f"{host_detail}; running {executable} in the worker image is not wired for it",
        )
    if image.usable:
        return Selection("image", f"{host_detail}; using the worker image ({image.detail})")
    return Selection(
        "skip",
        f"{host_detail}; and the worker image cannot be used ({image.detail})",
    )


def _host_facts(host: HostCapability, found: str | None, probe: _Probe) -> HostFacts:
    if found is None:
        return HostFacts(False, None, False)
    installed = probe(found)
    if installed is None:
        return HostFacts(True, None, False)
    return HostFacts(True, installed.executable_version, host.matches_tested(installed))


def _docker_probe_runner(
    engine: OciEngine, profile: RunnerProfile, image: str
) -> Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]:
    """A ``probe_installed_host`` runner that runs the probe command in the image."""

    def run(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        name = f"enginery-test-probe-{uuid.uuid4().hex[:10]}"
        return engine.run_cli(
            probe_arguments(profile, image, arguments, name=name), timeout=_PROBE_TIMEOUT_SECONDS
        )

    return run


@functools.cache
def _image_facts(harness: str) -> tuple[ImageFacts, str | None]:
    """Whether the pinned worker image's harness is the catalog's tested executable."""
    executable, probe = _PROBES[harness]
    catalog_host = load_execution_catalog().host(harness)
    engine = OciEngine()
    try:
        engine.qualify()
    except MissingPrerequisiteError as error:
        return ImageFacts(False, f"no qualified container engine is reachable: {error}"), None
    reference = default_tag()
    try:
        engine.image_inspect(reference)
    except MissingPrerequisiteError as error:
        return ImageFacts(False, f"image {reference} is not available: {error}"), None
    runner = _docker_probe_runner(engine, RunnerProfile(), reference)
    try:
        installed = probe(executable, runner)
    except EngineryError as error:
        return ImageFacts(False, f"image {reference} could not be probed: {error}"), None
    if installed is None:
        return ImageFacts(False, f"{executable} in image {reference} did not identify itself"), None
    if not catalog_host.matches_tested(installed):
        return (
            ImageFacts(
                False,
                f"{executable} in image {reference} is {installed.executable_version}, "
                f"not the catalog's tested {catalog_host.tested_executable_version}",
            ),
            None,
        )
    return ImageFacts(
        True, f"{executable} {installed.executable_version} in image {reference}"
    ), reference


@dataclass(frozen=True, slots=True)
class RealHarness:
    """A real harness binary to run: on the host, or inside the pinned worker image."""

    harness: str
    executable: str
    version: str
    binary: Path | None
    image: str | None
    reason: str

    @property
    def in_image(self) -> bool:
        return self.image is not None

    @property
    def stub_host(self) -> str:
        """The host name the harness uses to reach the stub."""
        return CONTAINER_STUB_HOST if self.in_image else "127.0.0.1"

    def stub(self, flavor: Flavor, turns: Sequence[Sequence[ToolRequest]]) -> LoopbackModelServer:
        """A stub the harness can reach: every interface in the image case, loopback otherwise."""
        return LoopbackModelServer(
            flavor,
            turns,
            bind_host="0.0.0.0" if self.in_image else "127.0.0.1",
        )

    def stub_url(self, server: LoopbackModelServer) -> str:
        return server.url_for(self.stub_host)

    def view(self, proof: ProofWorkspace, path: Path) -> Path:
        """``path`` (inside the proof workspace) as the harness sees it."""
        if not self.in_image:
            return path
        return Path(WORKSPACE_MOUNT) / path.relative_to(proof.workspace)

    def run(
        self,
        command: Sequence[str],
        proof: ProofWorkspace,
        worker_environment: Mapping[str, str],
        *,
        home_files: Mapping[str, str] | None = None,
        host_paths: Mapping[str, str] | None = None,
        extra_environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``command`` (argv[0] is the harness) with the stub-reachable scrubbed environment.

        ``home_files`` are written beneath the harness's HOME. ``host_paths`` rewrites exact
        argv entries that only exist inside the image when running on the host.
        """
        files = home_files or {}
        extra = extra_environment or {}
        if self.in_image:
            return self._run_in_image(command, proof, worker_environment, files, extra)
        assert self.binary is not None
        environment = scrubbed_environment(self.binary, proof, worker_environment)
        environment.update(extra)
        for relative, content in files.items():
            target = proof.home / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        host_command = [str(self.binary)]
        mapping = {WORKSPACE_MOUNT: str(proof.workspace), **(host_paths or {})}
        host_command += [mapping.get(argument, argument) for argument in command[1:]]
        return subprocess.run(
            host_command,
            cwd=proof.workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    def _run_in_image(
        self,
        command: Sequence[str],
        proof: ProofWorkspace,
        worker_environment: Mapping[str, str],
        home_files: Mapping[str, str],
        extra_environment: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert self.image is not None
        environment = dict(worker_environment)
        # Nothing listens on the discard port: any attempt to reach a host other than the stub
        # (a harness's own catalog or telemetry lookups) is refused locally.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            environment[name] = "http://127.0.0.1:9"
        for name in ("NO_PROXY", "no_proxy"):
            environment[name] = f"127.0.0.1,localhost,{CONTAINER_STUB_HOST}"
        environment.update(extra_environment)
        home = environment["HOME"]
        seeds: list[str] = []
        seed_arguments: list[str] = []
        for index, (relative, content) in enumerate(home_files.items()):
            destination = f"{home}/{relative}"
            variable = f"ENGINERY_TEST_SEED_{index}"
            seeds.append(
                f'mkdir -p "$(dirname "{destination}")"'
                f' && printf %s "${variable}" > "{destination}"'
            )
            seed_arguments += ["-e", f"{variable}={content}"]
        script = "; ".join([*seeds, 'exec "$@"'])
        arguments = [
            "run",
            "--rm",
            *hardening_arguments(RunnerProfile(), network="bridge"),
            "--add-host",
            f"{CONTAINER_STUB_HOST}:host-gateway",
            "--label",
            "dev.enginery.test-proof=1",
            "--name",
            f"enginery-test-proof-{uuid.uuid4().hex[:10]}",
            "-v",
            f"{proof.workspace}:{WORKSPACE_MOUNT}:rw",
            # The bare "remote" keeps its host path so the workspace's origin URL resolves.
            "-v",
            f"{proof.remote}:{proof.remote}:rw",
            "-w",
            WORKSPACE_MOUNT,
            *(item for key, value in environment.items() for item in ("-e", f"{key}={value}")),
            *seed_arguments,
            "--entrypoint",
            "sh",
            self.image,
            "-c",
            script,
            "sh",
            *command,
        ]
        return OciEngine().run_cli(arguments, timeout=_CONTAINER_TIMEOUT_SECONDS)


def require_real_target(harness: str) -> RealHarness:
    """The binary to prove against: the host's if it is the catalog version, else the image's.

    Skips, with a reason naming both the host binary and the worker image, only when neither
    is usable. Never runs a harness version the catalog does not test.
    """
    executable, probe = _PROBES[harness]
    catalog_host = load_execution_catalog().host(harness)
    host = _host_facts(catalog_host, shutil.which(executable), probe)
    image_ref: str | None = None
    image: ImageFacts | None = None
    if not (host.found and host.matches_catalog) and harness in _IMAGE_CAPABLE:
        image, image_ref = _image_facts(harness)
    selection = select_target(executable, catalog_host.tested_executable_version, host, image)
    if selection.target == "skip":
        pytest.skip(selection.reason)
    found = shutil.which(executable)
    return RealHarness(
        harness=harness,
        executable=executable,
        version=catalog_host.tested_executable_version,
        binary=Path(found) if selection.target == "host" and found else None,
        image=image_ref if selection.target == "image" else None,
        reason=selection.reason,
    )


def require_real_harness(harness: str) -> Path:
    """The host binary path for harnesses proven on the host (skips otherwise, naming why)."""
    target = require_real_target(harness)
    assert target.binary is not None
    return target.binary


@dataclass(frozen=True, slots=True)
class ProofWorkspace:
    """A real git repository, a local bare 'remote' and a directory outside the workspace."""

    root: Path
    workspace: Path
    outside: Path
    remote: Path
    home: Path

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=self.workspace if cwd is None else cwd,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    def remote_refs(self) -> str:
        return self.git("for-each-ref", cwd=self.remote).strip()


def make_proof_workspace(root: Path) -> ProofWorkspace:
    proof = ProofWorkspace(
        root=root,
        workspace=root / "workspace",
        outside=root / "outside",
        remote=root / "remote.git",
        home=root / "home",
    )
    for directory in (proof.workspace, proof.outside, proof.home):
        directory.mkdir(parents=True)
    subprocess.run(("git", "init", "--bare", str(proof.remote)), check=True, capture_output=True)
    proof.git("init", "-b", "main")
    proof.git("config", "user.name", "Setup")
    proof.git("config", "user.email", "setup@enginery.invalid")
    (proof.workspace / "README.md").write_text("project\n", encoding="utf-8")
    (proof.outside / "secret.txt").write_text("outside the workspace\n", encoding="utf-8")
    proof.git("add", "README.md")
    proof.git("commit", "-m", "initial")
    proof.git("remote", "add", "origin", str(proof.remote))
    return proof


def scrubbed_environment(
    harness_binary: Path, proof: ProofWorkspace, worker_environment: Mapping[str, str]
) -> dict[str, str]:
    """The adapter's container environment, re-rooted onto the throw-away host directories.

    Paths that only exist inside the image (HOME, config dirs, PATH) are replaced by temporary
    directories; every other entry is exactly what the isolated worker would receive.
    """
    git_binary = shutil.which("git")
    assert git_binary is not None
    bun_binary = shutil.which("bun")
    search_path = {str(harness_binary.parent), str(Path(git_binary).parent), "/usr/bin", "/bin"}
    if bun_binary is not None:
        search_path.add(str(Path(bun_binary).parent))
    environment = dict(worker_environment)
    # Nothing listens on the discard port: any attempt to reach a non-loopback host (a harness's
    # own catalog or telemetry lookups) is refused locally instead of leaving this machine.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment[name] = "http://127.0.0.1:9"
    for name in ("NO_PROXY", "no_proxy"):
        environment[name] = "127.0.0.1,localhost"
    environment["PATH"] = ":".join(sorted(search_path))
    environment["HOME"] = str(proof.home)
    environment["TMPDIR"] = str(proof.root)
    if "CLAUDE_CONFIG_DIR" in environment:
        environment["CLAUDE_CONFIG_DIR"] = str(proof.home / ".claude")
        (proof.home / ".claude").mkdir()
    if "PI_CODING_AGENT_DIR" in environment:
        environment["PI_CODING_AGENT_DIR"] = str(proof.home / ".omp" / "agent")
        (proof.home / ".omp" / "agent").mkdir(parents=True)
    return environment
