from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.capabilities.serialization import write_lock
from enginery.cli.main import main
from enginery.cli.stage1_request import (
    _LOCAL_CONFIGURATION_DIGEST,
    _LOCAL_ENVIRONMENT_DIGEST,
    _NO_CAPABILITY_LOCK_DIGEST,
)
from enginery.domain.digests import Digest
from enginery.workflows.stage1 import stage1_request_from_state


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-m", "fixture", cwd=repository)
    return repository, _git("rev-parse", "HEAD", cwd=repository)


def _minimal_argv(
    *, tmp_path: Path, repository: Path, base_revision: str, output: Path, run_id: str = "run-1"
) -> list[str]:
    return [
        "stage1",
        "build-request",
        "--output",
        str(output),
        "--run-id",
        run_id,
        "--repository",
        "owner/repo",
        "--external-reference",
        "owner/repo#1",
        "--source-snapshot-reference",
        "issue:1@1",
        "--source-revision",
        "1",
        "--base-revision",
        base_revision,
        "--title",
        "Bounded change",
        "--objective",
        "Change one bounded behavior.",
        "--acceptance-criterion",
        "observable result",
        "--repository-path",
        str(repository),
        "--workspace-path",
        str(tmp_path / "workspace"),
        "--artifact-root",
        str(tmp_path / "artifacts"),
    ]


def test_build_request_output_round_trips_through_stage1_start_unmodified(
    tmp_path: Path,
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"

    build_exit = main(
        _minimal_argv(
            tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
        )
    )

    assert build_exit == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.run.repository == "owner/repo"

    database = tmp_path / "ledger.db"
    start_exit = main(
        [
            "stage1",
            "start",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--request",
            str(output),
        ]
    )

    assert start_exit == 0


def test_build_request_defaults_match_documented_example(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"

    main(
        _minimal_argv(
            tmp_path=tmp_path,
            repository=repository,
            base_revision=base_revision,
            output=output,
            run_id="issue-142",
        )
    )

    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.run.policy_set_version == "policy-v1"
    assert request.head_branch == "enginery/issue-142"
    assert request.validation_commands == (("uv", "run", "pytest", "-q"),)
    assert request.required_checks == ("CI",)
    assert request.repair_limit == 1
    assert request.implementation.time_budget_seconds == 1800
    assert request.implementation.cost_budget is not None
    assert str(request.implementation.cost_budget) == "5.0"
    assert request.implementation.permitted_capabilities == ("git",)
    assert request.implementation.evidence_requirements == ("redacted harness transcript",)
    assert request.execution_configuration.github_credential_reference == "operator-gh-cli"
    assert (
        request.execution_configuration.harness_credential_reference == "operator-harness-session"
    )
    assert request.execution_configuration.harness_executable == "omp"
    assert request.run.capability_lock_digest == _NO_CAPABILITY_LOCK_DIGEST
    assert request.run.environment_manifest_digest == _LOCAL_ENVIRONMENT_DIGEST
    assert request.run.configuration_snapshot_digest == _LOCAL_CONFIGURATION_DIGEST
    assert request.work_snapshot.work_item.id.value == "work-issue-142"


def test_build_request_derives_capability_lock_digest_from_real_lockfile(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    lockfile = tmp_path / "capabilities.lock.json"
    lock = CapabilityLock(
        entries=(
            LockedCapability(
                name="git",
                version="1.0.0",
                digest=Digest.of_bytes(b"capability-bytes"),
                provenance=ProvenanceRecord(
                    status=ProvenanceStatus.LOCAL_TRUSTED,
                    source_label="local",
                    signer_key_id=None,
                    verified_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                license=None,
                introduced_by_run=False,
            ),
        )
    )
    write_lock(lock, lockfile)
    output = tmp_path / "request.json"

    main(
        [
            *_minimal_argv(
                tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
            ),
            "--capability-lockfile",
            str(lockfile),
        ]
    )

    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.run.capability_lock_digest == lock.digest()
    assert request.run.capability_lock_digest != _NO_CAPABILITY_LOCK_DIGEST


def test_build_request_accepts_explicit_digest_override(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    override = Digest.of_bytes(b"real-environment-manifest")

    main(
        [
            *_minimal_argv(
                tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
            ),
            "--environment-manifest-digest",
            str(override),
        ]
    )

    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.run.environment_manifest_digest == override


def test_build_request_accepts_digest_override_from_file(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    config_file = tmp_path / "configuration.json"
    config_file.write_text('{"real": "configuration"}', encoding="utf-8")

    main(
        [
            *_minimal_argv(
                tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
            ),
            "--configuration-snapshot-file",
            str(config_file),
        ]
    )

    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.run.configuration_snapshot_digest == Digest.of_bytes(config_file.read_bytes())


def test_build_request_marks_inapplicable_criteria(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )
    argv += ["--acceptance-criterion", "second criterion", "--inapplicable-criterion", "1"]

    main(argv)

    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.applicable_criteria == (True, False)


def test_build_request_no_cost_budget_flag_omits_cost_budget(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )
    argv.append("--implementation-no-cost-budget")

    main(argv)

    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.implementation.cost_budget is None


def test_build_request_rejects_invalid_cost_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )
    argv += ["--implementation-cost-budget", "not-a-decimal"]

    exit_code = main(argv)

    assert exit_code != 0
    assert "must be a decimal" in capsys.readouterr().err
    assert not output.exists()


def test_build_request_claude_code_provider_defaults_executable(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )
    argv += ["--harness-provider", "claude-code"]

    main(argv)

    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.execution_configuration.harness_provider == "claude-code"
    assert request.execution_configuration.harness_executable == "claude"
