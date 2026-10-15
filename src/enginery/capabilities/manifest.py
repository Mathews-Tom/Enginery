"""The declared metadata every capability package carries.

Each package ships exactly one manifest, ``capability.json``, at its root.
It is a strict, versioned JSON document (standard library only; unknown
keys and duplicate keys are rejected so a hidden field can never ride along
unreviewed) declaring the package's identity, kind, license, transitive
dependency pins, permissions, and host compatibility. The manifest is itself
an asset of the package closure, so every field it declares is covered by the
closure digest.

Schema (version 1)::

    {
      "schema_version": 1,
      "name": "adr-writer",
      "version": "1.1.1",
      "kind": "instruction",            # instruction | command | hook | adapter
      "license": "MIT",                 # optional SPDX identifier
      "dependencies": [                 # optional transitive pins
        {"name": "...", "version": "...", "closure_digest": "sha256:..."}
      ],
      "permissions": ["repository.read"],           # optional, closed vocabulary
      "compatibility": {                # optional
        "adapter_api_min": 1,
        "adapter_api_max": 1,
        "platforms": ["darwin", "linux"]
      }
    }
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field

from enginery.capabilities.errors import CapabilityManifestError
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

MANIFEST_FILE_NAME = "capability.json"
MANIFEST_SCHEMA_VERSION = 1

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_PLATFORM_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "version",
        "kind",
        "license",
        "dependencies",
        "permissions",
        "compatibility",
    }
)


class PackageKind(enum.StrEnum):
    """What a package is for; only command and hook packages are ever executed."""

    INSTRUCTION = "instruction"
    COMMAND = "command"
    HOOK = "hook"
    ADAPTER = "adapter"


class CapabilityPermission(enum.StrEnum):
    """The closed vocabulary of authority a package may declare it needs."""

    REPOSITORY_READ = "repository.read"
    REPOSITORY_WRITE = "repository.write"
    PROCESS_EXECUTE = "process.execute"
    NETWORK_ACCESS = "network.access"
    ENVIRONMENT_READ = "environment.read"


def validate_name(name: str) -> str:
    if not _NAME_PATTERN.fullmatch(name):
        raise InvalidInputError(
            "capability name must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}", details={"name": name}
        )
    return name


def validate_version(version: str) -> str:
    if not _VERSION_PATTERN.fullmatch(version):
        raise InvalidInputError(
            "capability version must match [A-Za-z0-9][A-Za-z0-9._+-]{0,63}",
            details={"version": version},
        )
    return version


@dataclass(frozen=True, slots=True)
class DependencyPin:
    """One declared transitive dependency, pinned to an exact closure digest."""

    name: str
    version: str
    closure_digest: Digest

    def __post_init__(self) -> None:
        validate_name(self.name)
        validate_version(self.version)

    def to_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "version": self.version,
            "closure_digest": str(self.closure_digest),
        }


@dataclass(frozen=True, slots=True)
class PackageCompatibility:
    """The adapter API range and platforms a package declares it supports.

    ``adapter_api_max`` of ``None`` means no declared upper bound; an empty
    ``platforms`` tuple means no declared platform restriction.
    """

    adapter_api_min: int = 1
    adapter_api_max: int | None = None
    platforms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.adapter_api_min < 1:
            raise InvalidInputError("compatibility adapter_api_min must be at least 1")
        if self.adapter_api_max is not None and self.adapter_api_max < self.adapter_api_min:
            raise InvalidInputError("compatibility adapter_api_max must not be below the minimum")
        if any(not _PLATFORM_PATTERN.fullmatch(platform) for platform in self.platforms):
            raise InvalidInputError("compatibility platforms must be lowercase identifiers")
        object.__setattr__(self, "platforms", tuple(sorted(set(self.platforms))))

    def to_json(self) -> dict[str, object]:
        return {
            "adapter_api_min": self.adapter_api_min,
            "adapter_api_max": self.adapter_api_max,
            "platforms": list(self.platforms),
        }

    @classmethod
    def from_json(cls, data: object) -> PackageCompatibility:
        if not isinstance(data, dict):
            raise InvalidInputError("compatibility must be a JSON object")
        unknown = set(data) - {"adapter_api_min", "adapter_api_max", "platforms"}
        if unknown:
            raise InvalidInputError(
                "compatibility contains unknown keys", details={"keys": sorted(unknown)}
            )
        minimum = data.get("adapter_api_min", 1)
        maximum = data.get("adapter_api_max")
        platforms = data.get("platforms", [])
        if not isinstance(minimum, int) or isinstance(minimum, bool):
            raise InvalidInputError("compatibility adapter_api_min must be an integer")
        if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool)):
            raise InvalidInputError("compatibility adapter_api_max must be an integer")
        if not isinstance(platforms, list) or not all(isinstance(item, str) for item in platforms):
            raise InvalidInputError("compatibility platforms must be a list of strings")
        return cls(adapter_api_min=minimum, adapter_api_max=maximum, platforms=tuple(platforms))


@dataclass(frozen=True, slots=True)
class PackageManifest:
    """The validated, canonical form of one package's ``capability.json``."""

    name: str
    version: str
    kind: PackageKind
    license: str | None = None
    dependencies: tuple[DependencyPin, ...] = ()
    permissions: tuple[CapabilityPermission, ...] = ()
    compatibility: PackageCompatibility = field(default_factory=PackageCompatibility)

    def __post_init__(self) -> None:
        validate_name(self.name)
        validate_version(self.version)
        if self.license is not None and not self.license.strip():
            raise InvalidInputError("package license, when present, must be non-blank")
        names = [pin.name for pin in self.dependencies]
        if len(names) != len(set(names)):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            raise InvalidInputError(
                f"package dependency names must be unique; duplicated: {', '.join(duplicates)}"
            )
        if self.name in names:
            raise InvalidInputError("a package cannot depend on itself")
        object.__setattr__(
            self, "dependencies", tuple(sorted(self.dependencies, key=lambda pin: pin.name))
        )
        object.__setattr__(self, "permissions", tuple(sorted(set(self.permissions))))

    def to_json(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "name": self.name,
            "version": self.version,
            "kind": self.kind.value,
            "dependencies": [pin.to_json() for pin in self.dependencies],
            "permissions": [permission.value for permission in self.permissions],
            "compatibility": self.compatibility.to_json(),
        }
        if self.license is not None:
            document["license"] = self.license
        return document


