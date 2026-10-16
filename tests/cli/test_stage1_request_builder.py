from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.application.effective_config import EffectiveConfig
from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.work_ports import DeclaredWorkClassification
from enginery.capabilities.serialization import write_lock
from enginery.cli._exit_codes import exit_code_for
from enginery.cli.main import main
from enginery.cli.stage1_request import (
    _LOCAL_ENVIRONMENT_DIGEST,
    _NO_CAPABILITY_LOCK_DIGEST,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import FailureClass
from enginery.domain.execution_catalog import SupportTier
from enginery.workflows.stage1 import stage1_request_from_state
from tests.support.capabilities import lock_of, locked_entry
from tests.support.github import (
    CANONICAL_LABELS,
    ISSUE_BODY,
    ISSUE_TITLE,
    ISSUE_UPDATED_AT,
    StandInGitHub,
    write_stand_in_gh,
)


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
    *,
    tmp_path: Path,
    repository: Path,
    base_revision: str,
    output: Path,
    run_id: str = "run-1",
    github: StandInGitHub | None = None,
) -> list[str]:
    github = github or write_stand_in_gh(tmp_path / "github")
    return [
        "stage1",
        "build-request",
        "--runner",
        "trusted-local",
        "--output",
        str(output),
        "--run-id",
        run_id,
        "--repository",
        "owner/repo",
        "--external-reference",
        "owner/repo#1",
        "--github-executable",
        str(github.executable),
        "--base-revision",
        base_revision,
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


@pytest.mark.parametrize("harness", ["omp", "claude-code"])
def test_build_request_binds_a_host_default_selection_against_the_packaged_catalog(
    tmp_path: Path, harness: str
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"

    exit_code = main(
        [
            *_minimal_argv(
                tmp_path=tmp_path,
                repository=repository,
                base_revision=base_revision,
                output=output,
            ),
            "--harness-provider",
            harness,
        ]
    )

    assert exit_code == 0
    request = stage1_request_from_state(json.loads(output.read_text(encoding="utf-8")))
    selection = request.execution_selection
    assert selection.harness == harness
    assert not selection.legacy
    assert selection.mode.value == "host_default"
    assert selection.selected_model is None
    assert selection.selected_effort is None
    assert selection.catalog_digest == load_execution_catalog().digest
    assert selection.support_tier is SupportTier.EXPERIMENTAL


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
    assert request.implementation.cost_budget is None
    assert raw["implementation"]["cost_budget"] is None
    assert request.implementation.permitted_capabilities == ("git",)
    assert request.implementation.evidence_requirements == ("redacted harness transcript",)
    assert request.execution_configuration.github_credential_reference == "operator-gh-cli"
    assert (
        request.execution_configuration.harness_credential_reference == "operator-harness-session"
    )
    assert request.execution_configuration.harness_executable == "omp"
    assert request.run.capability_lock_digest == _NO_CAPABILITY_LOCK_DIGEST
    assert request.run.environment_manifest_digest == _LOCAL_ENVIRONMENT_DIGEST
    document = json.loads(output.read_text(encoding="utf-8"))
    assert (
        request.run.configuration_snapshot_digest
        == EffectiveConfig.from_state(document["effective_config"]).digest
    )
    assert request.work_snapshot.work_item.id.value == "github:owner/repo#1"


def test_build_request_derives_capability_lock_digest_from_real_lockfile(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    lockfile = tmp_path / "capabilities.lock.json"
    lock = lock_of(locked_entry("git", version="1.0.0", license_id=None))
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


def test_build_request_refuses_a_legacy_single_blob_capability_lockfile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base_revision = _repository(tmp_path)
    lockfile = tmp_path / "capabilities.lock.json"
    lockfile.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "name": "git",
                        "version": "1.0.0",
                        "digest": str(Digest.of_bytes(b"capability-bytes")),
                        "license": None,
                        "introduced_by_run": False,
                        "provenance": {
                            "status": "local_trusted",
                            "source_label": "local",
                            "signer_key_id": None,
                            "verified_at": "2026-01-01T00:00:00+00:00",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "request.json"

    exit_code = main(
        [
            *_minimal_argv(
                tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
            ),
            "--capability-lockfile",
            str(lockfile),
        ]
    )

    assert exit_code == exit_code_for(FailureClass.VALIDATION_FAILURE)
    assert "re-resolved" in capsys.readouterr().err
    assert not output.exists()


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


@pytest.mark.parametrize(
    "flag", ["--configuration-snapshot-digest", "--configuration-snapshot-file"]
)
def test_build_request_has_no_flag_to_supply_the_configuration_digest(
    tmp_path: Path, flag: str
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"

    with pytest.raises(SystemExit) as refused:
        main(
            [
                *_minimal_argv(
                    tmp_path=tmp_path,
                    repository=repository,
                    base_revision=base_revision,
                    output=output,
                ),
                flag,
                "sha256:" + "0" * 64,
            ]
        )

    assert refused.value.code == exit_code_for(FailureClass.INVALID_INPUT)
    assert not output.exists()


def test_build_request_marks_inapplicable_criteria(tmp_path: Path) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )
    argv += ["--inapplicable-criterion", "0"]

    main(argv)

    raw = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(raw)
    assert request.applicable_criteria == (False,)


def test_build_request_no_longer_offers_a_flag_to_drop_the_cost_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )
    argv.append("--implementation-no-cost-budget")

    with pytest.raises(SystemExit) as stopped:
        main(argv)

    assert stopped.value.code == 2
    assert "--implementation-no-cost-budget" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("value", ["5.0", "0", "not-a-decimal"])
def test_build_request_refuses_any_hard_dollar_budget_naming_that_no_adapter_enforces_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )
    argv += ["--implementation-cost-budget", value]

    exit_code = main(argv)

    assert exit_code == exit_code_for(FailureClass.INVALID_INPUT)
    error = capsys.readouterr().err
    assert "no supported harness enforces a hard-dollar budget" in error
    assert "--implementation-time-budget-seconds" in error
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


def _built(tmp_path: Path, *extra: str, github: StandInGitHub | None = None) -> tuple[int, Path]:
    repository, base_revision = _repository(tmp_path)
    from tests.support.behavioral import declare_behavioral_policy

    base_revision = declare_behavioral_policy(repository)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path,
        repository=repository,
        base_revision=base_revision,
        output=output,
        github=github,
    )
    return main([*argv, *extra]), output


def _start_and_advance(tmp_path: Path, request_path: Path) -> int:
    database = tmp_path / "ledger.db"
    assert (
        main(
            [
                "stage1",
                "start",
                "--database",
                str(database),
                "--owner",
                "operator",
                "--request",
                str(request_path),
            ]
        )
        == 0
    )
    return main(
        [
            "stage1",
            "watch",
            "--database",
            str(database),
            "--owner",
            "operator",
            "--run-id",
            "run-1",
            "--advance",
        ]
    )


def test_build_request_binds_the_adapter_snapshot_and_its_classification_verbatim(
    tmp_path: Path,
) -> None:
    github = write_stand_in_gh(tmp_path / "github")

    exit_code, output = _built(tmp_path, github=github)

    assert exit_code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    request = stage1_request_from_state(document)
    fetched = GitHubWorkLedger(
        GitHubAdapterConfig(
            repository="owner/repo",
            credential_reference="operator-gh-cli",
            executable=str(github.executable),
        )
    ).fetch("owner/repo#1")
    snapshot = request.work_snapshot
    source_url = "https://api.github.com/repos/owner/repo/issues/1"
    assert snapshot == fetched
    assert request.run.work_item_snapshot_digest == fetched.bound_digest
    assert snapshot.work_item.title == ISSUE_TITLE
    assert snapshot.work_item.acceptance_criteria == (ISSUE_BODY,)
    assert snapshot.source_revision.startswith(f"{ISSUE_UPDATED_AT}:sha256:")
    assert snapshot.classification_provenance == DeclaredWorkClassification(
        source_provider="github-issues", source_url=source_url, canonical_labels=CANONICAL_LABELS
    )
    assert document["classification_provenance"] == {
        "source_provider": "github-issues",
        "source_url": source_url,
        "canonical_labels": list(CANONICAL_LABELS),
    }


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ((), "exactly one declared work-kind label"),
        (("enginery/work-kind/issue",), "exactly one declared risk label"),
        (
            ("enginery/work-kind/issue", "enginery/risk/low", "enginery/risk/medium"),
            "exactly one declared risk label",
        ),
        (
            ("enginery/work-kind/issue", "enginery/risk/high"),
            "risk labels must use the exact declared vocabulary",
        ),
        (
            ("enginery/work-kind/milestone", "enginery/risk/low"),
            "work-kind labels must use the exact declared vocabulary",
        ),
    ],
    ids=["no-labels", "no-risk", "two-risks", "undeclared-risk", "undeclared-work-kind"],
)
def test_build_request_refuses_an_issue_without_one_declared_classification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], labels: tuple[str, ...], message: str
) -> None:
    github = write_stand_in_gh(tmp_path / "github")
    github.serve(labels=labels)

    exit_code, output = _built(tmp_path, github=github)

    assert exit_code == 2
    assert message in capsys.readouterr().err
    assert not output.exists()


