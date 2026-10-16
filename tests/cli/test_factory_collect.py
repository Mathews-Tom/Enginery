from __future__ import annotations

import json
import shlex
import sqlite3
import subprocess
from pathlib import Path

import pytest

from enginery.adapters.github import (
    GitHubAdapterConfig,
    GitHubCliUnavailableError,
    GitHubWorkLedger,
)
from enginery.application.factory_intake import IntakeEntry
from enginery.cli import factory_collect
from enginery.cli.factory_collect import collect, index_path, intake_prerequisites, read_index
from enginery.cli.main import _build_parser
from enginery.domain.errors import (
    AuthenticationFailureError,
    EngineryError,
    InvalidInputError,
    TransientProviderFailureError,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / ".enginery").mkdir(parents=True)
    (root / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n', encoding="utf-8"
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(factory_collect, "repository_name", lambda _: "owner/repo")
    monkeypatch.setattr(factory_collect, "intake_prerequisites", lambda *_: [])
    return root


def adapter(updated: str = "2026-10-16T00:00:00Z") -> GitHubWorkLedger:
    labels = [{"name": "enginery/work-kind/issue"}, {"name": "enginery/risk/low"}]

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        endpoint = argv[-1]
        issue = {
            "number": 1,
            "title": "Title\x1b[31m",
            "body": "Bounded change",
            "updated_at": updated,
            "url": "https://api.github.com/repos/owner/repo/issues/1",
            "labels": labels,
        }
        if "/issues?" in endpoint:
            payload = [issue, {**issue, "number": 2, "labels": labels[:1]}]
        elif "/labels?" in endpoint:
            payload = labels
        else:
            payload = issue
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    return GitHubWorkLedger(GitHubAdapterConfig("owner/repo", "gh-session"), command)


def test_dry_collect_has_no_local_or_external_mutations(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory_collect, "github_ledger", lambda *_: adapter())
    report = collect(project, dry_run=True)
    assert report["local_writes"] == []
    assert not index_path(project).parent.exists()
    assert report["external_effects"] == {
        "github_auth_probes": 0,
        "github_reads": 5,
        "github_mutations": 0,
        "hosted_inference_calls": 0,
    }
    eligible, held = report["candidates"]
    assert eligible["eligible"] and eligible["dedup"] == "new"
    assert eligible["selection"]["harness"]["winner"] == "factory"
    assert not held["eligible"] and "risk" in held["holds"][0]["detail"]
    assert "\\u001b" in "\n".join(factory_collect.document_lines(report))


def test_collect_discloses_index_and_cursor_and_detects_changed_snapshot(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory_collect, "github_ledger", lambda *_: adapter())
    first = collect(project, dry_run=False)
    assert str(index_path(project)) in first["local_writes"]
    recorded = read_index(project).repositories["owner/repo"]
    assert recorded.last_seen_updated_at == "2026-10-16T00:00:00Z"
    assert recorded.issues == {}
    snapshot = adapter().fetch("owner/repo#1")
    recorded.issues["owner/repo#1"] = IntakeEntry(
        "owner/repo#1",
        "fr-existing",
        str(snapshot.bound_digest),
        str(snapshot.source_revision),
        "2026-10-16T00:00:00Z",
    )
    index = read_index(project)
    index.repositories["owner/repo"] = recorded
    factory_collect.write_document(index_path(project), index.to_state())
    second = collect(project, dry_run=True)
    assert second["candidates"][0]["dedup"] == "bound:fr-existing"
    monkeypatch.setattr(
        factory_collect, "github_ledger", lambda *_: adapter("2026-10-17T00:00:00Z")
    )
    changed = collect(project, dry_run=True)
    assert changed["candidates"][0]["dedup"] == "changed:fr-existing"
    assert all(hold["prerequisite"] != "bound snapshot" for hold in changed["holds"])


def assert_repair(command: str) -> None:
    for part in command.split("; "):
        argv = shlex.split(part)
        if argv[0] == "enginery":
            _build_parser().parse_args(argv[1:])
        elif argv[:3] == ["gh", "issue", "edit"]:
            assert argv[3].isascii() and argv[3].isdigit()
            assert argv[4:6] == ["--repo", "owner/repo"]
            assert len(argv[6:]) >= 2 and len(argv[6:]) % 2 == 0
            assert set(argv[6::2]) <= {"--add-label", "--remove-label"}
        else:
            assert argv[0] in {"gh", "git", "enginery-operator"}


@pytest.mark.parametrize(
    "labels",
    [
        (),
        ("enginery/work-kind/issue",),
        ("enginery/risk/low",),
        ("enginery/work-kind/issue", "enginery/risk/high"),
        ("enginery/work-kind/issue", "enginery/risk/low", "enginery/risk/medium"),
        ("enginery/work-kind/issue", "enginery/work-kind/issue", "enginery/risk/low"),
    ],
)
def test_classification_repairs_name_allowed_values_and_remove_offenders(
    labels: tuple[str, ...],
) -> None:
    repair = factory_collect.classification_repair("owner/repo", "owner/repo#12", labels)
    assert_repair(repair)
    argv = shlex.split(repair)
    if "enginery/risk/high" in labels:
        assert "--remove-label enginery/risk/high" in repair
    if not any(label.startswith("enginery/risk/") for label in labels):
        assert "enginery/risk/<low|medium>" in argv
    if not any(label.startswith("enginery/work-kind/") for label in labels):
        assert "enginery/work-kind/<issue|plan>" in argv


def test_paused_large_ledger_is_read_only(project: Path) -> None:
    path = factory_collect.database_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE process_manager_state "
            "(process_manager_name TEXT, state_key TEXT, state_json TEXT)"
        )
        connection.execute(
            "INSERT INTO process_manager_state VALUES (?, ?, ?)",
            ("operator-control", "control", json.dumps({"paused": True, "padding": "x" * 1100000})),
        )
    before = path.read_bytes()
    holds = factory_collect.paused_hold(project)
    assert holds[0]["prerequisite"] == "dispatch"
    assert_repair(holds[0]["resolve"])
    connection = factory_collect.read_ledger(project)
    assert connection is not None
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden (id INTEGER)")
    finally:
        connection.close()
    assert path.read_bytes() == before
    assert not path.with_name(path.name + "-wal").exists()


