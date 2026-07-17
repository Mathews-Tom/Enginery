"""Tests for enginery.domain.digests."""

from __future__ import annotations

import pytest

from enginery.domain.digests import Digest


class TestDigest:
    def test_of_bytes_produces_a_stable_sha256_hex_digest(self) -> None:
        digest = Digest.of_bytes(b"hello world")

        assert digest.algorithm == "sha256"
        assert digest.hex_value == (
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        )
        assert str(digest) == f"sha256:{digest.hex_value}"

    def test_of_bytes_is_deterministic(self) -> None:
        assert Digest.of_bytes(b"same payload") == Digest.of_bytes(b"same payload")

    def test_of_bytes_differs_for_different_payloads(self) -> None:
        assert Digest.of_bytes(b"a") != Digest.of_bytes(b"b")

    def test_of_json_is_stable_across_key_insertion_order(self) -> None:
        first = Digest.of_json({"a": 1, "b": 2})
        second = Digest.of_json({"b": 2, "a": 1})

        assert first == second

    def test_of_json_distinguishes_nested_structures(self) -> None:
        assert Digest.of_json({"a": [1, 2]}) != Digest.of_json({"a": [2, 1]})

    def test_rejects_unsupported_algorithm(self) -> None:
        with pytest.raises(Exception, match="algorithm"):
            Digest(algorithm="md5", hex_value="0" * 64)

    def test_rejects_malformed_hex_value(self) -> None:
        with pytest.raises(Exception, match="hexadecimal"):
            Digest(algorithm="sha256", hex_value="not-hex")

    def test_rejects_wrong_length_hex_value(self) -> None:
        with pytest.raises(Exception, match="hexadecimal"):
            Digest(algorithm="sha256", hex_value="ab")

    def test_is_immutable(self) -> None:
        digest = Digest.of_bytes(b"x")
        with pytest.raises(AttributeError):
            digest.hex_value = "0" * 64  # type: ignore[misc]

    def test_is_hashable_and_usable_as_a_set_member(self) -> None:
        assert len({Digest.of_bytes(b"x"), Digest.of_bytes(b"x"), Digest.of_bytes(b"y")}) == 2
