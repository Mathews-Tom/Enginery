"""Host and adapter-API compatibility receipts for locked packages.

A package declares the adapter API range and platforms it supports in its
manifest. At resolve time the resolver compares that declaration with the
host actually running Enginery and records the outcome as a receipt on the
lock entry, so the compatibility decision is bound into the lock digest
rather than re-derived (possibly differently) later. An incompatible package
can still be locked and inspected; installing it is refused.
"""

from __future__ import annotations

import enum
import sys
from dataclasses import dataclass

from enginery.application.adapter_types import ADAPTER_API_VERSION
from enginery.capabilities.manifest import PackageCompatibility
from enginery.domain.errors import InvalidInputError

_PLATFORM_ALIASES = {"darwin": "darwin", "linux": "linux", "win32": "windows", "cygwin": "windows"}


class CompatibilityStatus(enum.StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class HostProfile:
    """The adapter API version and platform of the host evaluating a package."""

    adapter_api_version: int
    platform: str

    def __post_init__(self) -> None:
        if self.adapter_api_version < 1:
            raise InvalidInputError("host adapter_api_version must be at least 1")
        if not self.platform.strip():
            raise InvalidInputError("host platform must be non-blank")


def current_host() -> HostProfile:
    """The running host: this build's adapter API version and normalized platform."""

    return HostProfile(
        adapter_api_version=ADAPTER_API_VERSION,
        platform=_PLATFORM_ALIASES.get(sys.platform, sys.platform),
    )


@dataclass(frozen=True, slots=True)
class CompatibilityReceipt:
    """The recorded result of evaluating a package's declared compatibility on a host."""

    host_adapter_api_version: int
    host_platform: str
    status: CompatibilityStatus
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.status is CompatibilityStatus.COMPATIBLE) != (not self.reasons):
            raise InvalidInputError(
                "a compatibility receipt has reasons exactly when it is incompatible"
            )

    @property
    def compatible(self) -> bool:
        return self.status is CompatibilityStatus.COMPATIBLE

    def to_json(self) -> dict[str, object]:
        return {
            "host_adapter_api_version": self.host_adapter_api_version,
            "host_platform": self.host_platform,
            "status": self.status.value,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_json(cls, data: object) -> CompatibilityReceipt:
        if not isinstance(data, dict):
            raise InvalidInputError("compatibility receipt must be a JSON object")
        version, platform = data.get("host_adapter_api_version"), data.get("host_platform")
        status, reasons = data.get("status"), data.get("reasons")
        if not isinstance(version, int) or isinstance(version, bool):
            raise InvalidInputError("compatibility receipt host_adapter_api_version is invalid")
        if not isinstance(platform, str) or not isinstance(status, str):
            raise InvalidInputError("compatibility receipt platform and status must be strings")
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            raise InvalidInputError("compatibility receipt reasons must be a list of strings")
        try:
            parsed_status = CompatibilityStatus(status)
        except ValueError as error:
            raise InvalidInputError("compatibility receipt status is unknown") from error
        return cls(
            host_adapter_api_version=version,
            host_platform=platform,
            status=parsed_status,
            reasons=tuple(reasons),
        )


def evaluate_compatibility(
    declared: PackageCompatibility, host: HostProfile
) -> CompatibilityReceipt:
    """Compare a package's declared compatibility with ``host`` and record the outcome."""

    reasons: list[str] = []
    if host.adapter_api_version < declared.adapter_api_min:
        reasons.append(
            f"requires adapter API >= {declared.adapter_api_min}, host provides "
            f"{host.adapter_api_version}"
        )
    if declared.adapter_api_max is not None and host.adapter_api_version > declared.adapter_api_max:
        reasons.append(
            f"supports adapter API <= {declared.adapter_api_max}, host provides "
            f"{host.adapter_api_version}"
        )
    if declared.platforms and host.platform not in declared.platforms:
        reasons.append(
            f"supports platforms {', '.join(declared.platforms)}, host is {host.platform}"
        )
    return CompatibilityReceipt(
        host_adapter_api_version=host.adapter_api_version,
        host_platform=host.platform,
        status=CompatibilityStatus.INCOMPATIBLE if reasons else CompatibilityStatus.COMPATIBLE,
        reasons=tuple(reasons),
    )


__all__ = [
    "CompatibilityReceipt",
    "CompatibilityStatus",
    "HostProfile",
    "current_host",
    "evaluate_compatibility",
]