@pytest.mark.parametrize(
    ("remote", "code", "operation"),
    [
        ("", 2, "add"),
        ("https://example.com/o/r", 0, "set-url"),
        ("https://github.com/../repo", 0, "set-url"),
        ("https://github.com/owner/.", 0, "set-url"),
        ("https://github.com/öwner/repo", 0, "set-url"),
        ("https://github.com/owner/repo;bad", 0, "set-url"),
    ],
)
def test_invalid_origins_name_matching_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote: str,
    code: int,
    operation: str,
) -> None:
    monkeypatch.setattr(
        factory_collect.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, code, remote, ""),
    )
    with pytest.raises(InvalidInputError, match=f"git remote {operation} origin"):
        factory_collect.repository_name(tmp_path)


def test_cursor_does_not_skip_first_held_issue(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = adapter()
    original = ledger.command_runner

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        result = original(argv)
        if "/issues?" in argv[-1]:
            rows = json.loads(result.stdout)
            rows[0]["updated_at"] = "2026-10-18T00:00:00Z"
            rows[1]["updated_at"] = "2026-10-15T00:00:00Z"
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        return result

    ledger.command_runner = command
    monkeypatch.setattr(factory_collect, "github_ledger", lambda *_: ledger)
    collect(project, dry_run=False)
    assert read_index(project).repositories["owner/repo"].last_seen_updated_at is None


@pytest.mark.parametrize(
    "error_type",
    [
        AuthenticationFailureError,
        GitHubCliUnavailableError,
        TransientProviderFailureError,
    ],
)
def test_intake_failure_has_separate_hold_and_parsable_retry(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[EngineryError],
) -> None:
    ledger = adapter()
    original = ledger.command_runner

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        if "/issues?" not in argv[-1]:
            raise error_type("issue intake unavailable")
        return original(argv)

    ledger.command_runner = command
    monkeypatch.setattr(factory_collect, "github_ledger", lambda *_: ledger)
    report = collect(project, dry_run=False)
    assert report["holds"][0]["prerequisite"] == "issue intake"
    assert_repair(report["holds"][0]["resolve"])
    assert read_index(project).repositories["owner/repo"].last_seen_updated_at is None


def test_prerequisite_auth_probe_count_and_repairs(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enginery.cli import factory_diagnostics

    monkeypatch.setattr(factory_collect, "intake_prerequisites", intake_prerequisites)
    monkeypatch.setattr(factory_diagnostics, "_executable", lambda value: value)
    calls: list[tuple[str, ...]] = []

    def command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "not authenticated")

    monkeypatch.setattr(factory_collect, "_run", command)
    report = collect(project, dry_run=True)
    assert report["external_effects"]["github_auth_probes"] == 1
    assert len(calls) == 1 and calls[0][1:] == ("auth", "status")
    assert report["holds"][0]["prerequisite"] == "github-auth"
    for hold in report["holds"]:
        assert_repair(hold["resolve"])


def test_dry_collect_reports_pause_without_changing_private_files(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enginery.cli import factory_diagnostics

    path = factory_collect.database_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE process_manager_state "
            "(process_manager_name TEXT, state_key TEXT, state_json TEXT)"
        )
        connection.execute(
            "INSERT INTO process_manager_state VALUES (?, ?, ?)",
            ("operator-control", "control", '{"paused":true}'),
        )
    monkeypatch.setattr(factory_collect, "intake_prerequisites", intake_prerequisites)
    monkeypatch.setattr(factory_diagnostics, "_executable", lambda value: value)
    monkeypatch.setattr(
        factory_collect,
        "_run",
        lambda argv: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    before = {file.name: file.read_bytes() for file in path.parent.iterdir() if file.is_file()}
    report = collect(project, dry_run=True)
    assert report["local_writes"] == []
    assert report["candidates"] == []
    assert report["holds"][0]["prerequisite"] == "dispatch"
    assert_repair(report["holds"][0]["resolve"])
    after = {file.name: file.read_bytes() for file in path.parent.iterdir() if file.is_file()}
    assert before == after


def test_missing_github_has_no_auth_probe(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from enginery.cli import factory_diagnostics

    monkeypatch.setattr(factory_collect, "intake_prerequisites", intake_prerequisites)
    monkeypatch.setattr(factory_diagnostics, "_executable", lambda _: None)
    report = collect(project, dry_run=True)
    assert report["holds"][0]["prerequisite"] == "github-cli"
    assert report["external_effects"]["github_auth_probes"] == 0
    assert report["external_effects"]["github_reads"] == 0


def test_missing_trust_mode_names_allowed_configuration(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project / ".enginery" / "project.toml").write_text("schema_version = 1\n")
    monkeypatch.setattr(factory_collect, "intake_prerequisites", intake_prerequisites)
    report = collect(project, dry_run=True)
    assert report["holds"][0]["prerequisite"] == "trust mode"
    repair = report["holds"][0]["resolve"]
    assert '"trusted-local" or "isolated-oci"' in repair
    assert str(project / ".enginery" / "project.toml") in repair


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_read_only_ledger_refuses_links(
    project: Path,
    tmp_path: Path,
    link_kind: str,
) -> None:
    target = tmp_path / "target.sqlite"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE untouched (id INTEGER)")
    path = factory_collect.database_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    if link_kind == "symlink":
        path.symlink_to(target)
    else:
        path.hardlink_to(target)
    with pytest.raises(InvalidInputError):
        factory_collect.read_ledger(project)
