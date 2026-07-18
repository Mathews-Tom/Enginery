"""Provider-neutral adapter contract values.

These values are shared by application ports and adapter implementations. They
contain only normalized Enginery data, never provider SDK objects.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import FailureClass, InvalidInputError, MissingPrerequisiteError
from enginery.domain.ids import OperationId
from enginery.domain.immutable import freeze_mapping

ADAPTER_API_VERSION = 1


class ProviderKind(enum.StrEnum):
    """The supported application-port families."""

    WORK_LEDGER = "work_ledger"
    HARNESS = "harness"
    WORKSPACE = "workspace"
    SOURCE_CONTROL = "source_control"
    VALIDATION = "validation"
    RELEASE = "release"
    DEPLOYMENT = "deployment"
    CAPABILITY_SOURCE = "capability_source"


class AdapterAvailability(enum.StrEnum):
    """Whether a configured adapter can serve its declared contract."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"


class AdapterEventKind(enum.StrEnum):
    """Provider-neutral lifecycle categories for normalized adapter events."""

    STARTED = "started"
    PROGRESS = "progress"
    TERMINAL = "terminal"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True, slots=True)
class AdapterCapability:
    """One versioned optional behavior exposed by an adapter."""

    name: str
    version: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidInputError("adapter capability name must be non-blank")
        if self.version < 1:
            raise InvalidInputError("adapter capability version must be at least one")


@dataclass(frozen=True, slots=True)
class AdapterFingerprint:
    """Immutable behavior identity bound to a run before execution."""

    provider_id: str
    provider_version: str
    api_version: int
    capabilities: tuple[AdapterCapability, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise InvalidInputError("adapter provider_id must be non-blank")
        if not self.provider_version.strip():
            raise InvalidInputError("adapter provider_version must be non-blank")
        if self.api_version != ADAPTER_API_VERSION:
            raise InvalidInputError(
                "adapter API version is incompatible",
                details={"expected": ADAPTER_API_VERSION, "actual": self.api_version},
            )
        names = [capability.name for capability in self.capabilities]
        if len(names) != len(set(names)):
            raise InvalidInputError("adapter capability names must be unique")
        ordered = tuple(sorted(self.capabilities, key=lambda capability: capability.name))
        object.__setattr__(self, "capabilities", ordered)

    @property
    def digest(self) -> Digest:
        """Return a canonical digest for durable run binding."""
        return Digest.of_json(
            {
                "api_version": self.api_version,
                "capabilities": [
                    {"name": capability.name, "version": capability.version}
                    for capability in self.capabilities
                ],
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
            }
        )


@dataclass(frozen=True, slots=True)
class AdapterStatus:
    """A provider's availability and fingerprint at probe time."""

    kind: ProviderKind
    availability: AdapterAvailability
    fingerprint: AdapterFingerprint | None
    detail: str

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise InvalidInputError("adapter status detail must be non-blank")
        if self.availability is AdapterAvailability.AVAILABLE and self.fingerprint is None:
            raise InvalidInputError("available adapters require a fingerprint")
        if self.availability is not AdapterAvailability.AVAILABLE and self.fingerprint is not None:
            raise InvalidInputError("unavailable adapters cannot report a fingerprint")


@dataclass(frozen=True, slots=True)
class NormalizedAdapterFailure:
    """A provider failure classified without provider-native exception data."""

    failure_class: FailureClass
    summary: str
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise InvalidInputError("adapter failure summary must be non-blank")
        if self.retry_after_seconds is not None and (
            not math.isfinite(self.retry_after_seconds) or self.retry_after_seconds < 0
        ):
            raise InvalidInputError("adapter failure retry delay must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class NormalizedAdapterEvent:
    """A redacted provider event ready for artifact-backed persistence."""

    kind: AdapterEventKind
    occurred_at: datetime
    operation_id: OperationId | None
    summary: str
    attributes: Mapping[str, str] = field(default_factory=dict)
    output_digest: Digest | None = None

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None:
            raise InvalidInputError("adapter event occurred_at must be timezone-aware")
        if not self.summary.strip():
            raise InvalidInputError("adapter event summary must be non-blank")
        if any(not key.strip() or not value.strip() for key, value in self.attributes.items()):
            raise InvalidInputError("adapter event attributes must contain non-blank strings")
        freeze_mapping(self, "attributes", self.attributes)


def require_matching_fingerprints(
    bound: Mapping[str, Digest], discovered: Mapping[str, AdapterStatus]
) -> None:
    """Fail before provider use when a bound adapter is unavailable or changed."""
    for provider_id, expected_digest in bound.items():
        status = discovered.get(provider_id)
        if status is None:
            raise MissingPrerequisiteError(
                "bound adapter is not configured", details={"provider_id": provider_id}
            )
        if status.availability is not AdapterAvailability.AVAILABLE or status.fingerprint is None:
            raise MissingPrerequisiteError(
                "bound adapter is unavailable",
                details={"provider_id": provider_id, "availability": status.availability.value},
            )
        if status.fingerprint.digest != expected_digest:
            raise MissingPrerequisiteError(
                "bound adapter fingerprint changed",
                details={"provider_id": provider_id},
            )


__all__ = [
    "ADAPTER_API_VERSION",
    "AdapterAvailability",
    "AdapterCapability",
    "AdapterEventKind",
    "AdapterFingerprint",
    "AdapterStatus",
    "NormalizedAdapterEvent",
    "NormalizedAdapterFailure",
    "ProviderKind",
    "require_matching_fingerprints",
]
