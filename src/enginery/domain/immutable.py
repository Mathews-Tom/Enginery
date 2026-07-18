"""Defensive immutability helpers for frozen dataclasses.

Domain aggregates model every field as an immutable value object.

A frozen dataclass field typed as ``Mapping[K, V]`` still accepts a plain
mutable ``dict`` at construction; without normalization, the caller's later
mutation of that same dict object would silently change the "immutable"
aggregate's observed state. Every aggregate with a ``Mapping``-typed field
calls ``freeze_mapping`` from its own ``__post_init__`` to store a read-only
``MappingProxyType`` snapshot instead of the caller's original object.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


def freeze_mapping[K, V](instance: object, field_name: str, value: Mapping[K, V]) -> None:
    """Replace ``instance.field_name`` with a read-only snapshot of ``value``.

    Bypasses the frozen dataclass ``__setattr__`` guard the same way any
    ``__post_init__`` normalization must. Call exactly once per field, from
    within ``__post_init__`` only — never after construction.
    """
    object.__setattr__(instance, field_name, MappingProxyType(dict(value)))


def thaw_json_value(value: object) -> object:
    """Return a mutable JSON-compatible value for canonical serialization."""

    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    return value


def freeze_json_mapping(
    instance: object,
    field_name: str,
    value: Mapping[str, object],
) -> None:
    """Store a deeply immutable JSON mapping snapshot on a frozen dataclass."""

    frozen = _freeze_json_value(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("JSON mapping snapshot must remain a mapping")
    object.__setattr__(instance, field_name, frozen)


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


__all__ = ["freeze_json_mapping", "freeze_mapping", "thaw_json_value"]
