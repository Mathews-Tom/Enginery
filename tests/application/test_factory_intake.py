from __future__ import annotations

import hashlib
import json

import pytest

from enginery.application.factory_intake import (
    IntakeEntry,
    IntakeIndex,
    RepositoryIntake,
    declared_classification,
    derive_run_id,
    label_names,
)
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError, TransientProviderFailureError


def test_canonical_classification_ignores_unrelated_labels() -> None:
    work_kind, risk, provenance = declared_classification(
        ("bug", "enginery/risk/medium", "enginery/work-kind/plan"),
        source_url="https://api.github.com/repos/o/r/issues/1",
    )
    assert (work_kind, risk) == (WorkKind.PLAN, RiskClass.MEDIUM)
    assert provenance.canonical_labels == ("enginery/work-kind/plan", "enginery/risk/medium")
    assert provenance.source_url == "https://api.github.com/repos/o/r/issues/1"


@pytest.mark.parametrize(
    ("labels", "reason"),
    [
        (("enginery/risk/low",), "exactly one declared work-kind"),
        (("enginery/work-kind/issue",), "exactly one declared risk"),
        (
            ("enginery/work-kind/issue", "enginery/work-kind/issue", "enginery/risk/low"),
            "exactly one declared work-kind",
        ),
        (
            ("enginery/work-kind/issue", "enginery/risk/low", "enginery/risk/medium"),
            "exactly one declared risk",
        ),
        (("ENGINERY/work-kind/issue", "enginery/risk/low"), "exact declared vocabulary"),
        (("enginery/work-kind/issue", "enginery/risk/high"), "exact declared vocabulary"),
    ],
)
def test_noncanonical_or_ambiguous_classification_is_ineligible(
    labels: tuple[str, ...], reason: str
) -> None:
    with pytest.raises(InvalidInputError, match=reason):
        declared_classification(labels)


def test_label_records_preserve_duplicates_and_payload_error_types() -> None:
    assert label_names([{"name": "b"}, {"name": "a"}, {"name": "a"}]) == ("a", "a", "b")
    with pytest.raises(TransientProviderFailureError, match="must be an object"):
        label_names(["bug"])
    with pytest.raises(InvalidInputError, match="'name' must be a non-blank string"):
        label_names([{"name": ""}])


def test_run_identity_matches_canonical_bound_document_and_changes_with_binding() -> None:
    expected = hashlib.sha256(
        b'{"issue":"o/r#1","project":"/project","snapshot":"sha256:abc"}'
    ).hexdigest()[:20]
    assert derive_run_id("/project", "o/r#1", "sha256:abc") == f"fr-{expected}"
    assert derive_run_id("/project", "o/r#1", "sha256:abc") == derive_run_id(
        "/project", "o/r#1", "sha256:abc"
    )
    for project, issue, digest in (
        ("/other", "o/r#1", "sha256:abc"),
        ("/project", "o/r#2", "sha256:abc"),
        ("/project", "o/r#1", "sha256:def"),
    ):
        assert derive_run_id(project, issue, digest) != f"fr-{expected}"


def test_index_roundtrips_repository_cursors_and_issue_bindings() -> None:
    entry = IntakeEntry("o/r#1", "fr-123", "sha256:abc", "rev-1", "2026-10-16T00:00:00Z")
    index = IntakeIndex({"o/r": RepositoryIntake("2026-10-16T00:00:00Z", {"o/r#1": entry})})
    state = json.loads(json.dumps(index.to_state()))
    assert state == {
        "schema_version": 1,
        "repositories": {
            "o/r": {
                "last_seen_updated_at": "2026-10-16T00:00:00Z",
                "issues": {
                    "o/r#1": {
                        "external_reference": "o/r#1",
                        "run_id": "fr-123",
                        "bound_digest": "sha256:abc",
                        "source_revision": "rev-1",
                        "recorded_at": "2026-10-16T00:00:00Z",
                    }
                },
            }
        },
    }
    assert IntakeIndex.from_state(state) == index
    assert IntakeIndex.from_state(IntakeIndex().to_state()) == IntakeIndex()


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_unknown_index_schema_is_refused(version: object) -> None:
    with pytest.raises(InvalidInputError, match="schema version"):
        IntakeIndex.from_state({"schema_version": version, "repositories": {}})


def test_index_rejects_issue_key_that_would_hide_another_binding() -> None:
    entry = IntakeEntry("o/r#1", "fr-123", "sha256:abc", "rev-1", "now")
    state = {
        "schema_version": 1,
        "repositories": {
            "o/r": {
                "last_seen_updated_at": None,
                "issues": {"o/r#2": entry.to_state()},
            }
        },
    }
    with pytest.raises(InvalidInputError, match="must match its external reference"):
        IntakeIndex.from_state(state)
