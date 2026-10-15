"""Directory-backed capability source: packages laid out as subdirectories of one root.

``root/<name>/`` holds one package: a ``capability.json`` manifest plus its
instruction, reference, script, and data files. The same adapter serves two
trust classes, chosen by the :class:`~enginery.capabilities.sources.SourceKind`
it is registered under: the reviewed project (packages committed in the
repository base revision) and a user-local directory. The adapter itself
grants no trust; it only reads files.

Reading never follows a link: the package directory and every file inside are
opened without following symlinks, and the closure builder rejects symlinks,
hardlinks, special files, traversal, and unreviewed asset types. A directory
under the root without a ``capability.json`` is not a package and is not
listed; a directory whose manifest is present but invalid fails loudly.

A directory has no immutable revision of its own, so the descriptor's
revision is the closure digest itself: it changes exactly when any file does.
``signatures`` is the only way to attach a signature claim; the resolver
treats it as unverified until a pinned key verifies it.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.delivery_ports import (
    CapabilityDescriptor,
    CapabilityListing,
    PackageFile,
)
from enginery.capabilities.closure import (
    MAX_FILE_BYTES,
    build_closure,
    read_package_directory,
)
from enginery.capabilities.errors import (
    CapabilityManifestError,
    CapabilityPathError,
    CapabilitySourceError,
    CapabilitySymlinkError,
)
from enginery.capabilities.manifest import (
    MANIFEST_FILE_NAME,
    PackageManifest,
    parse_manifest,
    validate_name,
)

_DEFAULT_PROVENANCE = "directory"
_LISTING_REVISION = "content-addressed"


class DirectoryCapabilitySource:
    """Packages read from the subdirectories of one root directory."""

    def __init__(
        self,
        root: Path,
        *,
        provenance: str = _DEFAULT_PROVENANCE,
        signatures: Mapping[tuple[str, str], tuple[bytes, str]] | None = None,
    ) -> None:
        if not provenance.strip():
            raise ValueError("directory capability provenance must be non-blank")
        self._root = root
        self._provenance = provenance
        self._signatures = dict(signatures or {})

    def probe(self) -> AdapterStatus:
        try:
            info = os.lstat(self._root)
        except OSError as error:
            return self._status(AdapterAvailability.MISCONFIGURED, f"{self._root}: {error}")
        if not stat.S_ISDIR(info.st_mode):
            return self._status(
                AdapterAvailability.MISCONFIGURED, f"{self._root} is not a real directory"
            )
        return self._status(AdapterAvailability.AVAILABLE, f"directory source at {self._root}")

    def discover(self) -> tuple[CapabilityListing, ...]:
        """Every package directory with a manifest; no closure is built."""

        try:
            with os.scandir(self._root) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError as error:
            raise CapabilitySourceError(
                "capability directory cannot be listed",
                details={"path": str(self._root), "error": str(error)},
            ) from error
        listings: list[CapabilityListing] = []
        for name in names:
            package_directory = self._root / name
            if not _is_directory_entry(package_directory):
                continue
            manifest = self._manifest(name)
            if manifest is None:
                continue
            listings.append(
                CapabilityListing(
                    name=manifest.name,
                    version=manifest.version,
                    provenance=self._provenance,
                    revision=_LISTING_REVISION,
                )
            )
        return tuple(listings)

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        validate_name(name)
        manifest = self._manifest(name)
        if manifest is None or manifest.version != version:
            return None
        closure = build_closure(read_package_directory(self._root / name))
        signature = self._signatures.get((name, version))
        return CapabilityDescriptor(
            name=name,
            version=version,
            digest=closure.digest,
            provenance=self._provenance,
            revision=str(closure.digest),
            license=None,
            signature=None if signature is None else signature[0],
            signer_key_id=None if signature is None else signature[1],
        )

    def fetch_closure(self, name: str, version: str) -> tuple[PackageFile, ...]:
        validate_name(name)
        manifest = self._manifest(name)
        if manifest is None or manifest.version != version:
            raise CapabilitySourceError(
                "capability is not present in the directory source",
                details={"name": name, "version": version, "root": str(self._root)},
            )
        return read_package_directory(self._root / name)

    def _manifest(self, name: str) -> PackageManifest | None:
        """The package's manifest, or ``None`` when ``name`` is not a package directory."""

        directory = self._root / name
        if not os.path.lexists(directory):
            return None
        if stat.S_ISLNK(os.lstat(directory).st_mode):
            raise CapabilitySymlinkError(
                "package directory is a symlink", details={"path": str(directory)}
            )
        path = directory / MANIFEST_FILE_NAME
        if not os.path.lexists(path):
            return None
        manifest = parse_manifest(_read_manifest_bytes(path))
        if manifest.name != name:
            raise CapabilityManifestError(
                "package manifest name does not match its directory",
                details={"path": str(path), "manifest_name": manifest.name},
            )
        return manifest

    def _status(self, availability: AdapterAvailability, detail: str) -> AdapterStatus:
        available = availability is AdapterAvailability.AVAILABLE
        return AdapterStatus(
            kind=ProviderKind.CAPABILITY_SOURCE,
            availability=availability,
            fingerprint=(
                AdapterFingerprint(
                    provider_id="directory-capability-source",
                    provider_version="1.0.0",
                    api_version=ADAPTER_API_VERSION,
                    capabilities=(
                        AdapterCapability(name="discover", version=1),
                        AdapterCapability(name="resolve", version=1),
                        AdapterCapability(name="fetch_closure", version=1),
                    ),
                )
                if available
                else None
            ),
            detail=detail,
        )


def _is_directory_entry(path: Path) -> bool:
    """Whether ``path`` is a directory or a symlink (a symlink is reported, not skipped)."""

    mode = os.lstat(path).st_mode
    return stat.S_ISDIR(mode) or stat.S_ISLNK(mode)


def _read_manifest_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CapabilitySymlinkError(
            "package manifest is a symlink or unreadable", details={"path": str(path)}
        ) from error
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise CapabilitySymlinkError(
                "package manifest is not a regular file", details={"path": str(path)}
            )
        content = handle.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise CapabilityPathError("package manifest is too large", details={"path": str(path)})
    return content


__all__ = ["DirectoryCapabilitySource"]
