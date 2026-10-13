"""The isolated validation runner against a real engine: the checkout travels through volumes.

Skipped, by ``real_engine``, when no qualified container engine is reachable. The runner is the
real ``OciValidationRunner`` and the launcher is the real ``run_launcher`` (called in process so
the stand-in image can be given an identity); the worker image is the TEST-ONLY STAND-IN.

What a qualification run must repeat against the production image: the validation command sees
the checkout it was given, its output comes back, and the container the receipt describes holds
only the two capped volumes (no bind mount of any host path) and is attached to no network.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from enginery.adapters.oci.engine import ObjectNotFoundError, OciEngine
from enginery.adapters.oci.launch import container_name_for, store_names_for
from enginery.adapters.oci.launcher import run_launcher
from enginery.adapters.oci.validation import OciValidationRunner
from enginery.domain.runner import (
    NO_NETWORK,
    OUT_MOUNT,
    WORKSPACE_MOUNT,
    ImageIdentity,
    LaunchSpec,
    RunnerContext,
    RunnerProfile,
    RunnerReceipt,
    RunnerSelection,
)

from .real_engine import remove_attempt


def _standin(_engine: OciEngine, image_id: str) -> ImageIdentity:
    """TEST-ONLY: the stand-in image carries no recipe labels, so it is given an identity."""
    return ImageIdentity(image_id=image_id, recipe_digest="sha256:" + "d" * 64)


def test_real_validation_is_seeded_and_read_back_through_volumes_never_bind_mounts(
    real_engine: OciEngine,
    real_profile: RunnerProfile,
    standin_image: str,
    real_root: Path,
) -> None:
    receipts = real_root / "receipts"
    receipts.mkdir(mode=0o700)
    checkout = real_root / "checkout"
    checkout.mkdir()
    (checkout / "tests.txt").write_text("from the verified head", encoding="utf-8")
    (checkout / "tests.txt").chmod(0o600)
    context = RunnerContext(
        selection=RunnerSelection.isolated(image=standin_image, profile=real_profile),
        profile=real_profile,
        receipt_root=str(receipts),
    )
    specs: list[LaunchSpec] = []
    log = io.StringIO()

    def launcher(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        items = list(argv)
        spec = LaunchSpec.from_state(json.loads(items[items.index("--spec") + 1]))
        specs.append(spec)
        code = run_launcher(
            spec,
            tuple(items[items.index("--") + 1 :]),
            engine=real_engine,
            verify=_standin,
            stderr=log,
        )
        return subprocess.CompletedProcess(items, code, "", log.getvalue())

    runner = OciValidationRunner(
        context, artifact_root=real_root / "artifacts", time_budget_seconds=60, launcher=launcher
    )
    command = (
        "python3",
        "-c",
        "print(open('/workspace/tests.txt').read())\n"
        "open('/workspace/left-behind.txt', 'w').write('x')",
    )

    try:
        completed = runner(command, checkout)
    finally:
        for spec in specs:
            remove_attempt(real_engine, spec.operation_id)

    assert completed.returncode == 0, log.getvalue()
    assert "from the verified head" in completed.stdout
    (spec,) = specs
    assert spec.relay_container is None and spec.attempt_network == NO_NETWORK
    receipt = RunnerReceipt.from_state(
        json.loads(Path(spec.receipt_path).read_text(encoding="utf-8"))
    )
    # No host path is mounted: the only non-tmpfs mounts are the attempt's two volumes.
    assert sorted(mount for mount in receipt.effective.mounts if mount[0] != "tmpfs") == sorted(
        [("volume", OUT_MOUNT, True), ("volume", WORKSPACE_MOUNT, True)]
    )
    assert receipt.effective.networks == (NO_NETWORK,)
    # Nothing of the attempt is left on the engine.
    names = store_names_for(container_name_for(spec.operation_id))
    assert not real_engine.container_exists(spec.container_name)
    assert not real_engine.container_exists(names.holder)
    with pytest.raises(ObjectNotFoundError):
        real_engine.volume_inspect(names.workspace_volume)
