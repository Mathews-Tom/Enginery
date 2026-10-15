"""Scoped, owned installation of locked package closures into a task-local root.

``build_install_plan`` is a pure preview: it reads the install root but
writes nothing, and reports every file that would be written, the approvals
still required (with exact digests), license and compatibility status, and
the selected source with every shadowed alternative.

``install_lock`` writes only inside the install root. It refuses unapproved
run-introduced packages, license holds, incompatible packages, and any
destination it does not already own, copies files from the *re-verified*
materialized closure, and records an owned-install manifest listing every
path it wrote with its digest.

``uninstall_install`` removes only files that the owned manifest lists *and*
whose current digest still matches. It never touches anything outside the
install root, never the content-addressed store, the lockfile, the ledger, or
any evidence or raw artifact, and it revokes the active install record by
replacing the manifest with a revocation tombstone. What it removed and what
it retained are both reported.
"""

from __future__ import annotations

import enum
import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from enginery.capabilities.closure import AssetKind, normalize_path, read_package_directory
from enginery.capabilities.compatibility import CompatibilityReceipt
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityIncompatibleError,
    CapabilityInstallConflictError,
    CapabilityLicenseHoldError,
    CapabilityNotInstalledError,
)
from enginery.capabilities.license import LicenseDisposition
from enginery.capabilities.lock import CapabilityLock, LockedCapability, ProvenanceStatus
from enginery.capabilities.manifest import (
    CapabilityPermission,
    PackageKind,
    digest_from_text,
    validate_name,
)
from enginery.capabilities.materialize import digest_path, materialize_lock, verify_materialized
from enginery.capabilities.resolver import ResolvedCapabilities
from enginery.capabilities.sources import SourceSelection
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

OWNED_MANIFEST_NAME = ".enginery-install.json"
REVOKED_RECORD_NAME = ".enginery-install-revoked.json"
INSTALL_SCHEMA_VERSION = 1


class BlockerKind(enum.StrEnum):
    APPROVAL_REQUIRED = "approval_required"
    LICENSE_HOLD = "license_hold"
    INCOMPATIBLE = "incompatible"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class PlanBlocker:
    kind: BlockerKind
    package: str | None
    detail: str

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind.value, "package": self.package, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """One file an install would write, as a task-local destination path."""

    package: str
    source_path: str
    destination: str
    kind: AssetKind
    size: int
    digest: Digest
    executable: bool

    def to_json(self) -> dict[str, object]:
        return {
            "package": self.package,
            "source_path": self.source_path,
            "destination": self.destination,
            "kind": self.kind.value,
            "size": self.size,
            "digest": str(self.digest),
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class PlannedApproval:
    """An approval still required, bound to the exact closure and whole-lock digests."""

    name: str
    version: str
    closure_digest: Digest
    lock_digest: Digest
    trust: ProvenanceStatus

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "closure_digest": str(self.closure_digest),
            "lock_digest": str(self.lock_digest),
            "trust": self.trust.value,
        }


