"""Helpers for the tests that talk to a real container engine.

These tests run only where a qualified engine is reachable; ``require_real_engine`` is the one
place that decides to skip, and it skips for that reason alone. Everything else that goes wrong
with an engine that *is* reachable fails the test.

The worker image used here is a TEST-ONLY STAND-IN, not the production worker image: the
pinned ``python`` base plus the real packaged ``worker_entry.py``. It exists so the real
hardened ``docker create`` command line (which runs ``python3 /opt/enginery/worker_entry.py``)
can be exercised on an engine without building the multi-gigabyte harness image.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tarfile
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import IO, Any

import pytest

from enginery.adapters.oci.engine import EngineCommandError, OciEngine
from enginery.adapters.oci.launch import container_name_for, store_names_for
from enginery.domain.errors import MissingPrerequisiteError
from enginery.domain.runner import LaunchSpec

# python:3.12-slim (Debian trixie), multi-arch index digest; present locally on the
# development engines and pulled by digest elsewhere.
STANDIN_BASE_IMAGE = (
    "python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
)
STANDIN_TAG_PREFIX = "enginery-test-standin"
_PULL_TIMEOUT_SECONDS = 900.0
_BUILD_TIMEOUT_SECONDS = 600.0


def require_real_engine() -> OciEngine:
    """The default engine if it can enforce the isolated profile, else skip the test."""
    engine = OciEngine()
    try:
        engine.qualify()
    except MissingPrerequisiteError as error:
        pytest.skip(f"no qualified container engine is reachable: {error}")
    return engine


def _worker_entry_source() -> bytes:
    return (
        resources.files("enginery.adapters.oci").joinpath("assets", "worker_entry.py").read_bytes()
    )


def build_standin_image(engine: OciEngine, workdir: Path) -> tuple[str, str]:
    """Build the stand-in image and return ``(tag, image id)``."""
    entry = _worker_entry_source()
    dockerfile = (
        f"FROM {STANDIN_BASE_IMAGE}\n"
        "COPY worker_entry.py /opt/enginery/worker_entry.py\n"
        "ENV HOME=/home/worker\n"
        'LABEL dev.enginery.test-standin="1"\n'
    )
    digest = hashlib.sha256(entry + dockerfile.encode()).hexdigest()[:12]
    tag = f"{STANDIN_TAG_PREFIX}:{digest}"
    context = workdir / "standin-context"
    context.mkdir(parents=True, exist_ok=True)
    (context / "worker_entry.py").write_bytes(entry)
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")

    if engine.run_cli(("image", "inspect", STANDIN_BASE_IMAGE)).returncode != 0:
        pulled = engine.run_cli(
            ("pull", "--quiet", STANDIN_BASE_IMAGE), timeout=_PULL_TIMEOUT_SECONDS
        )
        if pulled.returncode != 0:
            raise EngineCommandError(
                "cannot obtain the stand-in base image", details={"stderr": pulled.stderr[-1000:]}
            )
    built = engine.run_cli(
        ("build", "--quiet", "--tag", tag, str(context)), timeout=_BUILD_TIMEOUT_SECONDS
    )
    if built.returncode != 0:
        raise EngineCommandError(
            "cannot build the stand-in image", details={"stderr": built.stderr[-2000:]}
        )
    image_id = str(engine.image_inspect(tag)["Id"])
    return tag, image_id


# --- an attempt's capped store -------------------------------------------------------


def create_attempt(
    engine: OciEngine,
    spec: LaunchSpec,
    inner: Sequence[str],
    *,
    nonce: str = "n" * 32,
    extra: Sequence[str] = (),
) -> None:
    """Create and start the store container, seed the workspace, create (not start) the worker.

    The same steps the launcher takes, with the real engine; ``extra`` is spliced into the
    worker's ``docker create`` flags (to create a deliberately weaker container).
    """
    from enginery.adapters.oci.launch import create_arguments, store_arguments
    from enginery.adapters.oci.store import write_tree_archive

    names = store_names_for(spec.container_name)
    created = engine.run_cli(store_arguments(spec))
    assert created.returncode == 0, created.stderr
    engine.start(names.holder)
    limits = spec.profile.resources
    engine.copy_in(
        names.holder,
        "/workspace",
        lambda stream: write_tree_archive(
            spec.workspace_path,
            stream,
            uid=spec.profile.uid,
            gid=spec.profile.gid,
            max_bytes=limits.workspace_mib * 1024 * 1024,
            max_entries=limits.workspace_inodes - 1,
        ),
    )
    arguments = create_arguments(spec, inner, nonce=nonce)
    worker = engine.run_cli((arguments[0], *extra, *arguments[1:]))
    assert worker.returncode == 0, worker.stderr


def remove_attempt(engine: OciEngine, operation_id: str) -> None:
    """Remove an attempt's worker, store container and volumes, whatever state it is in."""
    worker = container_name_for(operation_id)
    names = store_names_for(worker)
    for container in (worker, names.holder):
        with contextlib.suppress(EngineCommandError):
            engine.remove(container)
    for volume in (names.workspace_volume, names.out_volume):
        with contextlib.suppress(EngineCommandError):
            engine.volume_remove(volume)


def read_volume(engine: OciEngine, spec: LaunchSpec, path: str) -> dict[str, bytes]:
    """The regular files under ``path`` of a running store container, by relative name."""
    holder = store_names_for(spec.container_name).holder
    found: dict[str, bytes] = {}

    def read(stream: IO[bytes]) -> None:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                extracted = archive.extractfile(member) if member.isreg() else None
                if extracted is not None:
                    found[member.name.removeprefix("./")] = extracted.read()

    engine.copy_out(holder, path, read)
    return found


def start_and_wait(engine: OciEngine, spec: LaunchSpec) -> tuple[int, dict[str, Any]]:
    """Start the worker, wait for it and parse the result file it left in the result volume."""
    engine.start(spec.container_name)
    code = engine.wait(spec.container_name)
    files = read_volume(engine, spec, f"/out/{spec.result_name}")
    return code, json.loads(files[spec.result_name])
