"""Build and verify one fixture package's wheel and sdist artifacts.

Uses a fixed, product-owned subprocess invocation (``uv build``) with a
hardcoded argument vector -- never an agent-authored or dynamically
assembled shell command, matching the "fixed broker" requirement applied
to every Stage 2 side effect. The caller supplies ``fixture_root``; this
module never infers a build target from untrusted input.

Verification has two independent layers: the build step itself confirms
exactly one wheel and one sdist were produced and that their filenames
carry the expected version, and ``verify_clean_install`` proves the built
wheel actually installs into an isolated, disposable virtual environment
and its smoke-testable module attributes match expectation -- a green
build is not itself proof of installability.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from enginery.application.delivery_ports import ReleaseArtifact
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
)

CommandRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]

_WHEEL_MEDIA_TYPE = "application/vnd.pypa.wheel"
_SDIST_MEDIA_TYPE = "application/gzip"


def _default_runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


@dataclass(frozen=True, slots=True)
class BuiltFixtureArtifacts:
    """The two immutable, digest-bound artifacts one fixture build produces."""

    wheel: ReleaseArtifact
    wheel_path: Path
    sdist: ReleaseArtifact
    sdist_path: Path


@dataclass(frozen=True, slots=True)
class FixtureBuilder:
    """Builds and verifies a fixture package's wheel and sdist via ``uv build``."""

    command_runner: CommandRunner = field(default=_default_runner)

    def build(self, fixture_root: Path, *, expected_version: str) -> BuiltFixtureArtifacts:
        if not fixture_root.is_absolute():
            raise InvalidInputError("fixture_root must be an absolute path")
        if not expected_version.strip():
            raise InvalidInputError("expected_version must be non-blank")
        dist_dir = fixture_root / "dist"
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
        result = self.command_runner(
            ("uv", "build", "--wheel", "--sdist", "-o", "dist"), fixture_root
        )
        if result.returncode != 0:
            raise ExternalConflictError(
                "fixture build failed", details={"stderr": result.stderr[-2000:]}
            )
        wheels = sorted(dist_dir.glob("*.whl"))
        sdists = sorted(dist_dir.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise InternalInvariantViolationError(
                "fixture build must produce exactly one wheel and one sdist",
                details={
                    "wheels": [path.name for path in wheels],
                    "sdists": [path.name for path in sdists],
                },
            )
        wheel_path, sdist_path = wheels[0], sdists[0]
        normalized_version = expected_version.replace("-", "_")
        if normalized_version not in wheel_path.name or expected_version not in sdist_path.name:
            raise ExternalConflictError(
                "built artifact filenames do not carry the expected version",
                details={
                    "expected_version": expected_version,
                    "wheel": wheel_path.name,
                    "sdist": sdist_path.name,
                },
            )
        return BuiltFixtureArtifacts(
            wheel=ReleaseArtifact(
                version=expected_version,
                digest=Digest.of_bytes(wheel_path.read_bytes()),
                media_type=_WHEEL_MEDIA_TYPE,
            ),
            wheel_path=wheel_path,
            sdist=ReleaseArtifact(
                version=expected_version,
                digest=Digest.of_bytes(sdist_path.read_bytes()),
                media_type=_SDIST_MEDIA_TYPE,
            ),
            sdist_path=sdist_path,
        )

    def verify_clean_install(
        self,
        artifacts: BuiltFixtureArtifacts,
        *,
        import_module: str,
        expected_version: str,
    ) -> None:
        """Install the built wheel into a fresh, disposable venv and smoke-check it.

        Never reuses the development environment: an import that only
        succeeds because a sibling editable install or the repository's
        own ``sys.path`` happens to satisfy it would be a false pass.
        """
        with self._disposable_venv() as python_executable:
            install = self.command_runner(
                (
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python_executable),
                    str(artifacts.wheel_path),
                ),
                artifacts.wheel_path.parent,
            )
            if install.returncode != 0:
                raise ExternalConflictError(
                    "fixture wheel did not install cleanly",
                    details={"stderr": install.stderr[-2000:]},
                )
            probe = self.command_runner(
                (
                    str(python_executable),
                    "-c",
                    f"import {import_module} as m; print(m.__version__)",
                ),
                artifacts.wheel_path.parent,
            )
            if probe.returncode != 0:
                raise ExternalConflictError(
                    "fixture module failed to import after a clean install",
                    details={"stderr": probe.stderr[-2000:]},
                )
            observed_version = probe.stdout.strip()
            if observed_version != expected_version:
                raise ExternalConflictError(
                    "installed fixture module version does not match the expected release version",
                    details={"expected": expected_version, "observed": observed_version},
                )

    @contextmanager
    def _disposable_venv(self) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(prefix="enginery-stage2-fixture-venv-") as raw_dir:
            venv_dir = Path(raw_dir)
            created = self.command_runner(("uv", "venv", str(venv_dir)), venv_dir.parent)
            if created.returncode != 0:
                raise ExternalConflictError(
                    "failed to create a disposable verification venv",
                    details={"stderr": created.stderr[-2000:]},
                )
            yield venv_dir / "bin" / "python"


__all__ = ["BuiltFixtureArtifacts", "FixtureBuilder"]