@dataclass(frozen=True, slots=True)
class PlannedPackage:
    """Everything a preview reports about one locked package."""

    name: str
    version: str
    kind: PackageKind
    revision: str
    closure_digest: Digest
    trust: ProvenanceStatus
    introduced_by_run: bool
    source_label: str
    license: str | None
    license_disposition: LicenseDisposition
    redistribution_blocked: bool
    permissions: tuple[CapabilityPermission, ...]
    receipt: CompatibilityReceipt
    selection: SourceSelection
    approval_required: bool
    approved: bool
    files: tuple[PlannedFile, ...]
    dependencies: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind.value,
            "revision": self.revision,
            "closure_digest": str(self.closure_digest),
            "trust": self.trust.value,
            "introduced_by_run": self.introduced_by_run,
            "source_label": self.source_label,
            "license": self.license,
            "license_disposition": self.license_disposition.value,
            "redistribution_blocked": self.redistribution_blocked,
            "permissions": [permission.value for permission in self.permissions],
            "compatibility": self.receipt.to_json(),
            "source": self.selection.to_json(),
            "approval_required": self.approval_required,
            "approved": self.approved,
            "files": [item.to_json() for item in self.files],
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """A side-effect-free description of what installing a lock would do."""

    lock_digest: Digest
    install_root: Path
    packages: tuple[PlannedPackage, ...]
    approvals_required: tuple[PlannedApproval, ...]
    blockers: tuple[PlanBlocker, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def files(self) -> tuple[PlannedFile, ...]:
        return tuple(item for package in self.packages for item in package.files)

    def to_json(self) -> dict[str, object]:
        return {
            "lock_digest": str(self.lock_digest),
            "install_root": str(self.install_root),
            "ready": self.ready,
            "packages": [package.to_json() for package in self.packages],
            "approvals_required": [approval.to_json() for approval in self.approvals_required],
            "blockers": [blocker.to_json() for blocker in self.blockers],
        }


@dataclass(frozen=True, slots=True)
class OwnedFile:
    path: str
    digest: Digest
    size: int
    executable: bool


@dataclass(frozen=True, slots=True)
class OwnedPackage:
    name: str
    version: str
    closure_digest: Digest
    files: tuple[OwnedFile, ...]


@dataclass(frozen=True, slots=True)
class OwnedManifest:
    """The active install record: every path the install owns, with its digest."""

    lock_digest: Digest
    packages: tuple[OwnedPackage, ...]
    approved_closures: tuple[tuple[str, Digest], ...] = ()

    def package(self, name: str) -> OwnedPackage | None:
        return next((item for item in self.packages if item.name == name), None)

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": INSTALL_SCHEMA_VERSION,
            "lock_digest": str(self.lock_digest),
            "approved_closures": {name: str(digest) for name, digest in self.approved_closures},
            "packages": [
                {
                    "name": package.name,
                    "version": package.version,
                    "closure_digest": str(package.closure_digest),
                    "files": [
                        {
                            "path": item.path,
                            "digest": str(item.digest),
                            "size": item.size,
                            "executable": item.executable,
                        }
                        for item in package.files
                    ],
                }
                for package in self.packages
            ],
        }


@dataclass(frozen=True, slots=True)
class InstallReport:
    lock_digest: Digest
    install_root: Path
    manifest_path: Path
    packages: tuple[str, ...]
    files_written: tuple[str, ...]
    already_installed: bool

    def to_json(self) -> dict[str, object]:
        return {
            "lock_digest": str(self.lock_digest),
            "install_root": str(self.install_root),
            "manifest": str(self.manifest_path),
            "packages": list(self.packages),
            "files_written": list(self.files_written),
            "already_installed": self.already_installed,
        }


@dataclass(frozen=True, slots=True)
class RetainedPath:
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class UninstallReport:
    lock_digest: Digest
    install_root: Path
    revocation_record: Path
    removed: tuple[str, ...]
    retained: tuple[RetainedPath, ...]
    already_absent: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "lock_digest": str(self.lock_digest),
            "install_root": str(self.install_root),
            "revocation_record": str(self.revocation_record),
            "removed": list(self.removed),
            "retained": [{"path": item.path, "reason": item.reason} for item in self.retained],
            "already_absent": list(self.already_absent),
        }


# --------------------------------------------------------------------------- owned manifest


