from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.cli import factory_collect
from enginery.cli.factory_collect import collect, index_path, read_index


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / ".enginery").mkdir(parents=True)
    (root / ".enginery" / "project.toml").write_text(
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n', encoding="utf-8"
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(factory_collect, "repository_name", lambda _: "owner/repo")
    monkeypatch.setattr(factory_collect, "intake_prerequisites", lambda _: [])
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
    run_id = recorded.issues["owner/repo#1"].run_id
    second = collect(project, dry_run=True)
    assert second["candidates"][0]["dedup"] == "bound:" + run_id
    monkeypatch.setattr(
        factory_collect, "github_ledger", lambda *_: adapter("2026-10-17T00:00:00Z")
    )
    changed = collect(project, dry_run=True)
    assert changed["candidates"][0]["dedup"] == "changed:" + run_id
    assert f"issue changed since run {run_id} was bound" in changed["holds"][0]["detail"]
    assert "--supersede" in changed["holds"][0]["resolve"]
