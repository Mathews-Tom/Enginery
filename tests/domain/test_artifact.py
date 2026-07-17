"""Tests for enginery.domain.artifact."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.artifact import Artifact, ArtifactKind, RedactionClassification
from enginery.domain.digests import Digest
from enginery.domain.ids import ArtifactId, NodeAttemptId, NodeId, RunId

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_artifact(**overrides: object) -> Artifact:
    defaults: dict[str, object] = {
        "id": ArtifactId("artifact-1"),
        "digest": Digest.of_bytes(b"payload"),
        "byte_size": 128,
        "media_type": "text/plain",
        "kind": ArtifactKind.PATCH,
        "run_id": RunId("run-1"),
        "node_id": NodeId("node-1"),
        "attempt_id": NodeAttemptId("attempt-1"),
        "storage_reference": "cas://sha256/abc",
        "redaction": RedactionClassification.INTERNAL,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return Artifact(**defaults)  # type: ignore[arg-type]


class TestArtifactKind:
    def test_has_the_ten_named_kinds(self) -> None:
        assert {member.value for member in ArtifactKind} == {
            "plan",
            "patch",
            "transcript",
            "log",
            "test_report",
            "review_report",
            "pr_metadata",
            "release_manifest",
            "evaluation_result",
            "human_decision",
        }


class TestRedactionClassification:
    def test_has_the_three_designed_classifications(self) -> None:
        assert {member.value for member in RedactionClassification} == {
            "public",
            "internal",
            "sensitive",
        }


class TestArtifact:
    def test_constructs_with_valid_fields(self) -> None:
        artifact = _make_artifact()

        assert artifact.schema_version == 1

    def test_is_immutable(self) -> None:
        artifact = _make_artifact()
        with pytest.raises(AttributeError):
            artifact.byte_size = 0  # type: ignore[misc]

    def test_rejects_negative_byte_size(self) -> None:
        with pytest.raises(Exception, match="byte_size"):
            _make_artifact(byte_size=-1)

    def test_rejects_blank_media_type(self) -> None:
        with pytest.raises(Exception, match="media_type"):
            _make_artifact(media_type="  ")

    def test_rejects_blank_storage_reference(self) -> None:
        with pytest.raises(Exception, match="storage_reference"):
            _make_artifact(storage_reference="  ")

    def test_rejects_naive_created_at(self) -> None:
        with pytest.raises(Exception, match="timezone-aware"):
            _make_artifact(created_at=datetime(2026, 1, 1))

    def test_rejects_schema_version_below_one(self) -> None:
        with pytest.raises(Exception, match="schema_version"):
            _make_artifact(schema_version=0)
