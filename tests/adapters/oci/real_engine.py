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

import hashlib
from importlib import resources
from pathlib import Path

import pytest

from enginery.adapters.oci.engine import EngineCommandError, OciEngine
from enginery.domain.errors import MissingPrerequisiteError

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
