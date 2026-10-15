"""Versioned catalog of the native execution controls each agent harness supports.

The catalog is data, not behavior: it names which flags a host accepts, which
level vocabulary its effort control uses, the exact form a model pin must take,
and the executable versions and protocol fingerprint those facts were tested against.
A host entry names either one exact executable version or an accepted version range
together with the version actually tested; the two are never conflated. It
never lists model identifiers; a public listing is not proof of authenticated
access, so the only model check is the exact-identifier form.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError
from enginery.domain.version_range import Version, VersionRange


class SupportTier(enum.Enum):
    """How far a host's native controls have been proven to work."""

    QUALIFIED = "qualified"
    EXPERIMENTAL = "experimental"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ModelControl:
    """The native model-selection control of one host."""

    flag: str
    pin_form: str
    pin_pattern: str
    effective_evidence: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.flag, self.pin_form, self.pin_pattern)):
            raise InvalidInputError("model control flag, pin form, and pattern must be non-blank")
        try:
            re.compile(self.pin_pattern)
        except re.error as error:
            raise InvalidInputError(
                "model control pin pattern is not a valid expression"
            ) from error

    def accepts(self, value: str) -> bool:
        return re.fullmatch(self.pin_pattern, value) is not None


@dataclass(frozen=True, slots=True)
class EffortControl:
    """The native effort control of one host, in the host's own vocabulary."""

    flag: str
    levels: tuple[str, ...]
    unpinnable: tuple[str, ...]
    effective_evidence: str

    def __post_init__(self) -> None:
        if not self.flag.strip():
            raise InvalidInputError("effort control flag must be non-blank")
        if not self.levels or any(not level.strip() for level in self.levels):
            raise InvalidInputError("effort control levels must be non-blank and non-empty")
        if set(self.levels) & set(self.unpinnable):
            raise InvalidInputError("effort control levels and unpinnable values must be disjoint")


@dataclass(frozen=True, slots=True)
class InstalledHost:
    """The executable version and protocol fingerprint an adapter probe observed."""

    executable_version: str
    protocol_fingerprint: str

    def __post_init__(self) -> None:
        if not self.executable_version.strip() or not self.protocol_fingerprint.strip():
            raise InvalidInputError("installed host identity must be non-blank")


@dataclass(frozen=True, slots=True)
class ExactHostVersion:
    """A host accepted only at the one executable version it was tested against."""

    version: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise InvalidInputError("host executable version must be non-blank")

    @property
    def expectation(self) -> str:
        return f"the tested {self.version}"

    def accepts(self, observed: str) -> bool:
        return observed == self.version

    def details(self) -> dict[str, str]:
        return {"tested_executable_version": self.version}

    def bound_details(self) -> dict[str, str]:
        return {"bound_executable_version": self.version}


@dataclass(frozen=True, slots=True)
class RangedHostVersion:
    """A host accepted across a version range, with the one version its flags were tested on.

    Acceptance is a statement about versions the controls are expected to keep working
    on; only ``tested`` was actually exercised. An observed version inside the range is
    accepted, never reported as tested.
    """

    accepted: VersionRange
    tested: Version

    def __post_init__(self) -> None:
        if not self.accepted.contains(self.tested):
            raise InvalidInputError(
                "the tested executable version must be inside the accepted range",
                details={"accepted": str(self.accepted), "tested": str(self.tested)},
            )

    @property
    def expectation(self) -> str:
        return f"the accepted range {self.accepted} (tested {self.tested})"

    def accepts(self, observed: str) -> bool:
        return self.accepted.accepts(observed)

    def details(self) -> dict[str, str]:
        return {
            "accepted_executable_versions": str(self.accepted),
            "tested_executable_version": str(self.tested),
        }

    def bound_details(self) -> dict[str, str]:
        return {
            "bound_executable_versions": str(self.accepted),
            "bound_tested_executable_version": str(self.tested),
        }


