"""Tests for enginery.domain.immutable."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from enginery.domain.immutable import freeze_mapping


@dataclass(frozen=True, slots=True)
class _Holder:
    payload: dict[str, int]

    def __post_init__(self) -> None:
        freeze_mapping(self, "payload", self.payload)


class TestFreezeMapping:
    def test_stores_a_read_only_snapshot(self) -> None:
        holder = _Holder(payload={"a": 1})

        with pytest.raises(TypeError):
            holder.payload["a"] = 2

    def test_snapshot_is_independent_of_the_caller_dict(self) -> None:
        source = {"a": 1}
        holder = _Holder(payload=source)
        source["a"] = 999

        assert holder.payload["a"] == 1

    def test_snapshot_still_compares_equal_to_a_plain_dict(self) -> None:
        holder = _Holder(payload={"a": 1})

        assert holder.payload == {"a": 1}
