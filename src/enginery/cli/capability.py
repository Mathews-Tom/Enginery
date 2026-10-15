"""``enginery capability``: lock drift checks and the scoped install lifecycle.

* ``lock --check`` verifies that every capability an existing lockfile records
  still has a materialized closure under the content-addressed store
  (:func:`enginery.capabilities.materialize.digest_path`) that matches the
  locked file records exactly -- no missing, changed, or extra file and no
  symlink. It never re-fetches from a live source and never grants an
  approval. A lockfile in the previous single-blob schema cannot be checked
  against a closure at all: every entry is reported as needing re-resolution
  rather than being silently upgraded. ``--replace-legacy-lock`` on ``inspect``,
  ``preview`` and ``install`` is the explicit way to resolve fresh past such a
  file (``install`` then replaces it).
* ``inspect`` resolves ``NAME@VERSION`` requests against the configured typed
  sources and reports each package's closure, trust, license, permissions,
  compatibility, approval requirement, and the selected source with every
  shadowed alternative (a shadowed source that fails is reported, not fatal).
  It reads sources only: it materializes nothing, writes nothing to the
  workspace, and executes nothing. A URL Git source or Armory is read over the
  network; any Git cache it needs is a temporary directory removed afterwards.
* ``preview`` is the same resolution plus the exact install plan (files to
  write, sizes, blockers, approvals still required). It has no side effects
  beyond those network reads; its exit code is non-zero when the plan has a
  blocker.
* ``install`` writes the verified closures into the task-local install root
  and records the owned-install manifest and lockfile. The lockfile keeps every
  earlier pin the new resolution does not replace. It never records an
  approval: an entry that needs exact-digest human approval is *held*, and the
  command prints the name, version, closure digest, and lock digest an owner
  would have to approve through the ``capability.materialize`` policy action.
  Project packages -- committed content of a pinned reviewed base commit --
  install without approval; a working directory is never a project source.
* ``uninstall`` removes only the owned, unmodified files of the active install
  and revokes its record. It never deletes the content-addressed store, the
  lockfile, the ledger, or any evidence.

Source precedence is ``project``, ``user``, ``git``, ``armory``; see
:mod:`enginery.capabilities.sources`.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enginery.adapters.armory import ArmoryCapabilitySource
from enginery.adapters.directory_capability import DirectoryCapabilitySource
from enginery.adapters.git_capability import GitCapabilitySource
from enginery.capabilities.errors import (
    CapabilityError,
    CapabilityIntegrityError,
    CapabilityLegacyLockError,
)
from enginery.capabilities.install import (
    BlockerKind,
    InstallPlan,
    PlannedApproval,
    UninstallReport,
    build_install_plan,
    install_lock,
    uninstall_install,
)
from enginery.capabilities.lock import CapabilityLock, LockedCapability, merge_locks
from enginery.capabilities.manifest import validate_name, validate_version
from enginery.capabilities.materialize import verify_materialized
from enginery.capabilities.resolver import (
    CapabilityRequest,
    CapabilityResolver,
    ResolvedCapabilities,
)
from enginery.capabilities.serialization import (
    is_legacy_lock,
    legacy_lock_from_json,
    lock_from_json,
    read_lock_document,
    write_lock,
)
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.domain.errors import FailureClass, InvalidInputError

DEFAULT_LOCKFILE = Path(".enginery/capabilities.lock.json")
DEFAULT_CAPABILITIES_ROOT = Path(".enginery/capabilities")
DEFAULT_INSTALL_ROOT = Path(".enginery/installed")
DEFAULT_GIT_CACHE = Path(".enginery/git-cache")

_BLOCKER_FAILURE_CLASS = {
    BlockerKind.LICENSE_HOLD: FailureClass.VALIDATION_FAILURE,
    BlockerKind.INCOMPATIBLE: FailureClass.VALIDATION_FAILURE,
    BlockerKind.CONFLICT: FailureClass.EXTERNAL_CONFLICT,
    BlockerKind.APPROVAL_REQUIRED: FailureClass.HUMAN_ACTION_REQUIRED,
}
_BLOCKER_PRIORITY = (
    BlockerKind.LICENSE_HOLD,
    BlockerKind.INCOMPATIBLE,
    BlockerKind.CONFLICT,
    BlockerKind.APPROVAL_REQUIRED,
)


@dataclass(frozen=True, slots=True)
class CapabilityDriftFinding:
    name: str
    version: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class CapabilityLockCheckReport:
    lockfile: Path | None
    findings: tuple[CapabilityDriftFinding, ...]

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)


def check_lock(*, lockfile: Path, capabilities_root: Path) -> CapabilityLockCheckReport:
    """Verify a lockfile's entries against the materialized store, if the lockfile exists.

    A missing lockfile is not drift -- it means no capability has been
    locked yet, matching "the engine works with Armory disabled" and,
    more generally, with no capabilities configured at all.
    """

    if not lockfile.exists():
        return CapabilityLockCheckReport(lockfile=None, findings=())
    document = read_lock_document(lockfile)
    if is_legacy_lock(document):
        legacy = legacy_lock_from_json(document)
        findings = tuple(
            CapabilityDriftFinding(
                name=entry.name,
                version=entry.version,
                ok=False,
                detail="legacy single-blob lock entry has no closure; re-resolve the capability",
            )
            for entry in legacy.entries
        )
        return CapabilityLockCheckReport(lockfile=lockfile, findings=findings)
    lock = lock_from_json(document)
    findings = tuple(_check_entry(entry, capabilities_root) for entry in lock.entries)
    return CapabilityLockCheckReport(lockfile=lockfile, findings=findings)


def _check_entry(entry: LockedCapability, root: Path) -> CapabilityDriftFinding:
    try:
        verify_materialized(entry, root)
    except CapabilityIntegrityError as error:
        differences = error.details.get("differences")
        suffix = f" ({'; '.join(map(str, differences))})" if isinstance(differences, list) else ""
        return CapabilityDriftFinding(
            name=entry.name, version=entry.version, ok=False, detail=f"{error}{suffix}"
        )
    return CapabilityDriftFinding(
        name=entry.name, version=entry.version, ok=True, detail="matches the locked closure"
    )


# --------------------------------------------------------------------------- parser


def _add_path_arguments(parser: argparse.ArgumentParser, *, install_root: bool = True) -> None:
    parser.add_argument("--lockfile", type=Path, default=DEFAULT_LOCKFILE)
    parser.add_argument("--capabilities-root", type=Path, default=DEFAULT_CAPABILITIES_ROOT)
    if install_root:
        parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")


def _add_source_arguments(parser: argparse.ArgumentParser, *, writes_cache: bool = False) -> None:
    parser.add_argument(
        "requests", nargs="+", metavar="NAME@VERSION", help="Capabilities to resolve."
    )
    parser.add_argument(
        "--project-source",
        metavar="REPOSITORY@COMMIT",
        help="Reviewed base: a Git repository (path or URL) pinned to a full 40-hex commit sha; "
        "its committed tree is read, never a working tree.",
    )
    parser.add_argument(
        "--project-subdir",
        default="",
        help="Directory inside the project source that holds the package directories.",
    )
    parser.add_argument(
        "--user-source",
        type=Path,
        action="append",
        default=[],
        help="User-local package directory; run-introduced and unauthenticated. Repeatable.",
    )
    parser.add_argument(
        "--git-source",
        action="append",
        default=[],
        metavar="REPOSITORY@COMMIT",
        help="Git repository (path or URL) pinned to a full 40-hex commit sha. Repeatable.",
    )
    parser.add_argument(
        "--git-subdir",
        default="",
        help="Directory inside each Git source that holds the package directories.",
    )
    if writes_cache:
        parser.add_argument(
            "--git-cache",
            type=Path,
            default=DEFAULT_GIT_CACHE,
            help="Directory caching URL Git sources (written by install only).",
        )
    parser.add_argument(
        "--replace-legacy-lock",
        action="store_true",
        help="Resolve fresh past a legacy single-blob lockfile; install replaces that file.",
    )
    parser.add_argument(
        "--armory", action="store_true", help="Also resolve from the Armory catalog (network)."
    )
    parser.add_argument(
        "--armory-revision", help="Full 40-hex Armory commit sha (default: current main)."
    )


def add_capability_parser(subparsers: Any) -> None:
    capability_parser = subparsers.add_parser(
        "capability", help="Capability lock and scoped install commands."
    )
    commands = capability_parser.add_subparsers(dest="capability_command")

    lock_parser = commands.add_parser("lock", help="Inspect or verify a capability lock.")
    lock_parser.add_argument(
        "--check", action="store_true", help="Verify the materialized store against the lockfile."
    )
    _add_path_arguments(lock_parser, install_root=False)

    inspect_parser = commands.add_parser(
        "inspect", help="Report closure, trust, license, and approval status; no side effects."
    )
    _add_source_arguments(inspect_parser)
    _add_path_arguments(inspect_parser)

    preview_parser = commands.add_parser(
        "preview", help="Show the exact install plan; no side effects."
    )
    _add_source_arguments(preview_parser)
    _add_path_arguments(preview_parser)

    install_parser = commands.add_parser(
        "install", help="Install verified closures into the task-local install root."
    )
    _add_source_arguments(install_parser, writes_cache=True)
    _add_path_arguments(install_parser)

    uninstall_parser = commands.add_parser(
        "uninstall", help="Remove the owned install files and revoke the install record."
    )
    _add_path_arguments(uninstall_parser)


# --------------------------------------------------------------------------- sources


def _parse_requests(values: Sequence[str]) -> tuple[CapabilityRequest, ...]:
    requests: list[CapabilityRequest] = []
    for raw in values:
        name, separator, version = raw.partition("@")
        if not separator or not version:
            raise InvalidInputError(
                "a capability request must be NAME@VERSION", details={"request": raw}
            )
        requests.append(
            CapabilityRequest(name=validate_name(name), version=validate_version(version))
        )
    return tuple(requests)


def _git_port(
    raw: str, *, flag: str, subdir: str, git_cache: Path
) -> tuple[str, GitCapabilitySource]:
    repository, separator, revision = raw.rpartition("@")
    if not separator or not repository:
        raise InvalidInputError(
            f"{flag} must be REPOSITORY@COMMIT with a full 40-hex commit sha",
            details={"source": raw},
        )
    port = GitCapabilitySource(
        repository=repository,
        revision=revision,
        package_root=subdir,
        cache_directory=git_cache,
    )
    return repository, port


def build_sources(args: argparse.Namespace, *, git_cache: Path) -> list[CapabilitySource]:
    """The typed sources the flags select; precedence is applied by the resolver.

    The project source is read with git plumbing from one pinned commit and
    never from a working tree, so what it serves is the committed content of a
    reviewed base revision. A user directory is the only directory-backed source.
    """

    sources: list[CapabilitySource] = []
    if args.project_source is not None:
        repository, project_port = _git_port(
            args.project_source,
            flag="--project-source",
            subdir=args.project_subdir,
            git_cache=git_cache,
        )
        sources.append(
            CapabilitySource(
                kind=SourceKind.PROJECT, label=f"project:{repository}", port=project_port
            )
        )
    for directory in args.user_source:
        sources.append(
            CapabilitySource(
                kind=SourceKind.USER,
                label=f"user:{directory}",
                port=DirectoryCapabilitySource(directory, provenance="user-directory"),
            )
        )
    for raw in args.git_source:
        repository, port = _git_port(
            raw, flag="--git-source", subdir=args.git_subdir, git_cache=git_cache
        )
        sources.append(CapabilitySource(kind=SourceKind.GIT, label=f"git:{repository}", port=port))
    if args.armory:
        sources.append(
            CapabilitySource(
                kind=SourceKind.ARMORY,
                label="armory",
                port=ArmoryCapabilitySource(revision=args.armory_revision),
            )
        )
    if not sources:
        raise InvalidInputError(
            "at least one source is required: --project-source, --user-source, "
            "--git-source, or --armory"
        )
    return sources


def _previous_lock(args: argparse.Namespace) -> CapabilityLock | None:
    """The lockfile's current-schema pins, or ``None`` when there is none to honour.

    A legacy single-blob lockfile cannot be read as pins. Without
    ``--replace-legacy-lock`` it is refused with the explicit way forward; with
    it, resolution starts fresh and ``install`` replaces the legacy file.
    """

    if not args.lockfile.exists():
        return None
    document = read_lock_document(args.lockfile)
    if not is_legacy_lock(document):
        return lock_from_json(document)
    if args.replace_legacy_lock:
        return None
    raise CapabilityLegacyLockError(
        f"the lockfile at {args.lockfile} uses the legacy single-blob schema and cannot be "
        "read as pins; re-run with --replace-legacy-lock to resolve fresh (install then "
        "replaces the legacy lockfile), or move the file aside",
        details={"lockfile": str(args.lockfile)},
    )


def _resolve(
    args: argparse.Namespace, *, report_shadowed: bool, git_cache: Path
) -> tuple[ResolvedCapabilities, CapabilityLock | None]:
    previous = _previous_lock(args)
    resolver = CapabilityResolver(build_sources(args, git_cache=git_cache))
    resolved = resolver.resolve_with_files(
        _parse_requests(args.requests),
        previous_lock=previous,
        report_shadowed=report_shadowed,
    )
    return resolved, previous


def _resolve_read_only(args: argparse.Namespace) -> ResolvedCapabilities:
    """Resolve for ``inspect``/``preview``: any Git cache is a temporary directory.

    The cache is deleted afterwards, so these commands leave nothing behind; a
    URL Git source or Armory is still read over the network.
    """

    with tempfile.TemporaryDirectory(prefix="enginery-git-cache-") as cache:
        resolved, _ = _resolve(args, report_shadowed=True, git_cache=Path(cache))
    return resolved


# --------------------------------------------------------------------------- output


def _emit(args: argparse.Namespace, payload: dict[str, object], text: Sequence[str]) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        for line in text:
            print(line)


def _package_lines(plan: InstallPlan, *, with_files: bool) -> list[str]:
    lines: list[str] = []
    for package in plan.packages:
        source = package.selection.selected
        lines.append(
            f"{package.name}@{package.version} [{package.kind.value}] closure "
            f"{package.closure_digest}"
        )
        lines.append(
            f"  source {source.kind.value} ({source.label}) revision {package.revision}; "
            f"trust {package.trust.value}; license {package.license or 'none'} "
            f"({package.license_disposition.value})"
        )
        permissions = ", ".join(item.value for item in package.permissions) or "none"
        lines.append(
            f"  permissions {permissions}; compatibility {package.receipt.status.value}; "
            f"approval {'REQUIRED' if package.approval_required else 'not required'}"
        )
        for alternative in package.selection.shadowed:
            offered = alternative.descriptor
            detail = alternative.problem or (str(offered.digest) if offered else "")
            lines.append(f"  shadowed {alternative.kind.value} ({alternative.label}): {detail}")
        if with_files:
            for item in package.files:
                lines.append(
                    f"  write {item.destination} [{item.kind.value}] "
                    f"{item.size} bytes {item.digest}"
                )
    return lines


def _approval_lines(approvals: Sequence[PlannedApproval]) -> list[str]:
    return [
        f"HOLD {item.name}@{item.version}: closure {item.closure_digest} "
        f"lock {item.lock_digest} trust {item.trust.value}"
        for item in approvals
    ]


def _blocker_exit_code(plan: InstallPlan) -> int:
    for kind in _BLOCKER_PRIORITY:
        if any(blocker.kind is kind for blocker in plan.blockers):
            return exit_code_for(_BLOCKER_FAILURE_CLASS[kind])
    return SUCCESS


# --------------------------------------------------------------------------- commands


def _run_lock(args: argparse.Namespace) -> int:
    if not args.check:
        raise InvalidInputError("`enginery capability lock` currently requires --check")
    report = check_lock(lockfile=args.lockfile, capabilities_root=args.capabilities_root)
    if report.lockfile is None:
        detail = f"no capability lock at {args.lockfile}; nothing to check"
        if args.json:
            print(json.dumps({"lockfile": None, "ok": True, "detail": detail}, sort_keys=True))
        else:
            print(detail)
        return SUCCESS
    if args.json:
        payload = {
            "lockfile": str(report.lockfile),
            "ok": report.ok,
            "findings": [
                {"name": f.name, "version": f.version, "ok": f.ok, "detail": f.detail}
                for f in report.findings
            ],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        for finding in report.findings:
            status = "OK" if finding.ok else "DRIFT"
            print(f"{status} {finding.name}@{finding.version}: {finding.detail}")
        print("no drift" if report.ok else "drift detected")
    return SUCCESS if report.ok else exit_code_for(FailureClass.VALIDATION_FAILURE)


def _run_inspect(args: argparse.Namespace) -> int:
    resolved = _resolve_read_only(args)
    plan = build_install_plan(resolved, install_root=args.install_root)
    payload: dict[str, object] = {
        "lock_digest": str(plan.lock_digest),
        "packages": [package.to_json() for package in plan.packages],
        "approvals_required": [item.to_json() for item in plan.approvals_required],
    }
    lines = [f"lock {plan.lock_digest}", *_package_lines(plan, with_files=False)]
    lines.extend(_approval_lines(plan.approvals_required))
    _emit(args, payload, lines)
    return SUCCESS


def _run_preview(args: argparse.Namespace) -> int:
    resolved = _resolve_read_only(args)
    plan = build_install_plan(resolved, install_root=args.install_root)
    lines = [f"lock {plan.lock_digest}", f"install root {plan.install_root}"]
    lines.extend(_package_lines(plan, with_files=True))
    lines.extend(_approval_lines(plan.approvals_required))
    lines.extend(
        f"BLOCKED {item.kind.value} {item.package or '-'}: {item.detail}" for item in plan.blockers
    )
    lines.append("ready to install" if plan.ready else "not ready to install")
    _emit(args, plan.to_json(), lines)
    return _blocker_exit_code(plan)


def _run_install(args: argparse.Namespace) -> int:
    resolved, previous = _resolve(args, report_shadowed=False, git_cache=args.git_cache)
    merged = merge_locks(previous, resolved.lock)
    plan = build_install_plan(resolved, install_root=args.install_root)
    only_approvals = all(item.kind is BlockerKind.APPROVAL_REQUIRED for item in plan.blockers)
    if plan.blockers and only_approvals:
        hold = {"status": "hold", **plan.to_json()}
        lines = [
            "install held: exact-digest human approval is required and the CLI never records one",
            *_approval_lines(plan.approvals_required),
            "approve each closure and lock digest through the capability.materialize policy "
            "action, then install programmatically with the digest-bound approvals",
        ]
        if args.json:
            print(json.dumps(hold, sort_keys=True))
        else:
            for line in lines:
                print(line)
        return exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    report = install_lock(
        resolved,
        install_root=args.install_root,
        capabilities_root=args.capabilities_root,
    )
    write_lock(merged, args.lockfile)
    payload = {**report.to_json(), "lockfile": str(args.lockfile), "status": "installed"}
    lines = [
        f"installed {', '.join(report.packages)} into {report.install_root}",
        *(f"  wrote {path}" for path in report.files_written),
        f"owned manifest {report.manifest_path}",
        f"lockfile {args.lockfile}",
    ]
    if report.already_installed:
        lines.insert(0, "already installed; nothing to write")
    _emit(args, payload, lines)
    return SUCCESS


def _retained_records(args: argparse.Namespace, report: UninstallReport) -> list[dict[str, str]]:
    retained = [{"path": item.path, "reason": item.reason} for item in report.retained]
    for path, reason in (
        (args.capabilities_root, "content-addressed store is never deleted by uninstall"),
        (args.lockfile, "lockfile is never deleted by uninstall"),
    ):
        if path.exists():
            retained.append({"path": str(path), "reason": reason})
    return retained


def _run_uninstall(args: argparse.Namespace) -> int:
    report = uninstall_install(args.install_root)
    retained = _retained_records(args, report)
    payload = {
        **{key: value for key, value in report.to_json().items() if key != "retained"},
        "retained": retained,
        "status": "uninstalled",
    }
    lines = [
        f"uninstalled lock {report.lock_digest} from {report.install_root}",
        *(f"  removed {path}" for path in report.removed),
        *(f"  already absent {path}" for path in report.already_absent),
        *(f"  retained {item['path']}: {item['reason']}" for item in retained),
        f"revocation record {report.revocation_record}",
    ]
    _emit(args, payload, lines)
    return SUCCESS


def _describe(error: CapabilityError) -> str:
    """The error message with its structured details, so the offending name or path is visible."""

    if not error.details:
        return str(error)
    return f"{error}: {json.dumps(dict(error.details), sort_keys=True, default=str)}"


def run_capability(args: argparse.Namespace) -> int:
    command = args.capability_command
    if command is None:
        raise InvalidInputError(
            "a capability subcommand is required", details={"command": "capability"}
        )
    handlers = {
        "lock": _run_lock,
        "inspect": _run_inspect,
        "preview": _run_preview,
        "install": _run_install,
        "uninstall": _run_uninstall,
    }
    handler = handlers.get(command)
    if handler is None:  # pragma: no cover - argparse restricts the choices
        raise AssertionError(f"unhandled capability command: {command}")
    try:
        return handler(args)
    except CapabilityError as error:
        print(_describe(error), file=sys.stderr)
        return exit_code_for(error.failure_class)


__all__ = [
    "CapabilityDriftFinding",
    "CapabilityLockCheckReport",
    "add_capability_parser",
    "build_sources",
    "check_lock",
    "run_capability",
]
