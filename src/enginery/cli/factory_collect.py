"""Read-only GitHub intake preview and explicit private cursor recording."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.application.factory_intake import (
    IntakeEntry,
    IntakeIndex,
    RepositoryIntake,
    declared_classification,
    derive_run_id,
)
from enginery.cli.config_locations import private_state_directory, project_identity
from enginery.cli.factory_diagnostics import _check_github, _run
from enginery.cli.factory_explain import offline_holds
from enginery.cli.factory_setup import ProjectConfiguration, load_project_configuration
from enginery.cli.setup_files import read_regular, require_private_path, write_private_atomically
from enginery.domain.errors import EngineryError, InvalidInputError
from enginery.engine.operator_control import read_control_state
from enginery.ledger.service import LedgerService

MAX_DOCUMENT_BYTES = 1024 * 1024


def text(value: object) -> str:
    """Escape terminal controls in repository-controlled strings."""
    return re.sub(r"[\x00-\x1f\x7f]", lambda match: repr(match[0])[1:-1], str(value))


def repository_name(project: Path) -> str:
    result = subprocess.run(
        ("git", "-C", str(project), "remote", "get-url", "origin"),
        check=False,
        capture_output=True,
        text=True,
    )
    remote = result.stdout.strip()
    match = re.fullmatch(
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?",
        remote,
    )
    # A local bare origin is supported for offline operation when its basename is owner/repo.git.
    if match is None and remote.startswith("/"):
        path = Path(remote)
        if path.suffix == ".git":
            remote = f"{path.parent.name}/{path.stem}"
            match = re.fullmatch(r"([\w.-]+/[\w.-]+)", remote)
    if result.returncode or match is None:
        raise InvalidInputError(
            "origin must identify a GitHub owner/repository; repair: "
            "git remote add origin https://github.com/OWNER/REPO.git"
        )
    return match[1]


def index_path(project: Path) -> Path:
    return private_state_directory(project) / "intake" / "index.json"


def read_document(path: Path) -> object | None:
    require_private_path(path, what="factory private document")
    raw = read_regular(path, what="factory private document")
    if raw is None:
        return None
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise InvalidInputError("factory private document exceeds the 1 MiB limit")
    try:
        document: object = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise InvalidInputError("factory private document is not valid JSON") from error
    return document


def read_index(project: Path) -> IntakeIndex:
    raw = read_document(index_path(project))
    return IntakeIndex() if raw is None else IntakeIndex.from_state(raw)


def write_document(path: Path, document: Mapping[str, object]) -> None:
    raw = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise InvalidInputError("factory private document exceeds the 1 MiB limit")
    write_private_atomically(path, raw, what="factory private document")


@contextlib.contextmanager
def intake_lock(project: Path) -> Iterator[None]:
    path = index_path(project).with_suffix(".lock")
    require_private_path(path, what="factory intake lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_private_path(path, what="factory intake lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def paused_hold(project: Path) -> list[dict[str, str]]:
    database = private_state_directory(project) / "ledger.sqlite"
    require_private_path(database, what="project ledger")
    if not database.exists():
        return []
    ledger = LedgerService.open(database)
    try:
        paused = read_control_state(ledger).paused
    finally:
        ledger.close()
    return (
        []
        if not paused
        else [
            {
                "prerequisite": "dispatch",
                "detail": "dispatch is paused by the operator",
                "resolve": f"enginery factory pause --lift --project {project}",
            }
        ]
    )


def intake_prerequisites(configuration: ProjectConfiguration) -> list[dict[str, str]]:
    holds = offline_holds(configuration)
    compiled = configuration.compiled
    if compiled is not None:
        holds.extend(
            {
                "prerequisite": item.name,
                "detail": item.detail,
                "resolve": item.resolve or "enginery factory doctor",
            }
            for item in _check_github(compiled.config.text("github_executable"), _run)
            if item.status == "hold"
        )
    return [*holds, *paused_hold(configuration.project)]


def github_ledger(configuration: ProjectConfiguration, repository: str) -> GitHubWorkLedger:
    compiled = configuration.compiled
    if compiled is None:
        raise InvalidInputError("declare a runner with enginery factory init first")
    return GitHubWorkLedger(
        GitHubAdapterConfig(
            repository=repository,
            credential_reference=compiled.config.text("github_credential_reference"),
            executable=compiled.config.text("github_executable"),
        )
    )


def selection_document(configuration: ProjectConfiguration) -> dict[str, object]:
    compiled = configuration.compiled
    if compiled is None:
        return {}
    return {
        name: {
            "value": compiled.config.plain_value(name),
            "winner": compiled.config.field(name).winner,
        }
        for name in ("harness", "model", "effort")
    }


def collect(project: Path, *, dry_run: bool) -> dict[str, object]:
    configuration = load_project_configuration(project)
    holds = intake_prerequisites(configuration)
    document: dict[str, object] = {
        "project": str(project),
        "candidates": [],
        "holds": holds,
        "external_effects": {"github_reads": 0, "github_mutations": 0, "hosted_inference_calls": 0},
        "local_writes": [],
    }
    if holds:
        return document
    try:
        repository = repository_name(project)
    except InvalidInputError as error:
        holds.append(
            {
                "prerequisite": "origin",
                "detail": str(error),
                "resolve": "git remote add origin https://github.com/OWNER/REPO.git",
            }
        )
        return document
    with contextlib.nullcontext() if dry_run else intake_lock(project):
        index = read_index(project)
        intake = index.repositories.setdefault(repository, RepositoryIntake())
        adapter = github_ledger(configuration, repository)
        rows: list[dict[str, object]] = []
        compiled = configuration.compiled
        assert compiled is not None
        for candidate in adapter.list_candidates(since=intake.last_seen_updated_at):
            row: dict[str, object] = {
                "external_reference": candidate.external_reference,
                "title": candidate.title,
                "eligible": False,
                "dedup": "new",
                "classification": {},
                "workflow_profile": compiled.config.plain_value("profile"),
                "selection": selection_document(configuration),
                "runner": compiled.runner.to_state(),
                "budget": {
                    "time_budget_seconds": compiled.config.plain_value("time_budget_seconds"),
                    "cost": "unenforced",
                },
                "holds": [],
            }
            rows.append(row)
            if (
                intake.last_seen_updated_at is None
                or candidate.updated_at > intake.last_seen_updated_at
            ):
                intake.last_seen_updated_at = candidate.updated_at
            try:
                kind, risk, _ = declared_classification(candidate.labels)
                snapshot = adapter.fetch(candidate.external_reference)
            except EngineryError as error:
                hold = {
                    "prerequisite": "classification",
                    "detail": str(error),
                    "resolve": (
                        f"gh issue edit {candidate.external_reference} "
                        "--add-label enginery/work-kind/issue --add-label enginery/risk/low"
                    ),
                }
                row["classification"] = {"hold": str(error)}
                row["holds"] = [hold]
                holds.append(hold)
                continue
            row["eligible"] = True
            row["classification"] = {"work_kind": kind.value, "risk": risk.value}
            previous = intake.issues.get(candidate.external_reference)
            digest = str(snapshot.bound_digest)
            if previous is not None:
                row["dedup"] = (
                    "bound:" if previous.bound_digest == digest else "changed:"
                ) + previous.run_id
                if previous.bound_digest != digest:
                    hold = changed_hold(project, candidate.external_reference, previous.run_id)
                    row["holds"] = [hold]
                    holds.append(hold)
            elif not dry_run:
                intake.issues[candidate.external_reference] = IntakeEntry(
                    external_reference=candidate.external_reference,
                    run_id=derive_run_id(
                        project_identity(project), candidate.external_reference, digest
                    ),
                    bound_digest=digest,
                    source_revision=str(snapshot.source_revision),
                    recorded_at=datetime.now(UTC).isoformat(),
                )
        document["candidates"] = rows
        document["external_effects"] = {
            "github_reads": adapter.github_reads,
            "github_mutations": 0,
            "hosted_inference_calls": 0,
        }
        if not dry_run:
            write_document(index_path(project), index.to_state())
            document["local_writes"] = [
                str(index_path(project)),
                str(index_path(project).with_suffix(".lock")),
            ]
    return document


def changed_hold(project: Path, issue: str, run_id: str) -> dict[str, str]:
    database = private_state_directory(project) / "ledger.sqlite"
    return {
        "prerequisite": "bound snapshot",
        "detail": f"issue changed since run {run_id} was bound",
        "resolve": (
            f"enginery factory drain --stop --project {project}; "
            f"enginery stage1 cancel --database {database} --run-id {run_id}; "
            f"enginery factory run {issue} --supersede --project {project}"
        ),
    }


def document_lines(document: Mapping[str, object]) -> list[str]:
    """Render all external text with terminal controls escaped, preserving JSON types."""
    return [text(line) for line in json.dumps(document, indent=2, sort_keys=True).splitlines()]
