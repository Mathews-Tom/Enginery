"""Read-only GitHub intake preview and explicit private cursor recording."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.application.factory_intake import IntakeIndex, RepositoryIntake
from enginery.application.work_classification import (
    CLASSIFICATION_PREFIXES,
    RISK_LABELS,
    WORK_KIND_LABELS,
    declared_classification,
)
from enginery.cli.config_locations import private_state_directory
from enginery.cli.factory_diagnostics import _check_github, _run
from enginery.cli.factory_explain import offline_holds
from enginery.cli.factory_setup import ProjectConfiguration, load_project_configuration
from enginery.cli.setup_files import (
    read_regular,
    refuse_links,
    require_private_path,
    write_private_atomically,
)
from enginery.domain.errors import EngineryError, InvalidInputError
from enginery.engine.operator_control import CONTROL_STATE_KEY, OPERATOR_CONTROL_MANAGER

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
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?",
        remote,
    )
    # A local bare origin is supported for offline operation when its basename is owner/repo.git.
    if match is None and remote.startswith("/"):
        path = Path(remote)
        if path.suffix == ".git":
            remote = f"{path.parent.name}/{path.stem}"
            match = re.fullmatch(r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)", remote)
    if (
        result.returncode
        or match is None
        or any(part in {".", ".."} for part in match[1].split("/"))
    ):
        operation = "add" if result.returncode else "set-url"
        raise InvalidInputError(
            "origin must identify a GitHub owner/repository; repair: "
            f"git remote {operation} origin https://github.com/OWNER/REPO.git"
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


def database_path(project: Path) -> Path:
    return private_state_directory(project) / "ledger.sqlite"


def require_state_path(path: Path, *, what: str) -> None:
    root = path.parents[3]
    refuse_links(root, path.relative_to(root), what=what)
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise InvalidInputError(f"{what} must be a regular file with no hard links")


def read_ledger(project: Path) -> sqlite3.Connection | None:
    path = database_path(project)
    require_state_path(path, what="project ledger")
    if not path.exists():
        return None
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=1")
    return connection


def paused_hold(project: Path) -> list[dict[str, str]]:
    connection = read_ledger(project)
    if connection is None:
        return []
    try:
        record = connection.execute(
            "SELECT state_json FROM process_manager_state "
            "WHERE process_manager_name = ? AND state_key = ?",
            (OPERATOR_CONTROL_MANAGER, CONTROL_STATE_KEY),
        ).fetchone()
        paused = record is not None and json.loads(record["state_json"]).get("paused") is True
    finally:
        connection.close()
    return (
        []
        if not paused
        else [
            {
                "prerequisite": "dispatch",
                "detail": "dispatch is paused by the operator",
                "resolve": (
                    "enginery-operator resume --database "
                    + shlex.quote(str(database_path(project)))
                ),
            }
        ]
    )


def intake_prerequisites(
    configuration: ProjectConfiguration, effects: dict[str, int] | None = None
) -> list[dict[str, str]]:
    holds = offline_holds(configuration)
    compiled = configuration.compiled

    def probe(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if effects is not None and tuple(arguments[1:]) == ("auth", "status"):
            effects["github_auth_probes"] += 1
        return _run(arguments)

    if compiled is not None:
        holds.extend(
            {
                "prerequisite": item.name,
                "detail": item.detail,
                "resolve": item.resolve or "enginery factory doctor",
            }
            for item in _check_github(compiled.config.text("github_executable"), probe)
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


def classification_repair(repository: str, reference: str, labels: Sequence[str]) -> str:
    command = ["gh", "issue", "edit", reference.rsplit("#", 1)[1], "--repo", repository]
    for prefix, allowed, placeholder in (
        (CLASSIFICATION_PREFIXES[0], WORK_KIND_LABELS, "enginery/work-kind/<issue|plan>"),
        (CLASSIFICATION_PREFIXES[1], RISK_LABELS, "enginery/risk/<low|medium>"),
    ):
        present = [label for label in labels if label.casefold().startswith(prefix)]
        if len(present) != 1 or present[0] not in allowed:
            for label in dict.fromkeys(present):
                command.extend(("--remove-label", label))
            command.extend(("--add-label", placeholder))
    return shlex.join(command)


def collect(project: Path, *, dry_run: bool) -> dict[str, object]:
    configuration = load_project_configuration(project)
    effects = {
        "github_auth_probes": 0,
        "github_reads": 0,
        "github_mutations": 0,
        "hosted_inference_calls": 0,
    }
    holds = intake_prerequisites(configuration, effects)
    document: dict[str, object] = {
        "project": str(project),
        "candidates": [],
        "holds": holds,
        "external_effects": effects,
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
                "resolve": str(error).split("repair: ", 1)[1],
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
        evaluated: list[str] = []
        held_at: list[str] = []
        try:
            candidates = adapter.list_candidates(since=intake.last_seen_updated_at)
        except EngineryError as error:
            holds.append(
                {
                    "prerequisite": "issue intake",
                    "detail": str(error),
                    "resolve": (
                        "gh auth status; enginery factory collect --project "
                        + shlex.quote(str(project))
                    ),
                }
            )
            effects["github_reads"] = adapter.github_reads
            return document
        for candidate in candidates:
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
            try:
                kind, risk, _ = declared_classification(candidate.labels)
            except InvalidInputError as error:
                hold = {
                    "prerequisite": "classification",
                    "detail": str(error),
                    "resolve": classification_repair(
                        repository, candidate.external_reference, candidate.labels
                    ),
                }
                row["classification"] = {"hold": str(error)}
                row["holds"] = [hold]
                holds.append(hold)
                held_at.append(candidate.updated_at)
                continue
            try:
                snapshot = adapter.fetch(candidate.external_reference)
            except EngineryError as error:
                hold = {
                    "prerequisite": "issue intake",
                    "detail": str(error),
                    "resolve": (
                        "gh auth status; enginery factory collect --project "
                        f"{shlex.quote(str(project))}"
                    ),
                }
                row["classification"] = {"hold": str(error)}
                row["holds"] = [hold]
                holds.append(hold)
                held_at.append(candidate.updated_at)
                continue
            row["eligible"] = True
            row["classification"] = {"work_kind": kind.value, "risk": risk.value}
            digest = str(snapshot.bound_digest)
            evaluated.append(candidate.updated_at)
            reference = snapshot.work_item.external_reference
            row["external_reference"] = reference
            previous = intake.issues.get(reference)
            if previous is not None:
                row["dedup"] = (
                    "bound:" if previous.bound_digest == digest else "changed:"
                ) + previous.run_id
        ceiling = min(held_at) if held_at else None
        safe = [updated for updated in evaluated if ceiling is None or updated <= ceiling]
        if safe:
            intake.last_seen_updated_at = max(safe)
        document["candidates"] = rows
        effects["github_reads"] = adapter.github_reads
        if not dry_run:
            write_document(index_path(project), index.to_state())
            document["local_writes"] = [
                str(index_path(project)),
                str(index_path(project).with_suffix(".lock")),
            ]
    return document


def document_lines(document: Mapping[str, object]) -> list[str]:
    """Render all external text with terminal controls escaped, preserving JSON types."""
    return [text(line) for line in json.dumps(document, indent=2, sort_keys=True).splitlines()]