def load_owned_manifest(install_root: Path) -> OwnedManifest | None:
    """The active owned-install manifest under ``install_root``, or ``None`` when none exists."""

    path = install_root / OWNED_MANIFEST_NAME
    if not os.path.lexists(path):
        return None
    try:
        document = json.loads(_read_regular(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise InvalidInputError(f"owned install manifest at {path} is unreadable") from error
    return _manifest_from_json(document)


def _manifest_from_json(document: object) -> OwnedManifest:
    if not isinstance(document, dict) or document.get("schema_version") != INSTALL_SCHEMA_VERSION:
        raise InvalidInputError("owned install manifest has an unsupported schema")
    packages_raw, approved_raw = document.get("packages"), document.get("approved_closures", {})
    lock_text = document.get("lock_digest")
    if not isinstance(packages_raw, list) or not isinstance(approved_raw, dict):
        raise InvalidInputError("owned install manifest is malformed")
    if not isinstance(lock_text, str):
        raise InvalidInputError("owned install manifest is missing its lock digest")
    packages = tuple(_owned_package(item) for item in packages_raw)
    if len({package.name for package in packages}) != len(packages):
        raise InvalidInputError("owned install manifest repeats a package")
    approved = tuple(
        sorted(
            ((str(name), digest_from_text(str(value))) for name, value in approved_raw.items()),
            key=lambda pair: pair[0],
        )
    )
    return OwnedManifest(
        lock_digest=digest_from_text(lock_text), packages=packages, approved_closures=approved
    )


def _owned_package(item: object) -> OwnedPackage:
    if not isinstance(item, dict):
        raise InvalidInputError("owned install manifest package must be an object")
    name, version, digest = item.get("name"), item.get("version"), item.get("closure_digest")
    files = item.get("files")
    if not isinstance(name, str) or not isinstance(version, str) or not isinstance(digest, str):
        raise InvalidInputError("owned install manifest package identity is invalid")
    if not isinstance(files, list):
        raise InvalidInputError("owned install manifest package files must be a list")
    validate_name(name)
    return OwnedPackage(
        name=name,
        version=version,
        closure_digest=digest_from_text(digest),
        files=tuple(_owned_file(name, entry) for entry in files),
    )


def _owned_file(package: str, entry: object) -> OwnedFile:
    if not isinstance(entry, dict):
        raise InvalidInputError("owned install manifest file must be an object")
    path, digest = entry.get("path"), entry.get("digest")
    size, executable = entry.get("size"), entry.get("executable")
    if not isinstance(path, str) or not isinstance(digest, str):
        raise InvalidInputError("owned install manifest file path and digest must be strings")
    if not isinstance(size, int) or isinstance(size, bool) or not isinstance(executable, bool):
        raise InvalidInputError("owned install manifest file size or executable is invalid")
    normalize_path(path)
    if not path.startswith(f"{package}/"):
        raise InvalidInputError(
            "owned install manifest file is outside its package directory", details={"path": path}
        )
    return OwnedFile(path=path, digest=digest_from_text(digest), size=size, executable=executable)


def _write_json_atomically(path: Path, document: object) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


# --------------------------------------------------------------------------- filesystem guards


def _read_regular(path: Path) -> bytes:
    """Read ``path`` only if it is a regular file reached without following a link."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        return handle.read()


def _require_real_directory(path: Path, *, what: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise CapabilityInstallConflictError(
            f"{what} cannot be read", details={"path": str(path)}
        ) from error
    if not stat.S_ISDIR(info.st_mode):
        raise CapabilityInstallConflictError(
            f"{what} is not a real directory", details={"path": str(path)}
        )


def _guard_destination(root: Path, relative: str) -> Path:
    """The destination under ``root`` for ``relative``, refusing links on its parent chain."""

    normalize_path(relative)
    current = root
    parts = relative.split("/")
    for part in parts[:-1]:
        current = current / part
        if os.path.lexists(current):
            _require_real_directory(current, what="install directory")
    return root / relative


def _file_digest(path: Path) -> Digest | None:
    """Digest of a regular, non-linked file, or ``None`` if it is missing or not such a file."""

    try:
        return Digest.of_bytes(_read_regular(path))
    except OSError:
        return None


# --------------------------------------------------------------------------- planning


def _destinations(entry: LockedCapability) -> tuple[PlannedFile, ...]:
    return tuple(
        PlannedFile(
            package=entry.name,
            source_path=record.path,
            destination=f"{entry.name}/{record.path}",
            kind=record.kind,
            size=record.size,
            digest=record.digest,
            executable=record.executable,
        )
        for record in entry.files
    )


def build_install_plan(
    resolved: ResolvedCapabilities,
    *,
    install_root: Path,
    approved_names: frozenset[str] = frozenset(),
) -> InstallPlan:
    """Describe installing ``resolved`` into ``install_root`` without any side effect."""

    lock = resolved.lock
    lock_digest = lock.digest()
    owned = load_owned_manifest(install_root) if os.path.lexists(install_root) else None
    packages: list[PlannedPackage] = []
    approvals: list[PlannedApproval] = []
    blockers: list[PlanBlocker] = []
    if owned is not None and owned.lock_digest != lock_digest:
        blockers.append(
            PlanBlocker(
                BlockerKind.CONFLICT,
                None,
                f"an install of lock {owned.lock_digest} is already recorded; uninstall it first",
            )
        )
    for entry in lock.entries:
        planned_files = _destinations(entry)
        approved = (not entry.requires_human_approval()) or entry.name in approved_names
        packages.append(
            PlannedPackage(
                name=entry.name,
                version=entry.version,
                kind=entry.kind,
                revision=entry.revision,
                closure_digest=entry.digest,
                trust=entry.provenance.status,
                introduced_by_run=entry.introduced_by_run,
                source_label=entry.provenance.source_label,
                license=entry.license,
                license_disposition=entry.license_disposition,
                redistribution_blocked=entry.redistribution_blocked(),
                permissions=entry.permissions,
                receipt=entry.receipt,
                selection=resolved.selections[entry.name],
                approval_required=entry.requires_human_approval(),
                approved=approved,
                files=planned_files,
                dependencies=tuple(f"{pin.name}@{pin.version}" for pin in entry.dependencies),
            )
        )
        if not approved:
            approvals.append(
                PlannedApproval(
                    name=entry.name,
                    version=entry.version,
                    closure_digest=entry.digest,
                    lock_digest=lock_digest,
                    trust=entry.provenance.status,
                )
            )
            blockers.append(
                PlanBlocker(
                    BlockerKind.APPROVAL_REQUIRED,
                    entry.name,
                    f"exact-digest human approval required for closure {entry.digest}",
                )
            )
        if entry.redistribution_blocked():
            blockers.append(
                PlanBlocker(
                    BlockerKind.LICENSE_HOLD,
                    entry.name,
                    f"license {entry.license!r} is {entry.license_disposition.value}",
                )
            )
        if not entry.receipt.compatible:
            blockers.append(
                PlanBlocker(BlockerKind.INCOMPATIBLE, entry.name, "; ".join(entry.receipt.reasons))
            )
        blockers.extend(_conflicts(entry, planned_files, install_root, owned))
    return InstallPlan(
        lock_digest=lock_digest,
        install_root=install_root,
        packages=tuple(packages),
        approvals_required=tuple(approvals),
        blockers=tuple(blockers),
    )


def _conflicts(
    entry: LockedCapability,
    planned_files: Iterable[PlannedFile],
    install_root: Path,
    owned: OwnedManifest | None,
) -> list[PlanBlocker]:
    owned_package = owned.package(entry.name) if owned is not None else None
    owned_files = owned_package.files if owned_package else ()
    owned_digests = {item.path: item.digest for item in owned_files}
    found: list[PlanBlocker] = []
    for item in planned_files:
        target = install_root / item.destination
        if not os.path.lexists(target):
            continue
        owned_and_intact = (
            owned_digests.get(item.destination) == item.digest
            and _file_digest(target) == item.digest
        )
        if not owned_and_intact:
            found.append(
                PlanBlocker(
                    BlockerKind.CONFLICT,
                    entry.name,
                    f"destination {item.destination} exists and is not an owned install file",
                )
            )
    return found


# --------------------------------------------------------------------------- install


def _raise_for(blocker: PlanBlocker) -> None:
    details: dict[str, object] = {"package": blocker.package, "reason": blocker.detail}
    if blocker.kind is BlockerKind.LICENSE_HOLD:
        raise CapabilityLicenseHoldError("install refused: license hold", details=details)
    if blocker.kind is BlockerKind.INCOMPATIBLE:
        raise CapabilityIncompatibleError("install refused: incompatible package", details=details)
    if blocker.kind is BlockerKind.CONFLICT:
        raise CapabilityInstallConflictError("install refused: conflict", details=details)
    raise CapabilityApprovalRequiredError(
        "install refused: exact-digest human approval required", details=details
    )


_BLOCKER_PRIORITY = (
    BlockerKind.LICENSE_HOLD,
    BlockerKind.INCOMPATIBLE,
    BlockerKind.CONFLICT,
    BlockerKind.APPROVAL_REQUIRED,
)


def install_lock(
    resolved: ResolvedCapabilities,
    *,
    install_root: Path,
    capabilities_root: Path,
    approved_names: frozenset[str] = frozenset(),
) -> InstallReport:
    """Install every package of ``resolved`` into ``install_root`` and record ownership."""

    plan = build_install_plan(resolved, install_root=install_root, approved_names=approved_names)
    for kind in _BLOCKER_PRIORITY:
        for blocker in plan.blockers:
            if blocker.kind is kind:
                _raise_for(blocker)
    lock = resolved.lock
    owned = load_owned_manifest(install_root) if os.path.lexists(install_root) else None
    materialize_lock(
        lock, resolved.files_by_name, root=capabilities_root, approved_names=approved_names
    )
    install_root.mkdir(parents=True, exist_ok=True)
    _require_real_directory(install_root, what="install root")
    created_files: list[Path] = []
    created_directories: list[Path] = []
    try:
        written = _write_packages(
            lock, capabilities_root, install_root, created_files, created_directories
        )
        manifest = _owned_manifest(lock, approved_names)
        _write_json_atomically(install_root / OWNED_MANIFEST_NAME, manifest.to_json())
    except BaseException:
        for path in reversed(created_files):
            path.unlink(missing_ok=True)
        for directory in reversed(created_directories):
            _remove_if_empty(directory)
        raise
    return InstallReport(
        lock_digest=lock.digest(),
        install_root=install_root,
        manifest_path=install_root / OWNED_MANIFEST_NAME,
        packages=tuple(entry.name for entry in lock.entries),
        files_written=tuple(written),
        already_installed=owned is not None and not written,
    )


def _write_packages(
    lock: CapabilityLock,
    capabilities_root: Path,
    install_root: Path,
    created_files: list[Path],
    created_directories: list[Path],
) -> list[str]:
    written: list[str] = []
    for entry in lock.entries:
        verify_materialized(entry, capabilities_root)
        directory = digest_path(capabilities_root, entry.digest)
        sources = {item.path: item for item in read_package_directory(directory)}
        for record in entry.files:
            relative = f"{entry.name}/{record.path}"
            target = _guard_destination(install_root, relative)
            if _file_digest(target) == record.digest:
                continue
            _make_directories(install_root, relative, created_directories)
            _create_exclusive(target, sources[record.path].content, executable=record.executable)
            created_files.append(target)
            written.append(relative)
    return written


def _make_directories(root: Path, relative: str, created: list[Path]) -> None:
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        if not os.path.lexists(current):
            current.mkdir()
            created.append(current)


def _create_exclusive(target: Path, content: bytes, *, executable: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o555 if executable else 0o444)
    except FileExistsError as error:
        raise CapabilityInstallConflictError(
            "install destination appeared during install", details={"path": str(target)}
        ) from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    target.chmod(0o555 if executable else 0o444)


def _owned_manifest(lock: CapabilityLock, approved_names: frozenset[str]) -> OwnedManifest:
    packages = tuple(
        OwnedPackage(
            name=entry.name,
            version=entry.version,
            closure_digest=entry.digest,
            files=tuple(
                OwnedFile(
                    path=f"{entry.name}/{record.path}",
                    digest=record.digest,
                    size=record.size,
                    executable=record.executable,
                )
                for record in entry.files
            ),
        )
        for entry in lock.entries
    )
    approved = tuple(
        (entry.name, entry.digest)
        for entry in lock.entries
        if entry.requires_human_approval() and entry.name in approved_names
    )
    return OwnedManifest(lock_digest=lock.digest(), packages=packages, approved_closures=approved)


# --------------------------------------------------------------------------- uninstall


def uninstall_install(install_root: Path) -> UninstallReport:
    """Remove the owned, unmodified files of the active install and revoke its record."""

    manifest = load_owned_manifest(install_root)
    if manifest is None:
        raise CapabilityNotInstalledError(
            "no active install is recorded under the install root",
            details={"install_root": str(install_root)},
        )
    _require_real_directory(install_root, what="install root")
    removed: list[str] = []
    retained: list[RetainedPath] = []
    absent: list[str] = []
    directories: set[str] = set()
    for package in manifest.packages:
        for item in package.files:
            directories.update(_parents(item.path))
            try:
                target = _guard_destination(install_root, item.path)
            except CapabilityInstallConflictError:
                retained.append(RetainedPath(item.path, "reached through a link or non-directory"))
                continue
            if not os.path.lexists(target):
                absent.append(item.path)
                continue
            actual = _file_digest(target)
            if actual != item.digest:
                modified = actual is not None
                reason = "modified after install" if modified else "no longer a plain file"
                retained.append(RetainedPath(item.path, reason))
                continue
            target.unlink()
            removed.append(item.path)
    for directory in sorted(directories, key=lambda value: (-value.count("/"), value)):
        _remove_if_empty(install_root / directory)
    revocation = install_root / REVOKED_RECORD_NAME
    record = {
        "schema_version": INSTALL_SCHEMA_VERSION,
        "revoked_lock_digest": str(manifest.lock_digest),
        "packages": [
            {"name": package.name, "closure_digest": str(package.closure_digest)}
            for package in manifest.packages
        ],
        "removed": removed,
        "retained": [{"path": item.path, "reason": item.reason} for item in retained],
        "already_absent": absent,
    }
    _write_json_atomically(revocation, record)
    (install_root / OWNED_MANIFEST_NAME).unlink()
    return UninstallReport(
        lock_digest=manifest.lock_digest,
        install_root=install_root,
        revocation_record=revocation,
        removed=tuple(removed),
        retained=tuple(retained),
        already_absent=tuple(absent),
    )


def _parents(relative: str) -> list[str]:
    parts = relative.split("/")[:-1]
    return ["/".join(parts[: index + 1]) for index in range(len(parts))]


def _remove_if_empty(directory: Path) -> None:
    try:
        info = os.lstat(directory)
    except OSError:
        return
    if not stat.S_ISDIR(info.st_mode):
        return
    try:
        directory.rmdir()
    except OSError:
        return  # not empty: it still holds retained or foreign files


__all__ = [
    "INSTALL_SCHEMA_VERSION",
    "OWNED_MANIFEST_NAME",
    "REVOKED_RECORD_NAME",
    "BlockerKind",
    "InstallPlan",
    "InstallReport",
    "OwnedFile",
    "OwnedManifest",
    "OwnedPackage",
    "PlanBlocker",
    "PlannedApproval",
    "PlannedFile",
    "PlannedPackage",
    "RetainedPath",
    "UninstallReport",
    "build_install_plan",
    "install_lock",
    "load_owned_manifest",
    "uninstall_install",
]
