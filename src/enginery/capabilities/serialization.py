"""JSON (de)serialization for a resolved capability lock.

Used by the ``enginery capability lock --check`` CLI command and by any
caller that wants to persist a resolved :class:`CapabilityLock` across a
run rather than re-resolving it. This never stores capability content
bytes -- only the digest, provenance evidence, and license a resolution
already computed.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError


def lock_to_json(lock: CapabilityLock) -> dict[str, object]:
    return {
        "entries": [
            {
                "name": entry.name,
                "version": entry.version,
                "digest": str(entry.digest),
                "license": entry.license,
                "introduced_by_run": entry.introduced_by_run,
                "provenance": {
                    "status": entry.provenance.status.value,
                    "source_label": entry.provenance.source_label,
                    "signer_key_id": entry.provenance.signer_key_id,
                    "verified_at": entry.provenance.verified_at.isoformat(),
                },
            }
            for entry in lock.entries
        ]
    }


def lock_from_json(data: object) -> CapabilityLock:
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise InvalidInputError("capability lock JSON must contain an 'entries' list")
    entries = tuple(_entry_from_json(item) for item in data["entries"])
    return CapabilityLock(entries=entries)


def _entry_from_json(item: object) -> LockedCapability:
    if not isinstance(item, dict):
        raise InvalidInputError("capability lock entry must be a JSON object")
    provenance_data = item.get("provenance")
    if not isinstance(provenance_data, dict):
        raise InvalidInputError("capability lock entry is missing its provenance object")
    license_value = item.get("license")
    signer_key_id = provenance_data.get("signer_key_id")
    return LockedCapability(
        name=_str(item, "name"),
        version=_str(item, "version"),
        digest=_digest(item, "digest"),
        license=license_value if isinstance(license_value, str) else None,
        introduced_by_run=bool(item.get("introduced_by_run")),
        provenance=ProvenanceRecord(
            status=ProvenanceStatus(_str(provenance_data, "status")),
            source_label=_str(provenance_data, "source_label"),
            signer_key_id=signer_key_id if isinstance(signer_key_id, str) else None,
            verified_at=datetime.fromisoformat(_str(provenance_data, "verified_at")),
        ),
    )


def _str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"capability lock entry field {key!r} must be a non-blank string")
    return value


def _digest(data: dict[str, object], key: str) -> Digest:
    raw = _str(data, key)
    algorithm, separator, hex_value = raw.partition(":")
    if not separator:
        raise InvalidInputError(
            f"capability lock entry field {key!r} is not an 'algorithm:hex' digest"
        )
    return Digest(algorithm=algorithm, hex_value=hex_value)


def write_lock(lock: CapabilityLock, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(lock_to_json(lock), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_lock(path: Path) -> CapabilityLock:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InvalidInputError(f"unable to read capability lock at {path}") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise InvalidInputError(f"capability lock at {path} is not valid JSON") from error
    return lock_from_json(data)


__all__ = ["lock_from_json", "lock_to_json", "read_lock", "write_lock"]
