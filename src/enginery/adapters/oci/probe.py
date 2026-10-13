"""Run a command in a throw-away hardened container of the pinned worker image.

A harness adapter in the isolated profile must report the version of the harness inside the
image, not of whatever is installed on the host. ``OciProbeRunner`` is the ``command_runner``
that makes that so: same hardening as a worker, no network, no mounts, nothing persisted.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import subprocess
from dataclasses import dataclass

from enginery.adapters.oci.engine import EngineCommandError, OciEngine
from enginery.adapters.oci.launch import probe_arguments
from enginery.domain.errors import InvalidInputError, OperationTimeoutError
from enginery.domain.runner import RunnerProfile

DEFAULT_PROBE_TIMEOUT_SECONDS = 120.0
_ENGINE_FAILURE_EXIT_CODE = 125
"""``docker run`` exits 125 when the engine, not the contained command, failed."""
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME_PREFIX = "enginery-probe-"


@dataclass(frozen=True, slots=True)
class OciProbeRunner:
    """Callable ``(argv) -> CompletedProcess`` running ``argv`` inside the pinned image.

    The contained command's own exit status, stdout and stderr are returned as-is, so a
    harness that is missing or broken in the image reads as such. A container that could not
    be started at all raises: it never reads as a successful probe.
    """

    engine: OciEngine
    image: str
    profile: RunnerProfile
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not _IMAGE_ID.fullmatch(self.image):
            raise InvalidInputError("a probe image must be an exact image id (sha256:<64 hex>)")
        if self.timeout_seconds <= 0:
            raise InvalidInputError("probe timeout must be positive")

    def __call__(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        name = f"{_NAME_PREFIX}{secrets.token_hex(8)}"
        arguments = probe_arguments(self.profile, self.image, argv, name=name)
        try:
            completed = self.engine.run_cli(arguments, timeout=self.timeout_seconds)
        except OperationTimeoutError:
            # The client was stopped, not the container: remove it so nothing lingers.
            with contextlib.suppress(EngineCommandError):
                self.engine.remove(name)
            raise
        if completed.returncode == _ENGINE_FAILURE_EXIT_CODE:
            raise EngineCommandError(
                "the probe container could not be started",
                details={"command": argv[0], "stderr": completed.stderr.strip()[-2000:]},
            )
        return subprocess.CompletedProcess(
            args=argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
