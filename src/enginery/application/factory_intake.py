"""Pure issue eligibility, durable intake documents and bound run identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from enginery.domain.errors import InvalidInputError


def derive_run_id(project_identity: str, external_reference: str, bound_digest: str) -> str:
    document = {
        "project": project_identity,
        "issue": external_reference,
        "snapshot": str(bound_digest),
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "fr-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class IntakeEntry:
    external_reference: str
    run_id: str
    bound_digest: str
    source_revision: str
    recorded_at: str

    def to_state(self) -> dict[str, object]:
        return {
            "external_reference": self.external_reference,
            "run_id": self.run_id,
            "bound_digest": self.bound_digest,
            "source_revision": self.source_revision,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_state(cls, state: object) -> IntakeEntry:
        document = _object(state)
        return cls(
            external_reference=_string(document, "external_reference"),
            run_id=_string(document, "run_id"),
            bound_digest=_string(document, "bound_digest"),
            source_revision=_string(document, "source_revision"),
            recorded_at=_string(document, "recorded_at"),
        )


@dataclass(slots=True)
class RepositoryIntake:
    last_seen_updated_at: str | None = None
    issues: dict[str, IntakeEntry] = field(default_factory=dict)

    def to_state(self) -> dict[str, object]:
        return {
            "last_seen_updated_at": self.last_seen_updated_at,
            "issues": {key: entry.to_state() for key, entry in self.issues.items()},
        }

    @classmethod
    def from_state(cls, state: object) -> RepositoryIntake:
        document = _object(state)
        cursor = document.get("last_seen_updated_at")
        if cursor is not None and not isinstance(cursor, str):
            raise InvalidInputError("Intake index cursor must be a string or null")
        issues: dict[str, IntakeEntry] = {}
        for key, value in _object(document.get("issues")).items():
            entry = IntakeEntry.from_state(value)
            if key != entry.external_reference:
                raise InvalidInputError("Intake index issue key must match its external reference")
            issues[key] = entry
        return cls(cursor, issues)


@dataclass(slots=True)
class IntakeIndex:
    repositories: dict[str, RepositoryIntake] = field(default_factory=dict)

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "repositories": {key: value.to_state() for key, value in self.repositories.items()},
        }

    @classmethod
    def from_state(cls, state: object) -> IntakeIndex:
        document = _object(state)
        if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
            raise InvalidInputError("Unsupported intake index schema version")
        return cls(
            {
                key: RepositoryIntake.from_state(value)
                for key, value in _object(document.get("repositories")).items()
            }
        )


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise InvalidInputError("Intake index records must be JSON objects")
    return cast(dict[str, object], value)


def _string(document: Mapping[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"Intake index field {name} must be a non-blank string")
    return value
