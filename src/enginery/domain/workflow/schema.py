"""Typed input/output schema declarations for workflow nodes and manifests.

A schema is data, never code: each field names a closed ``FieldType`` and a
required flag. This is deliberately too small to express computation, which
is exactly the point — it proves a manifest cannot embed a general-purpose
program through its schema declarations.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field

from enginery.domain.errors import InvalidInputError

_FIELD_SCHEMA_KEYS = frozenset({"type", "required"})


class FieldType(enum.Enum):
    """The closed set of primitive shapes a schema field may declare."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass(frozen=True, slots=True)
class FieldSchema:
    """One named, typed field of an input or output schema."""

    name: str
    field_type: FieldType
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise InvalidInputError("field schema name must be a non-blank string")

    @classmethod
    def from_mapping(cls, name: str, raw: Mapping[str, object]) -> FieldSchema:
        unknown_keys = set(raw) - _FIELD_SCHEMA_KEYS
        if unknown_keys:
            raise InvalidInputError(
                "field schema declares unknown keys; manifests cannot embed executable payloads",
                details={"field": name, "unknown_keys": sorted(unknown_keys)},
            )
        if "type" not in raw:
            raise InvalidInputError(
                "field schema is missing required key 'type'", details={"field": name}
            )
        raw_type = raw["type"]
        if not isinstance(raw_type, str):
            raise InvalidInputError("field schema 'type' must be a string", details={"field": name})
        try:
            field_type = FieldType(raw_type)
        except ValueError as error:
            raise InvalidInputError(
                f"field schema declares unknown type {raw_type!r}",
                details={"field": name, "type": raw_type},
            ) from error
        required = raw.get("required", True)
        if not isinstance(required, bool):
            raise InvalidInputError(
                "field schema 'required' must be a boolean", details={"field": name}
            )
        return cls(name=name, field_type=field_type, required=required)


@dataclass(frozen=True, slots=True)
class IOSchema:
    """A named, ordered set of schema fields with no duplicate names."""

    fields: tuple[FieldSchema, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        names = [item.name for item in self.fields]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise InvalidInputError(
                "schema declares duplicate field names", details={"fields": sorted(duplicates)}
            )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Mapping[str, object]]) -> IOSchema:
        return cls(
            fields=tuple(
                FieldSchema.from_mapping(name, field_raw) for name, field_raw in raw.items()
            )
        )


__all__ = ["FieldSchema", "FieldType", "IOSchema"]
