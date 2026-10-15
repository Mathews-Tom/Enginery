from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from enginery.capabilities.errors import CapabilityLegacyLockError, CapabilityPathError
from enginery.capabilities.lock import CapabilityLock, ProvenanceStatus
from enginery.capabilities.manifest import CapabilityPermission, PackageCompatibility
from enginery.capabilities.serialization import (
    LOCK_SCHEMA_VERSION,
    is_legacy_lock,
    legacy_lock_from_json,
    lock_from_json,
    lock_to_json,
    read_lock,
    write_lock,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from tests.support.capabilities import (
    NOW,
    lock_of,
    locked_entry,
    package_files,
    pin_for,
)


def _entries(payload: dict[str, object]) -> list[dict[str, Any]]:
    entries = payload["entries"]
    assert isinstance(entries, list)
    return entries


def _lock() -> CapabilityLock:
    dependency_files = package_files("dep-a", extra={"scripts/run.py": b"print(1)\n"})
    trusted = locked_entry(
        files=package_files(
            "skill-a",
            dependencies=[pin_for("dep-a", dependency_files)],
            permissions=[CapabilityPermission.REPOSITORY_READ],
            compatibility=PackageCompatibility(adapter_api_min=1, platforms=("darwin", "linux")),
        ),
        introduced_by_run=False,
    )
    dependency = locked_entry(files=dependency_files, introduced_by_run=True)
    authenticated = locked_entry(
        "skill-b",
        introduced_by_run=True,
        status=ProvenanceStatus.AUTHENTICATED,
        signer_key_id="publisher-1",
        license_id=None,
    )
    return lock_of(trusted, dependency, authenticated)


def _legacy_document() -> dict[str, object]:
    return {
        "entries": [
            {
                "name": "skill-a",
                "version": "1",
                "digest": str(Digest.of_bytes(b"blob")),
                "license": "MIT",
                "introduced_by_run": False,
                "provenance": {
                    "status": "local_trusted",
                    "source_label": "local",
                    "signer_key_id": None,
                    "verified_at": NOW.isoformat(),
                },
            },
            {
                "name": "skill-b",
                "version": "2",
                "digest": str(Digest.of_bytes(b"other blob")),
                "license": None,
                "introduced_by_run": True,
                "provenance": {
                    "status": "unauthenticated",
                    "source_label": "armory",
                    "signer_key_id": None,
                    "verified_at": NOW.isoformat(),
                },
            },
        ]
    }


def test_lock_round_trips_through_json_with_closure_details() -> None:
    lock = _lock()

    restored = lock_from_json(lock_to_json(lock))

    assert restored == lock
    assert restored.digest() == lock.digest()
    skill_a = restored.get("skill-a")
    assert skill_a is not None
    assert [pin.name for pin in skill_a.dependencies] == ["dep-a"]
    assert skill_a.permissions == (CapabilityPermission.REPOSITORY_READ,)
    assert skill_a.compatibility.platforms == ("darwin", "linux")
    skill_b = restored.get("skill-b")
    assert skill_b is not None
    assert skill_b.provenance.signer_key_id == "publisher-1"


def test_serialized_lock_declares_the_current_schema_version() -> None:
    assert lock_to_json(_lock())["schema_version"] == LOCK_SCHEMA_VERSION == 2


def test_write_and_read_lock_round_trip(tmp_path: Path) -> None:
    lock = _lock()
    path = tmp_path / "nested" / "capabilities.lock.json"

    write_lock(lock, path)
    restored = read_lock(path)

    assert restored.digest() == lock.digest()


def test_lock_from_json_rejects_missing_entries_list() -> None:
    with pytest.raises(InvalidInputError, match="entries"):
        lock_from_json({})


def test_lock_from_json_rejects_malformed_digest() -> None:
    payload = lock_to_json(_lock())
    _entries(payload)[0]["digest"] = "not-a-digest"

    with pytest.raises(InvalidInputError):
        lock_from_json(payload)


def test_lock_from_json_rejects_a_digest_that_no_longer_matches_the_recorded_files() -> None:
    payload = lock_to_json(lock_of(locked_entry("skill-a")))
    _entries(payload)[0]["files"][0]["digest"] = str(Digest.of_bytes(b"edited"))

    with pytest.raises(InvalidInputError, match="closure digest"):
        lock_from_json(payload)


def test_lock_from_json_rejects_an_unsatisfied_dependency_pin() -> None:
    payload = lock_to_json(_lock())
    payload["entries"] = [entry for entry in _entries(payload) if entry["name"] != "dep-a"]

    with pytest.raises(InvalidInputError, match="dependency pin"):
        lock_from_json(payload)


def test_lock_from_json_rejects_an_unknown_schema_version() -> None:
    payload = lock_to_json(_lock())
    payload["schema_version"] = 99

    with pytest.raises(InvalidInputError, match="schema_version"):
        lock_from_json(payload)


def test_lock_from_json_rejects_an_unknown_package_kind() -> None:
    payload = lock_to_json(lock_of(locked_entry("skill-a")))
    _entries(payload)[0]["kind"] = "plugin"

    with pytest.raises(InvalidInputError):
        lock_from_json(payload)


def test_lock_from_json_rejects_a_traversing_recorded_path() -> None:
    payload = lock_to_json(lock_of(locked_entry("skill-a")))
    _entries(payload)[0]["files"][0]["path"] = "../escape.md"

    with pytest.raises(CapabilityPathError, match="unsafe"):
        lock_from_json(payload)


def test_read_lock_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(InvalidInputError, match="not valid JSON"):
        read_lock(path)


def test_read_lock_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="unable to read"):
        read_lock(tmp_path / "missing.json")