def render_manifest(manifest: PackageManifest) -> bytes:
    """The canonical on-disk bytes of ``manifest`` (sorted keys, trailing newline)."""

    return (json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key!r}")
        document[key] = value
    return document


def parse_manifest(raw: bytes) -> PackageManifest:
    """Decode and strictly validate a ``capability.json`` document."""

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as error:
        raise CapabilityManifestError(
            "package manifest is not valid UTF-8 JSON", details={"reason": str(error)}
        ) from error
    if not isinstance(document, dict):
        raise CapabilityManifestError("package manifest root must be a JSON object")
    try:
        return _manifest_from_document(document)
    except (InvalidInputError, ValueError) as error:
        raise CapabilityManifestError(
            "package manifest is invalid", details={"reason": str(error)}
        ) from error


def _manifest_from_document(document: dict[str, object]) -> PackageManifest:
    unknown = set(document) - _TOP_LEVEL_KEYS
    if unknown:
        raise InvalidInputError(
            "package manifest contains unknown keys", details={"keys": sorted(unknown)}
        )
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise InvalidInputError(
            f"package manifest schema_version must be {MANIFEST_SCHEMA_VERSION}"
        )
    license_value = document.get("license")
    if license_value is not None and not isinstance(license_value, str):
        raise InvalidInputError("package manifest license must be a string")
    return PackageManifest(
        name=_text(document, "name"),
        version=_text(document, "version"),
        kind=PackageKind(_text(document, "kind")),
        license=license_value,
        dependencies=tuple(_pin(item) for item in _list(document, "dependencies")),
        permissions=tuple(
            CapabilityPermission(_item_text(item)) for item in _list(document, "permissions")
        ),
        compatibility=(
            PackageCompatibility.from_json(document["compatibility"])
            if "compatibility" in document
            else PackageCompatibility()
        ),
    )


def _text(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"package manifest field {key!r} must be a non-blank string")
    return value


def _item_text(item: object) -> str:
    if not isinstance(item, str):
        raise InvalidInputError("package manifest list items must be strings")
    return item


def _list(document: dict[str, object], key: str) -> list[object]:
    value = document.get(key, [])
    if not isinstance(value, list):
        raise InvalidInputError(f"package manifest field {key!r} must be a list")
    return value


def digest_from_text(raw: str) -> Digest:
    algorithm, separator, hex_value = raw.partition(":")
    if not separator:
        raise InvalidInputError("digest must be formatted as 'algorithm:hex'")
    return Digest(algorithm=algorithm, hex_value=hex_value)


def _pin(item: object) -> DependencyPin:
    if not isinstance(item, dict):
        raise InvalidInputError("package dependency must be a JSON object")
    unknown = set(item) - {"name", "version", "closure_digest"}
    if unknown:
        raise InvalidInputError(
            "package dependency contains unknown keys", details={"keys": sorted(unknown)}
        )
    return DependencyPin(
        name=_text(item, "name"),
        version=_text(item, "version"),
        closure_digest=digest_from_text(_text(item, "closure_digest")),
    )


__all__ = [
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "CapabilityPermission",
    "DependencyPin",
    "PackageCompatibility",
    "PackageKind",
    "PackageManifest",
    "digest_from_text",
    "parse_manifest",
    "render_manifest",
    "validate_name",
    "validate_version",
]
