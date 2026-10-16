"""``enginery factory explain``: why every compiled field has the value it has.

Reads the same compiler output ``doctor`` and ``build-request`` use. For every field it
reports the value, the winning layer, each shadowed lower-layer value, the hard constraints
that applied and the source file and sha256 of every layer; it reports the files setup
created with their ownership status; and it lists the holds that can be derived offline. It
reads local files only and changes nothing; host prerequisites are ``factory doctor``'s job.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from enginery.application.effective_config import (
    COMPILED_FIELD_NAMES,
    MERGED,
    UNSET,
    CompiledConfig,
)
from enginery.application.project_policy import BUILTIN_CAPABILITIES
from enginery.capabilities.install import load_owned_manifest
from enginery.cli.config_locations import (
    PROJECT_POLICY_RELATIVE,
    private_state_directory,
)
from enginery.cli.factory_diagnostics import trust_mode_resolution
from enginery.cli.factory_setup import (
    DIRECTORY_ROLE,
    INSTALL_ROOT_RELATIVE,
    ProjectConfiguration,
)
from enginery.cli.factory_uninstall import validate_owned_path
from enginery.cli.setup_files import OWNERSHIP_FILE, load_ownership, read_regular


@dataclass(frozen=True, slots=True)
class OwnedFileStatus:
    path: str
    role: str
    status: str  # "owned", "user-modified", "missing" or "user-owned"
    detail: str

    def to_json(self) -> dict[str, object]:
        return {"path": self.path, "role": self.role, "status": self.status, "detail": self.detail}


def owned_file_statuses(
    project: Path, env: Mapping[str, str] | None = None
) -> tuple[OwnedFileStatus, ...]:
    """Every file setup recorded, judged against what is on disk now."""
    record = load_ownership(private_state_directory(project, env) / OWNERSHIP_FILE, project=project)
    statuses: list[OwnedFileStatus] = []
    recorded: set[str] = set()
    if record is not None:
        paths = [(owned, project / validate_owned_path(project, owned)) for owned in record.files]
        for owned, path in paths:
            recorded.add(owned.path)
            if owned.role == DIRECTORY_ROLE:
                present = os.path.lexists(path)
                statuses.append(
                    OwnedFileStatus(
                        owned.path,
                        owned.role,
                        "owned" if present else "missing",
                        "created by setup" if present else "no longer exists",
                    )
                )
                continue
            current = read_regular(path, what="a setup-owned file")
            if current is None:
                statuses.append(
                    OwnedFileStatus(owned.path, owned.role, "missing", "no longer exists")
                )
            elif hashlib.sha256(current).hexdigest() == owned.sha256:
                statuses.append(
                    OwnedFileStatus(
                        owned.path, owned.role, "owned", "unchanged since setup wrote it"
                    )
                )
            else:
                statuses.append(
                    OwnedFileStatus(
                        owned.path,
                        owned.role,
                        "user-modified",
                        "changed since setup wrote it; uninstall keeps it",
                    )
                )
    policy = project / PROJECT_POLICY_RELATIVE
    if str(policy) not in recorded and os.path.lexists(policy):
        statuses.append(
            OwnedFileStatus(
                str(policy), "project-policy", "user-owned", "not created by setup; never removed"
            )
        )
    return tuple(statuses)


def offline_holds(configuration: ProjectConfiguration) -> list[dict[str, str]]:
    """The holds derivable from local files alone."""
    compiled = configuration.compiled
    if compiled is None:
        return [
            {
                "prerequisite": "trust mode",
                "detail": "no layer declares a trust mode (trusted-local or isolated-oci)",
                "resolve": trust_mode_resolution(configuration.project / PROJECT_POLICY_RELATIVE),
            }
        ]
    manifest = load_owned_manifest(configuration.project / INSTALL_ROOT_RELATIVE)
    holds: list[dict[str, str]] = []
    for name in compiled.config.strings("permitted_capabilities"):
        if name in BUILTIN_CAPABILITIES or (manifest and manifest.package(name)):
            continue
        holds.append(
            {
                "prerequisite": f"capability {name}",
                "detail": f"capability {name} is permitted but not installed",
                "resolve": f"enginery capability preview {name}@<version> "
                "--project-source <REPOSITORY>@<COMMIT> --project-subdir <DIR>",
            }
        )
    return holds


def _fields(compiled: CompiledConfig) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for name in COMPILED_FIELD_NAMES:
        item = compiled.config.field(name)
        shadowed = [entry.to_state() for entry in item.shadowed]
        rows.append(
            {
                "name": name,
                "value": compiled.config.plain_value(name),
                "winner": item.winner,
                "chain": [entry.to_state() for entry in item.chain],
                "shadowed": shadowed,
            }
        )
    return rows


def explain(
    configuration: ProjectConfiguration, env: Mapping[str, str] | None = None
) -> dict[str, object]:
    """The explanation document; canonical JSON when serialized with sorted keys."""
    compiled = configuration.compiled
    document: dict[str, object] = {
        "project": str(configuration.project),
        "effective_configuration_digest": None if compiled is None else str(compiled.config.digest),
        "fields": [] if compiled is None else _fields(compiled),
        "hard_constraints": []
        if compiled is None
        else [constraint.to_state() for constraint in compiled.config.constraints],
        "sources": []
        if compiled is None
        else [record.to_state() for record in compiled.config.sources],
        "setup_owned_files": [
            item.to_json() for item in owned_file_statuses(configuration.project, env)
        ],
        "holds": offline_holds(configuration),
        "mutations": [],
    }
    return document


def explain_lines(document: Mapping[str, object]) -> list[str]:
    """Human-readable rendering of :func:`explain`."""
    lines = [f"effective configuration for {document['project']}"]
    digest = document["effective_configuration_digest"]
    lines.append(f"digest: {digest}" if digest else "digest: none (no trust mode is declared)")
    for row in document["fields"]:  # type: ignore[attr-defined]
        winner = row["winner"]
        origin = (
            "no layer sets it"
            if winner == UNSET
            else "merged across layers"
            if winner == MERGED
            else f"{winner} layer"
        )
        lines.append(f"  {row['name']} = {row['value']!r}  ({origin})")
        for entry in row["chain"]:
            marker = (
                "  <- wins" if entry is row["chain"][0] and winner not in {UNSET, MERGED} else ""
            )
            note = f" [{entry['note']}]" if entry["note"] else ""
            pinned = " [pinned]" if entry["pinned"] else ""
            lines.append(
                f"      {entry['layer']:9} {entry['value']!r}{pinned}{note}  "
                f"{entry['source']} {entry['source_digest'] or ''}{marker}".rstrip()
            )
    for constraint in document["hard_constraints"]:  # type: ignore[attr-defined]
        lines.append(
            f"  hard constraint {constraint['name']} = {constraint['value']!r} "
            f"({constraint['layer']} layer, {constraint['source']})"
        )
    for item in document["setup_owned_files"]:  # type: ignore[attr-defined]
        lines.append(f"  setup file [{item['status']}] {item['path']}: {item['detail']}")
    for hold in document["holds"]:  # type: ignore[attr-defined]
        lines.append(
            f"  HOLD {hold['prerequisite']}: {hold['detail']} (resolve: {hold['resolve']})"
        )
    return lines


__all__ = ["OwnedFileStatus", "explain", "explain_lines", "offline_holds", "owned_file_statuses"]
