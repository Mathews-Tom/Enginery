"""Smoke tests for the ``enginery gate status`` CLI command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.adapters.github import GitHubWorkLedger
from enginery.cli.main import main
from enginery.domain.digests import Digest
from enginery.domain.g4_authority_evidence import G4AuthorityEvidence
from enginery.domain.g4_deficiency import G4DeficiencyFinding
from enginery.ledger.g4_authority_evidence import G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE
from enginery.ledger.g4_deficiency import record_g4_deficiency
from enginery.ledger.service import LedgerService


def _write_floor_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_gate_status_reports_fail_closed_against_an_empty_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    floor_config = _write_floor_config(
        tmp_path / "floor.toml",
        """
        schema_version = 2
        [registered_principals]
        identities = []
        """,
    )

    exit_code = main(
        [
            "gate",
            "status",
            "--gate",
            "G4",
            "--database",
            str(database),
            "--floor-config",
            str(floor_config),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"] == "G4"
    assert payload["overall"] == "fail"
    conditions = {condition["id"]: condition for condition in payload["conditions"]}
    assert conditions["corpus_diversity"]["status"] == "fail"
    assert conditions["registered_human_principals"]["status"] == "fail"
    assert conditions["completed_run_diversity"]["status"] == "unmeasured"
    assert conditions["recurring_evidence_backed_deficiency"]["status"] == "unmeasured"
    # An empty ledger's fail-closed exit code matches doctor's "missing
    # prerequisite" convention -- the gate has not passed.
    assert exit_code != 0


def test_gate_status_rejects_an_unsupported_gate_name(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    floor_config = _write_floor_config(
        tmp_path / "floor.toml", "schema_version = 2\n[registered_principals]\nidentities = []\n"
    )

    with pytest.raises(SystemExit):
        main(
            [
                "gate",
                "status",
                "--gate",
                "G7",
                "--database",
                str(database),
                "--floor-config",
                str(floor_config),
            ]
        )


def test_gate_status_prints_human_readable_output_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "ledger.db"
    floor_config = _write_floor_config(
        tmp_path / "floor.toml", "schema_version = 2\n[registered_principals]\nidentities = []\n"
    )

    main(
        [
            "gate",
            "status",
            "--gate",
            "G4",
            "--database",
            str(database),
            "--floor-config",
            str(floor_config),
        ]
    )

    out = capsys.readouterr().out
    assert out.startswith("gate G4: fail\n")
    assert "[fail] corpus_diversity:" in out
    assert "[unmeasured] recurring_evidence_backed_deficiency:" in out


def test_gate_records_verified_g4_authority_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "ledger.db"
    floor_config = _write_floor_config(
        tmp_path / "floor.toml",
        """
        schema_version = 2
        [registered_principals]
        identities = [
          { id = "producer", github_login = "producer-login" },
          { id = "approver-one", github_login = "approver-one-login" },
          { id = "approver-two", github_login = "approver-two-login" },
        ]
        """,
    )
    finding = G4DeficiencyFinding(
        finding_id="finding-1",
        deficiency="Validation command fails after generated dependency update.",
        cited_run_ids=("run-1", "run-2"),
        evidence_pull_request_number=42,
        evidence_document_digest=Digest.of_bytes(b"evidence"),
        producer_principal_id="producer",
        evidence_pull_request_author_login="evidence-author",
        recorded_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    ledger = LedgerService.open(database)
    try:
        record_g4_deficiency(ledger, finding=finding, correlation_id="record-deficiency-finding")
    finally:
        ledger.close()

    evidence = G4AuthorityEvidence(
        finding_id=finding.finding_id,
        pull_request_number=finding.evidence_pull_request_number,
        merged_head_revision="a" * 40,
        document_digest=finding.evidence_document_digest,
        approver_principal_ids=("approver-one", "approver-two"),
        verified_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    def verify(
        self: GitHubWorkLedger,
        *,
        finding: G4DeficiencyFinding,
        principal_github_logins: dict[str, str],
        verified_at: datetime,
    ) -> G4AuthorityEvidence:
        assert self.config.repository == "Mathews-Tom/Enginery"
        assert finding.finding_id == "finding-1"
        assert principal_github_logins["approver-one"] == "approver-one-login"
        assert verified_at.tzinfo is UTC
        return evidence

    monkeypatch.setattr(GitHubWorkLedger, "verify_g4_evidence", verify)

    exit_code = main(
        [
            "gate",
            "record-g4-deficiency-evidence",
            "--database",
            str(database),
            "--finding-id",
            finding.finding_id,
            "--correlation-id",
            "record-authority-evidence",
            "--github-repository",
            "Mathews-Tom/Enginery",
            "--github-credential-reference",
            "operator-gh-cli",
            "--floor-config",
            str(floor_config),
            "--json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == evidence.to_state()
    ledger = LedgerService.open(database)
    try:
        projection = ledger.read_projection(
            aggregate_type=G4_AUTHORITY_EVIDENCE_AGGREGATE_TYPE,
            aggregate_id=finding.finding_id,
        )
    finally:
        ledger.close()
    assert projection is not None
    assert projection.state == evidence.to_state()
