"""Helpers for tests that run the real harness binaries against a loopback model server.

These tests are the only place a real ``claude`` or ``omp`` process is started. They never use
a real credential or reach a hosted endpoint: the harness is pointed at a loopback stub with a
throw-away HOME and configuration directory and a scrubbed environment.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from enginery.adapters.claude_code import probe_installed_host as probe_claude
from enginery.adapters.omp import probe_installed_host as probe_omp
from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.execution_catalog import InstalledHost

_PROBES = {"claude-code": ("claude", probe_claude), "omp": ("omp", probe_omp)}


def require_real_harness(harness: str) -> Path:
    """Return the path of the tested harness binary, or skip when it is absent or another version.

    The permission mappings are written against exactly the harness version the execution
    catalog records, so any other installed version is not evidence for them.
    """
    executable, probe = _PROBES[harness]
    found = shutil.which(executable)
    if found is None:
        pytest.skip(f"real {executable} binary is not installed")
    installed: InstalledHost | None = probe(found)
    if installed is None:
        pytest.skip(f"real {executable} binary did not identify itself")
    if not load_execution_catalog().host(harness).matches(installed):
        pytest.skip(
            f"installed {executable} {installed.executable_version} is not the tested version"
        )
    return Path(found)


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
