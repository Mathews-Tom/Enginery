"""``enginery doctor``: report only locally implemented prerequisites.

This milestone implements nothing beyond the package itself, so the report
is intentionally narrow: it does not claim to check git, GitHub, SQLite, or
any adapter that does not exist yet.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import metadata

_MIN_PYTHON = (3, 12)
_DISTRIBUTION = "enginery"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)


def _check_python_version(*, actual: tuple[int, int, int] | None = None) -> DoctorCheck:
    version = actual if actual is not None else sys.version_info[:3]
    ok = version[:2] >= _MIN_PYTHON
    required = ".".join(str(part) for part in _MIN_PYTHON)
    running = ".".join(str(part) for part in version)
    return DoctorCheck(
        name="python_version",
        ok=ok,
        detail=f"running Python {running}; requires >= {required}",
    )


def _check_package_metadata(*, distribution: str = _DISTRIBUTION) -> DoctorCheck:
    try:
        version = metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return DoctorCheck(
            name="package_metadata",
            ok=False,
            detail=f"distribution {distribution!r} is not installed",
        )
    return DoctorCheck(
        name="package_metadata", ok=True, detail=f"{distribution} {version} installed"
    )


def run_doctor() -> DoctorReport:
    return DoctorReport(checks=(_check_python_version(), _check_package_metadata()))
