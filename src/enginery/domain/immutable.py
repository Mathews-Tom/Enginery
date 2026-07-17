"""Defensive immutability helpers for frozen dataclasses (03_SYSTEM_DESIGN.md
§8.1 selects "immutable value objects" for every domain aggregate).

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


__all__ = ["freeze_mapping"]