HostVersion = ExactHostVersion | RangedHostVersion
"""A host's version policy: frozen exact history, or an accepted range with a tested version."""


@dataclass(frozen=True, slots=True)
class HostCapability:
    """One host's accepted and tested versions, protocol fingerprint, tier, and native controls."""

    harness: str
    version: HostVersion
    protocol_fingerprint: str
    support_tier: SupportTier
    model: ModelControl
    effort: EffortControl

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.harness, self.protocol_fingerprint)):
            raise InvalidInputError("host capability identity must be non-blank")

    def matches(self, installed: InstalledHost) -> bool:
        """Return whether an observed host has the protocol and an accepted executable version."""
        return installed.protocol_fingerprint == self.protocol_fingerprint and self.version.accepts(
            installed.executable_version
        )

    @property
    def tested_executable_version(self) -> str:
        """The one executable version this host's controls were actually tested against."""
        version = self.version
        return str(version.tested) if isinstance(version, RangedHostVersion) else version.version

    def matches_tested(self, installed: InstalledHost) -> bool:
        """Return whether an observed host has the protocol and exactly the tested version.

        For a pinned runner image, where a version merely inside the accepted range is not
        what was qualified. The tested version is always inside the accepted range.
        """
        return self.matches(installed) and (
            installed.executable_version == self.tested_executable_version
        )

    def require_installed(self, installed: InstalledHost | None) -> None:
        """Reject native controls against a host the catalog does not accept.

        An unobservable host, a different protocol, or an executable version the catalog
        does not accept (outside the range, or not a plain numeric version) makes every
        mandatory control unsupported: the flags and level vocabularies were only tested
        against the recorded fingerprint, so nothing is guessed.
        """
        if installed is None:
            raise MissingPrerequisiteError(
                f"the installed {self.harness} could not be probed, so its native controls "
                "are unsupported",
                details={"harness": self.harness},
            )
        if not self.matches(installed):
            raise MissingPrerequisiteError(
                f"the installed {self.harness} ({installed.executable_version}, "
                f"{installed.protocol_fingerprint}) differs from {self.version.expectation} "
                f"({self.protocol_fingerprint}), so its native controls are unsupported",
                details={
                    "harness": self.harness,
                    "installed_executable_version": installed.executable_version,
                    "installed_protocol_fingerprint": installed.protocol_fingerprint,
                    **self.version.details(),
                    "tested_protocol_fingerprint": self.protocol_fingerprint,
                },
            )

    def require_model_pin(self, value: str) -> None:
        """Reject any model value that is not the host's exact-identifier form."""
        if not self.model.accepts(value):
            raise InvalidInputError(
                f"model {value!r} is not an exact {self.harness} model pin; "
                f"a pin must be a {self.model.pin_form}",
                details={"harness": self.harness, "model": value},
            )

    def require_effort_level(self, value: str) -> None:
        """Reject any effort value outside the host's own pinnable level vocabulary."""
        if value in self.effort.unpinnable:
            raise InvalidInputError(
                f"effort {value!r} delegates the choice to {self.harness} and cannot be pinned; "
                f"pin one of: {', '.join(self.effort.levels)}",
                details={"harness": self.harness, "effort": value},
            )
        if value not in self.effort.levels:
            raise InvalidInputError(
                f"effort {value!r} is not a supported {self.harness} effort level; "
                f"supported levels: {', '.join(self.effort.levels)}",
                details={"harness": self.harness, "effort": value},
            )

    def require_usable(self) -> None:
        """Reject any selection against a host the catalog marks unsupported."""
        if self.support_tier is SupportTier.UNSUPPORTED:
            raise MissingPrerequisiteError(
                f"{self.harness} is marked unsupported in the execution catalog",
                details={"harness": self.harness},
            )