def test_build_request_rejects_a_bad_pin_before_it_reads_the_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = write_stand_in_gh(tmp_path / "github")

    exit_code, output = _built(
        tmp_path, "--harness-provider", "claude-code", "--model", "opus", github=github
    )

    assert exit_code == 2
    assert "opus" in capsys.readouterr().err
    assert not output.exists()
    assert github.calls() == []


@pytest.mark.parametrize(
    "flag",
    [
        "--work-item-id",
        "--source-provider",
        "--work-kind",
        "--source-snapshot-reference",
        "--source-revision",
        "--title",
        "--objective",
        "--acceptance-criterion",
        "--constraint",
        "--risk-class",
    ],
)
def test_build_request_no_longer_accepts_a_flag_the_adapter_now_decides(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    with pytest.raises(SystemExit) as raised:
        _built(tmp_path, flag, "value")

    assert raised.value.code == 2
    assert f"unrecognized arguments: {flag} value" in capsys.readouterr().err
    assert not (tmp_path / "request.json").exists()


def test_a_built_request_qualifies_against_the_issue_it_was_built_from(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, output = _built(tmp_path)
    assert exit_code == 0
    capsys.readouterr()

    assert _start_and_advance(tmp_path, output) == 0

    watched = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert watched["action_taken"] == "qualify"


def test_a_built_request_is_stale_once_the_issue_it_was_built_from_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = write_stand_in_gh(tmp_path / "github")
    exit_code, output = _built(tmp_path, github=github)
    assert exit_code == 0
    github.serve(updated_at="2026-10-14T00:00:00Z")
    capsys.readouterr()

    assert _start_and_advance(tmp_path, output) == exit_code_for(FailureClass.STALE_EVIDENCE)

    assert "no longer matches the run request" in capsys.readouterr().err


@pytest.mark.parametrize("index", ["1", "-1"])
def test_build_request_rejects_an_inapplicable_criterion_outside_the_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], index: str
) -> None:
    exit_code, output = _built(tmp_path, "--inapplicable-criterion", index)

    assert exit_code == 2
    assert f"--inapplicable-criterion [{index}] is outside the issue's 1 acceptance" in (
        capsys.readouterr().err
    )
    assert not output.exists()


