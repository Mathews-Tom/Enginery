"""``enginery factory uninstall``: reversible removal of exactly what setup created.

The plan comes from the private ownership record alone. A file is removed only when its
current sha256 still equals the one setup recorded; a file the user changed is kept and
reported as user-owned; a file that is already gone is a no-op. The directory setup created is
removed only while it is empty, and the record itself goes last, so a second run finds nothing
to do. Setup never touches ``user.toml``, the ledger, evidence, the capability store or
lockfile, or authentication state, and the record is checked to name only the two paths
setup ever creates: a tampered record that names anything else is refused, not obeyed.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from enginery.cli.config_locations import PROJECT_POLICY_RELATIVE, private_state_directory
from enginery.cli.factory_setup import (
    DIRECTORY_ROLE,
    POLICY_DIRECTORY_RELATIVE,
    PROJECT_POLICY_ROLE,
)
from enginery.cli.setup_files import (
    OWNERSHIP_FILE,
    OwnedFile,
    load_ownership,
    read_regular,
    refuse_links,
    remove_empty_directory,
    remove_regular,
    require_private_path,
    sha256_bytes,
)
from enginery.domain.errors import InvalidInputError

NEVER_TOUCHED: tuple[str, ...] = (
    "user.toml",
    "the ledger",
    "evidence and artifacts",
    "the capability store",
    "the capability lockfile",
    "authentication state",
)


@dataclass(frozen=True, slots=True)
class RemovalOutcome:
    """What uninstall does with one setup-created path."""

    path: Path
    role: str
    action: str  # "remove", "keep" or "already-removed"
    detail: str

    def to_json(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "role": self.role,
            "action": self.action,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class UninstallPlan:
    project: Path
    outcomes: tuple[RemovalOutcome, ...]
    record_path: Path
    has_record: bool

    @property
    def kept(self) -> tuple[RemovalOutcome, ...]:
        return tuple(item for item in self.outcomes if item.action == "keep")

    def to_json(self, *, applied: bool, removed: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "mode": "apply" if applied else "preview",
            "project": str(self.project),
            "files": [item.to_json() for item in self.outcomes],
            "ownership_record": {
                "path": str(self.record_path),
                "action": "remove" if self.has_record else "none",
            },
            "kept_user_owned": [
                str(item.path) for item in self.kept if item.role == PROJECT_POLICY_ROLE
            ],
            "kept_not_empty": [str(item.path) for item in self.kept if item.role == DIRECTORY_ROLE],
            "removed": list(removed),
            "never_touched": list(NEVER_TOUCHED),
            "status": "nothing-to-uninstall" if not self.has_record else "ok",
        }


def validate_owned_path(project: Path, owned: OwnedFile) -> Path:
    """Validate the recorded role and path, refusing traversal and links."""
    try:
        relative = Path(owned.path).relative_to(project)
    except ValueError as error:
        raise InvalidInputError(
            "the setup ownership record names a path outside the project; refusing to act on it",
            details={"path": owned.path},
        ) from error
    allowed = {
        PROJECT_POLICY_ROLE: PROJECT_POLICY_RELATIVE,
        DIRECTORY_ROLE: POLICY_DIRECTORY_RELATIVE,
    }
    if allowed.get(owned.role) != relative:
        raise InvalidInputError(
            "the setup ownership record names a path setup never creates; refusing to act on it",
            details={"path": owned.path, "role": owned.role},
        )
    refuse_links(project, relative, what="a setup-owned path")
    return relative


def plan_uninstall(project: Path, env: Mapping[str, str] | None = None) -> UninstallPlan:
    """What uninstall would do; reads the record and the owned files, changes nothing."""
    record_path = private_state_directory(project, env) / OWNERSHIP_FILE
    require_private_path(record_path, what="the setup ownership record")
    record = load_ownership(record_path, project=project)
    if record is None:
        return UninstallPlan(project, (), record_path, has_record=False)
    outcomes: list[RemovalOutcome] = []
    removing_policy = False
    ordered = sorted(record.files, key=lambda item: item.role == DIRECTORY_ROLE)
    for owned in ordered:
        relative = validate_owned_path(project, owned)
        path = project / relative
        if owned.role == PROJECT_POLICY_ROLE:
            current = read_regular(path, what="the project policy file")
            if current is None:
                outcomes.append(
                    RemovalOutcome(path, owned.role, "already-removed", "no file at this path")
                )
            elif sha256_bytes(current) == owned.sha256:
                removing_policy = True
                outcomes.append(
                    RemovalOutcome(path, owned.role, "remove", "still exactly what setup wrote")
                )
            else:
                outcomes.append(
                    RemovalOutcome(
                        path,
                        owned.role,
                        "keep",
                        "changed since setup wrote it, so it is user-owned and is kept",
                    )
                )
            continue
        if not os.path.lexists(path):
            outcomes.append(
                RemovalOutcome(path, owned.role, "already-removed", "no directory at this path")
            )
            continue
        remaining = set(os.listdir(path)) - (
            {PROJECT_POLICY_RELATIVE.name} if removing_policy else set()
        )
        if remaining:
            outcomes.append(
                RemovalOutcome(
                    path,
                    owned.role,
                    "keep",
                    "holds files uninstall will not remove, so the directory is kept",
                )
            )
        else:
            outcomes.append(
                RemovalOutcome(path, owned.role, "remove", "empty once setup's file is removed")
            )
    return UninstallPlan(project, tuple(outcomes), record_path, has_record=True)


def apply_uninstall(plan: UninstallPlan) -> tuple[str, ...]:
    """Perform exactly the previewed removals, then the ownership record."""
    require_private_path(plan.record_path, what="the setup ownership record")
    removed: list[str] = []
    for outcome in plan.outcomes:
        if outcome.action != "remove":
            continue
        relative = outcome.path.relative_to(plan.project)
        if outcome.role == PROJECT_POLICY_ROLE:
            remove_regular(plan.project, relative, what="the project policy file")
            removed.append(str(outcome.path))
        elif remove_empty_directory(plan.project, relative, what="the policy directory"):
            removed.append(str(outcome.path))
    if plan.has_record:
        require_private_path(plan.record_path, what="the setup ownership record")
        plan.record_path.unlink()
        removed.append(str(plan.record_path))
        with contextlib.suppress(OSError):  # the private directory is shared when not empty
            require_private_path(plan.record_path, what="the setup ownership record")
            plan.record_path.parent.rmdir()
    return tuple(removed)


__all__ = [
    "NEVER_TOUCHED",
    "RemovalOutcome",
    "UninstallPlan",
    "apply_uninstall",
    "plan_uninstall",
    "validate_owned_path",
]