@dataclass(frozen=True, slots=True)
class ExecutionCatalog:
    """A verified catalog revision, bound by the digest of its exact bytes."""

    version: str
    digest: Digest
    hosts: tuple[HostCapability, ...]

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise InvalidInputError("execution catalog version must be non-blank")
        names = [host.harness for host in self.hosts]
        if len(set(names)) != len(names):
            raise InvalidInputError("execution catalog hosts must be unique")

    @property
    def harnesses(self) -> tuple[str, ...]:
        return tuple(host.harness for host in self.hosts)

    def host(self, harness: str) -> HostCapability:
        """Return one host's capabilities or reject an unknown harness."""
        for host in self.hosts:
            if host.harness == harness:
                return host
        raise InvalidInputError(
            f"harness {harness!r} is not in the execution catalog; "
            f"known harnesses: {', '.join(self.harnesses)}",
            details={"harness": harness},
        )


def catalog_from_mapping(data: Mapping[str, object], *, digest: Digest) -> ExecutionCatalog:
    """Decode one parsed catalog document into typed, validated capabilities."""
    version = data.get("catalog_version")
    hosts = data.get("hosts")
    if not isinstance(version, str) or not isinstance(hosts, list):
        raise InvalidInputError("execution catalog requires a version and a host list")
    return ExecutionCatalog(
        version=version,
        digest=digest,
        hosts=tuple(_host_from_mapping(entry) for entry in hosts),
    )


def _host_from_mapping(entry: object) -> HostCapability:
    if not isinstance(entry, dict):
        raise InvalidInputError("execution catalog host must be a table")
    try:
        tier = SupportTier(_text(entry, "support_tier"))
    except ValueError as error:
        raise InvalidInputError("execution catalog host support tier is unknown") from error
    model = _table(entry, "model")
    effort = _table(entry, "effort")
    return HostCapability(
        harness=_text(entry, "harness"),
        version=_host_version_from_mapping(entry),
        protocol_fingerprint=_text(entry, "protocol_fingerprint"),
        support_tier=tier,
        model=ModelControl(
            flag=_text(model, "flag"),
            pin_form=_text(model, "pin_form"),
            pin_pattern=_text(model, "pin_pattern"),
            effective_evidence=_text(model, "effective_evidence", allow_blank=True),
        ),
        effort=EffortControl(
            flag=_text(effort, "flag"),
            levels=_texts(effort, "levels"),
            unpinnable=_texts(effort, "unpinnable"),
            effective_evidence=_text(effort, "effective_evidence", allow_blank=True),
        ),
    )


def _host_version_from_mapping(entry: Mapping[str, object]) -> HostVersion:
    """Decode a host's version policy: one exact version, or a range plus its tested version.

    The two forms are mutually exclusive and never coerced into each other, so a frozen
    exact entry keeps its exact semantics and a range entry cannot lose its tested version.
    """
    exact = "executable_version" in entry
    ranged = "executable_versions" in entry or "tested_executable_version" in entry
    if exact == ranged:
        raise InvalidInputError(
            "an execution catalog host needs either executable_version or both "
            "executable_versions and tested_executable_version, not both or neither"
        )
    if exact:
        return ExactHostVersion(_text(entry, "executable_version"))
    return RangedHostVersion(
        accepted=VersionRange.parse(_text(entry, "executable_versions")),
        tested=Version.parse(_text(entry, "tested_executable_version")),
    )


def _table(entry: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = entry.get(key)
    if not isinstance(value, dict):
        raise InvalidInputError(f"execution catalog host requires a {key!r} table")
    return value


def _text(entry: Mapping[str, object], key: str, *, allow_blank: bool = False) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        raise InvalidInputError(f"execution catalog field {key!r} must be a string")
    return value


def _texts(entry: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = entry.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InvalidInputError(f"execution catalog field {key!r} must be a list of strings")
    return tuple(str(item) for item in value)


__all__ = [
    "EffortControl",
    "ExactHostVersion",
    "ExecutionCatalog",
    "HostCapability",
    "HostVersion",
    "InstalledHost",
    "ModelControl",
    "RangedHostVersion",
    "SupportTier",
    "catalog_from_mapping",
]