def test_build_request_names_a_github_cli_that_cannot_be_run_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "no-such-gh"
    github = StandInGitHub(
        executable=missing, state_path=tmp_path / "state.json", log_path=tmp_path / "calls.jsonl"
    )

    exit_code, output = _built(tmp_path, github=github)

    assert exit_code == exit_code_for(FailureClass.MISSING_PREREQUISITE)
    assert str(missing) in capsys.readouterr().err
    assert not output.exists()


def test_watch_advance_reports_a_github_cli_that_has_gone_missing_with_a_mapped_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    github = write_stand_in_gh(tmp_path / "github")
    exit_code, output = _built(tmp_path, github=github)
    assert exit_code == 0
    github.executable.unlink()
    capsys.readouterr()

    assert _start_and_advance(tmp_path, output) == exit_code_for(FailureClass.MISSING_PREREQUISITE)

    assert str(github.executable) in capsys.readouterr().err


_IMAGE = "sha256:" + "a" * 64


def _isolated_argv(**kwargs: object) -> list[str]:
    argv = _minimal_argv(**kwargs)  # type: ignore[arg-type]
    index = argv.index("trusted-local")
    argv[index] = "isolated-oci"
    return [*argv, "--runner-image", _IMAGE]


def test_an_isolated_request_must_name_its_validation_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _isolated_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )

    assert main(argv) == exit_code_for(FailureClass.INVALID_INPUT)
    error = capsys.readouterr().err
    assert "requires --validation-command" in error and "offline" in error
    assert not output.exists()

    # The host default spelled out explicitly cannot run offline in the image either.
    assert main([*argv, "--validation-command", "uv run pytest -q"]) == exit_code_for(
        FailureClass.INVALID_INPUT
    )
    assert not output.exists()

    assert main([*argv, "--validation-command", "python3 -m unittest"]) == 0
    request = stage1_request_from_state(json.loads(output.read_text(encoding="utf-8")))
    assert request.validation_commands == (("python3", "-m", "unittest"),)


def test_an_explicit_first_attempt_id_sets_the_first_generation_and_must_end_in_a_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )

    assert main([*argv, "--implementation-attempt-id", "issue-142-attempt-3"]) == 0
    request = stage1_request_from_state(json.loads(output.read_text(encoding="utf-8")))
    assert request.implementation.attempt_id == "issue-142-attempt-3"

    output.unlink()
    assert main([*argv, "--implementation-attempt-id", "first"]) == exit_code_for(
        FailureClass.INVALID_INPUT
    )
    assert "must end in -<number>" in capsys.readouterr().err
    assert not output.exists()


def test_protected_paths_are_bound_into_the_request_and_absent_when_not_given(
    tmp_path: Path,
) -> None:
    repository, base_revision = _repository(tmp_path)
    output = tmp_path / "request.json"
    argv = _minimal_argv(
        tmp_path=tmp_path, repository=repository, base_revision=base_revision, output=output
    )

    assert main(argv) == 0
    plain = json.loads(output.read_text(encoding="utf-8"))
    assert "protected_paths" not in plain, "a request without extras keeps its legacy shape"

    assert main([*argv, "--protected-path", "deploy/**", "--protected-path", "*.tf"]) == 0
    bound = json.loads(output.read_text(encoding="utf-8"))
    assert bound["protected_paths"] == ["deploy/**", "*.tf"]
    assert stage1_request_from_state(bound).protected_paths == ("deploy/**", "*.tf")
    assert bound["request_digest"] != plain["request_digest"]

    assert main([*argv, "--protected-path", "../escape"]) == exit_code_for(
        FailureClass.INVALID_INPUT
    )
