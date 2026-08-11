from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.domain.g4_deficiency import G4DeficiencyFinding


def _finding(*, cited_run_ids: tuple[str, ...] = ("run-1", "run-2")) -> G4DeficiencyFinding:
    return G4DeficiencyFinding(
        finding_id="finding-1",
        deficiency="Validation command fails after generated dependency update.",
        cited_run_ids=cited_run_ids,
        evidence_pull_request_number=42,
        producer_principal_id="operator-a",
        evidence_pull_request_author_login="author-a",
        recorded_at=datetime(2026, 8, 10, tzinfo=UTC),
    )


def test_finding_requires_two_distinct_cited_runs() -> None:
    with pytest.raises(InvalidInputError, match="two distinct"):
        _finding(cited_run_ids=("run-1", "run-1"))


def test_finding_serializes_durable_evidence_references() -> None:
    finding = _finding()

    assert finding.to_state()["cited_run_ids"] == ["run-1", "run-2"]
    assert finding.to_state()["evidence_pull_request_number"] == 42
