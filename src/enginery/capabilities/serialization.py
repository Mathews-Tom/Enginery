"""JSON (de)serialization for a resolved capability lock.

Used by the ``enginery capability lock --check`` CLI command and by any
caller that wants to persist a resolved :class:`CapabilityLock` across a
run rather than re-resolving it. This never stores package file bytes --
only each package's closure records (path, kind, digest, size, executable
bit), dependency pins, origin, provenance evidence, and license a
resolution already computed.

The current schema is version 2 and is identified by an explicit
``schema_version``. A document without one is the previous single-blob
schema: :func:`lock_from_json` refuses it with
:class:`~enginery.capabilities.errors.CapabilityLegacyLockError`, and
:func:`legacy_lock_from_json` decodes it read-only for inspection. A legacy
lock is never upgraded in place; it must be re-resolved.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from enginery.capabilities.closure import AssetRecord
from enginery.capabilities.errors import CapabilityLegacyLockError
from enginery.capabilities.legacy_lock import LegacyCapabilityLock, LegacyLockedCapability
from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.capabilities.manifest import (
    CapabilityPermission,
    DependencyPin,
    PackageCompatibility,
    PackageKind,
    digest_from_text,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

LOCK_SCHEMA_VERSION = 2


def lock_to_json(lock: CapabilityLock) -> dict[str, object]:
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "entries": [
            {
                "name": entry.name,
                "version": entry.version,
                "kind": entry.kind.value,
                "revision": entry.revision,
                "digest": str(entry.digest),
                "license": entry.license,
                "introduced_by_run": entry.introduced_by_run,
                "provenance": {
                    "status": entry.provenance.status.value,
                    "source_label": entry.provenance.source_label,
                    "signer_key_id": entry.provenance.signer_key_id,
                    "verified_at": entry.provenance.verified_at.isoformat(),
                },
                "files": [record.to_json() for record in entry.files],
                "dependencies": [pin.to_json() for pin in entry.dependencies],
                "permissions": [permission.value for permission in entry.permissions],
                "compatibility": entry.compatibility.to_json(),
            }
            for entry in lock.entries
        ],
    }


def lock_from_json(data: object) -> CapabilityLock:
    """Decode a current-schema lock; a legacy lock raises, it is never upgraded."""

    entries = _entries(data)
    if not isinstance(data, dict) or "schema_version" not in data:
        raise CapabilityLegacyLockError(
            "capability lock uses the legacy single-blob schema and must be re-resolved",
            details={"entries": len(entries)},
        )
    if type(data["schema_version"]) is not int or data["schema_version"] != LOCK_SCHEMA_VERSION:
        raise InvalidInputError(
            "unsupported capability lock schema_version",
            details={"schema_version": str(data["schema_version"])},
        )
    return CapabilityLock(entries=tuple(_entry_from_json(item) for item in entries))


def legacy_lock_from_json(data: object) -> LegacyCapabilityLock:
    """Decode a previous-schema lock read-only; it carries no closure and cannot materialize."""

    entries = _entries(data)
    if isinstance(data, dict) and "schema_version" in data:
        raise InvalidInputError("capability lock declares a schema_version; it is not legacy")
    decoded = tuple(_legacy_entry_from_json(item) for item in entries)
    names = [entry.name for entry in decoded]
    if len(names) != len(set(names)):
        raise InvalidInputError("capability lock entries must have unique names")
    return LegacyCapabilityLock(entries=tuple(sorted(decoded, key=lambda entry: entry.name)))


def is_legacy_lock(data: object) -> bool:
    """Whether ``data`` is a previous-schema lock document (no ``schema_version``)."""

    return isinstance(data, dict) and "schema_version" not in data and "entries" in data


def _entries(data: object) -> list[object]:
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise InvalidInputError("capability lock JSON must contain an 'entries' list")
    return list(data["entries"])


def _provenance(item: dict[str, object]) -> ProvenanceRecord:
    provenance_data = item.get("provenance")
    if not isinstance(provenance_data, dict):
        raise InvalidInputError("capability lock entry is missing its provenance object")
    signer_key_id = provenance_data.get("signer_key_id")
    return ProvenanceRecord(
        status=ProvenanceStatus(_str(provenance_data, "status")),
        source_label=_str(provenance_data, "source_label"),
        signer_key_id=signer_key_id if isinstance(signer_key_id, str) else None,
        verified_at=datetime.fromisoformat(_str(provenance_data, "verified_at")),
    )


def _legacy_entry_from_json(item: object) -> LegacyLockedCapability:
    if not isinstance(item, dict):
        raise InvalidInputError("capability lock entry must be a JSON object")
    license_value = item.get("license")
    return LegacyLockedCapability(
        name=_str(item, "name"),
        version=_str(item, "version"),
        digest=_digest(item, "digest"),
        provenance=_provenance(item),
        license=license_value if isinstance(license_value, str) else None,
        introduced_by_run=bool(item.get("introduced_by_run")),
    )


def _entry_from_json(item: object) -> LockedCapability:
    if not isinstance(item, dict):
        raise InvalidInputError("capability lock entry must be a JSON object")
    license_value = item.get("license")
    introduced = item.get("introduced_by_run")
    if not isinstance(introduced, bool):
        raise InvalidInputError("capability lock entry introduced_by_run must be a boolean")
    try:
        return LockedCapability(
            name=_str(item, "name"),
            version=_str(item, "version"),
            kind=PackageKind(_str(item, "kind")),
            revision=_str(item, "revision"),
            digest=_digest(item, "digest"),
            provenance=_provenance(item),
            license=license_value if isinstance(license_value, str) else None,
            introduced_by_run=introduced,
            files=tuple(AssetRecord.from_json(record) for record in _list(item, "files")),
            dependencies=tuple(_pin(pin) for pin in _list(item, "dependencies")),
            permissions=tuple(
                CapabilityPermission(_item_str(value)) for value in _list(item, "permissions")
            ),
            compatibility=PackageCompatibility.from_json(item.get("compatibility")),
        )
    except ValueError as error:
        raise InvalidInputError(f"capability lock entry is invalid: {error}") from error


def _pin(item: object) -> DependencyPin:
    if not isinstance(item, dict):
        raise InvalidInputError("capability lock dependency must be a JSON object")
    return DependencyPin(
        name=_str(item, "name"),
        version=_str(item, "version"),
        closure_digest=_digest(item, "closure_digest"),
    )


def _list(data: dict[str, object], key: str) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise InvalidInputError(f"capability lock entry field {key!r} must be a list")
    return value


def _item_str(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidInputError("capability lock list items must be strings")
    return value


def _str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"capability lock entry field {key!r} must be a non-blank string")
    return value


def _digest(data: dict[str, object], key: str) -> Digest:
    return digest_from_text(_str(data, key))


def write_lock(lock: CapabilityLock, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(lock_to_json(lock), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_lock_document(path: Path) -> object:
    """Read and JSON-decode a lockfile without interpreting its schema."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InvalidInputError(f"unable to read capability lock at {path}") from error
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise InvalidInputError(f"capability lock at {path} is not valid JSON") from error


def read_lock(path: Path) -> CapabilityLock:
    """Read a current-schema lockfile; a legacy lockfile raises ``CapabilityLegacyLockError``."""

    return lock_from_json(read_lock_document(path))


__all__ = [
    "LOCK_SCHEMA_VERSION",
    "is_legacy_lock",
    "legacy_lock_from_json",
    "lock_from_json",
    "lock_to_json",
    "read_lock",
    "read_lock_document",
    "write_lock",
]
