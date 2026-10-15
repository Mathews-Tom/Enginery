"""Build the worker image on a real container engine and run it under the hardened profile.

These tests need a reachable docker engine (and registry access for the pinned base). They skip
only through ``requires_docker_engine`` when no engine answers; the fake-engine suite in
``test_image_recipe.py`` always runs.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from enginery.adapters.oci import image
from enginery.adapters.oci.engine import OciEngine
from enginery.domain.runner import ImageIdentity


def docker_engine_available() -> bool:
    """True when a docker CLI is installed and its engine answers."""
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


requires_docker_engine = pytest.mark.skipif(
    not docker_engine_available(), reason="no reachable docker engine"
)

HARDENED_FLAGS = (
    "--read-only",
    "--cap-drop",
    "ALL",
    "--security-opt",
    "no-new-privileges",
    "--user",
    "65532:65532",
    "--network",
    "none",
    "--tmpfs",
    "/tmp:rw,nosuid,nodev,exec,size=256m",
    "--tmpfs",
    "/home/worker:rw,nosuid,nodev,exec,size=256m,uid=65532,gid=65532",
)
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Worker",
    "GIT_AUTHOR_EMAIL": "worker@enginery.invalid",
    "GIT_COMMITTER_NAME": "Worker",
    "GIT_COMMITTER_EMAIL": "worker@enginery.invalid",
}

pytestmark = requires_docker_engine


@pytest.fixture(scope="module")
def engine() -> OciEngine:
    return OciEngine()


@pytest.fixture(scope="module")
def built(engine: OciEngine) -> ImageIdentity:
    return image.build_image(engine)


def hardened_run(
    engine: OciEngine,
    identity: ImageIdentity,
    command: Sequence[str],
    *,
    extra: Sequence[str] = (),
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    return engine.run_cli(
        ["run", "--rm", *HARDENED_FLAGS, *extra, identity.image_id, *command], timeout=timeout
    )


def test_the_built_image_verifies_against_the_current_recipe(
    engine: OciEngine, built: ImageIdentity
) -> None:
    assert image.verify_image(engine, built.image_id) == built
    assert built.recipe_digest == image.recipe_digest()
    assert dict(built.harness_versions) == {
        "bun": "1.3.14",
        "claude-code": "2.1.296",
        "omp": "18.10.3",
    }


def test_the_image_runs_as_the_worker_user_with_no_entrypoint(
    engine: OciEngine, built: ImageIdentity
) -> None:
    completed = engine.run_cli(
        ["image", "inspect", "--format", "{{json .Config}}", built.image_id], timeout=60
    )
    config = json.loads(completed.stdout)
    assert config["User"] == "65532:65532"
    assert not config.get("Entrypoint")
    assert config["WorkingDir"] == "/workspace"
    assert config["Labels"][image.RUNNER_IMAGE_LABEL] == "1"
    assert not any("TOKEN" in entry or "KEY" in entry for entry in config["Env"])


@pytest.mark.parametrize(
    ("command", "first_line_prefix"),
    [
        (["omp", "--help"], "omp v18.10.3"),
        (["claude", "--version"], "2.1.296 "),
        (["git", "--version"], "git version 2."),
        (["python3", "--version"], "Python 3."),
        (["uv", "--version"], "uv 0.13.0 "),
        (["bun", "--version"], "1.3.14"),
    ],
)
def test_every_tool_runs_under_the_hardened_flags(
    engine: OciEngine, built: ImageIdentity, command: list[str], first_line_prefix: str
) -> None:
    completed = hardened_run(engine, built, command)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines()[0].startswith(first_line_prefix)


def test_only_the_scratch_filesystems_are_writable_and_no_capability_remains(
    engine: OciEngine, built: ImageIdentity
) -> None:
    script = (
        "touch /tmp/ok /home/worker/ok && echo scratch-writable;"
        " touch /usr/denied 2>/dev/null || echo rootfs-readonly;"
        " grep CapEff /proc/self/status; id -u; id -g; echo $HOME"
    )

    completed = hardened_run(engine, built, ["sh", "-c", script])

    assert completed.stdout.splitlines() == [
        "scratch-writable",
        "rootfs-readonly",
        "CapEff:\t0000000000000000",
        "65532",
        "65532",
        "/home/worker",
    ]


def _prepare_repository(workspace: Path) -> str:
    environment = {**os.environ, **GIT_IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.name", "Worker"],
        ["config", "user.email", "worker@enginery.invalid"],
    ):
        subprocess.run(["git", "-C", str(workspace), *args], check=True, env=environment)
    (workspace / "README.md").write_text("base\n", encoding="utf-8")
    for args in (["add", "README.md"], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(workspace), *args], check=True, env=environment)
    return subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()


def _open_permissions(path: Path) -> None:
    subprocess.run(["chmod", "-R", "a+rwX", str(path)], check=True)


def test_worker_entry_commits_in_the_workspace_bundles_it_and_leaves_host_deletable_files(
    engine: OciEngine, built: ImageIdentity, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    out = tmp_path / "out"
    workspace.mkdir()
    out.mkdir()
    base = _prepare_repository(workspace)
    intake = tmp_path / "intake"
    subprocess.run(["git", "clone", "-q", str(workspace), str(intake)], check=True)
    _open_permissions(workspace)
    _open_permissions(out)
    seed = base64.b64encode(b"seeded\n").decode()
    command = (
        "git checkout -q -b work/change && echo changed > change.txt && git add change.txt"
        " && git commit -q -m 'worker change' && cat /home/worker/.seed"
        " && stat -c %a /home/worker/.seed"
    )

    completed = engine.run_cli(
        [
            "run",
            "--rm",
            *HARDENED_FLAGS,
            *[item for key, value in GIT_IDENTITY.items() for item in ("-e", f"{key}={value}")],
            "-v",
            f"{workspace}:/workspace:rw",
            "-v",
            f"{out}:/out:rw",
            "-w",
            "/workspace",
            built.image_id,
            "python3",
            "/opt/enginery/worker_entry.py",
            "--operation-id",
            "op-docker",
            "--result",
            "/out/result.json",
            "--deadline-seconds",
            "120",
            "--bundle-branch",
            "work/change",
            "--bundle-base",
            base,
            "--bundle-path",
            "/out/branch.bundle",
            "--seed-file",
            f"/home/worker/.seed={seed}",
            "--",
            "sh",
            "-ceu",
            command,
        ],
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result == {
        "operation_id": "op-docker",
        "output": "seeded\n600\n",
        "schema_version": 1,
        "terminal_status": "succeeded",
    }
    subprocess.run(
        ["git", "-C", str(intake), "bundle", "verify", str(out / "branch.bundle")], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(intake),
            "fetch",
            "-q",
            str(out / "branch.bundle"),
            "refs/heads/work/change:refs/heads/work/change",
        ],
        check=True,
    )
    listing = subprocess.run(
        ["git", "-C", str(intake), "log", "--format=%s", f"{base}..work/change"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert listing.stdout.strip() == "worker change"
    # Whatever the container created must not trap the host operator.
    shutil.rmtree(workspace)
    shutil.rmtree(out)


def test_worker_entry_enforces_the_deadline_inside_the_container(
    engine: OciEngine, built: ImageIdentity, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    _open_permissions(out)
    started = time.monotonic()

    completed = engine.run_cli(
        [
            "run",
            "--rm",
            *HARDENED_FLAGS,
            "-v",
            f"{out}:/out:rw",
            built.image_id,
            "python3",
            "/opt/enginery/worker_entry.py",
            "--operation-id",
            "op-deadline",
            "--result",
            "/out/result.json",
            "--deadline-seconds",
            "2",
            "--kill-grace-seconds",
            "1",
            "--",
            "sh",
            "-c",
            "sleep 300 & sleep 300",
        ],
        timeout=120,
    )

    assert time.monotonic() - started < 100
    assert completed.returncode == 143, completed.stderr
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert result["terminal_status"] == "failed"
    assert result["output"] == (
        "[enginery worker: deadline of 2 seconds exceeded; process group terminated]\n"
    )
    shutil.rmtree(out)