def test_legacy_lock_is_recognized_and_refused_by_the_current_decoder() -> None:
    document = _legacy_document()

    assert is_legacy_lock(document) is True
    with pytest.raises(CapabilityLegacyLockError, match="re-resolved"):
        lock_from_json(document)


def test_current_lock_is_not_classified_as_legacy() -> None:
    assert is_legacy_lock(lock_to_json(_lock())) is False


def test_legacy_lockfile_is_refused_by_read_lock(tmp_path: Path) -> None:
    path = tmp_path / "legacy.lock.json"
    path.write_text(json.dumps(_legacy_document()), encoding="utf-8")

    with pytest.raises(CapabilityLegacyLockError):
        read_lock(path)


def test_legacy_lock_decodes_read_only_as_single_blob_entries() -> None:
    legacy = legacy_lock_from_json(_legacy_document())

    assert [entry.name for entry in legacy.entries] == ["skill-a", "skill-b"]
    assert legacy.entries[0].digest == Digest.of_bytes(b"blob")
    assert legacy.entries[1].provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert not isinstance(legacy, CapabilityLock)
    assert not hasattr(legacy.entries[0], "files")
    with pytest.raises(AttributeError):
        legacy.entries = ()  # type: ignore[misc]


def test_legacy_lock_digest_matches_what_the_previous_schema_computed() -> None:
    legacy = legacy_lock_from_json(_legacy_document())
    expected = Digest.of_json(
        [
            {
                "name": entry.name,
                "version": entry.version,
                "digest": str(entry.digest),
                "provenance_status": entry.provenance.status.value,
                "introduced_by_run": entry.introduced_by_run,
            }
            for entry in legacy.entries
        ]
    )

    assert legacy.digest() == expected


def test_legacy_decoder_refuses_a_current_schema_document() -> None:
    with pytest.raises(InvalidInputError, match="not legacy"):
        legacy_lock_from_json(lock_to_json(_lock()))


def test_legacy_decoder_rejects_duplicate_names() -> None:
    document = _legacy_document()
    entries = document["entries"]
    assert isinstance(entries, list)
    entries.append(dict(entries[0]))

    with pytest.raises(InvalidInputError, match="unique"):
        legacy_lock_from_json(document)
