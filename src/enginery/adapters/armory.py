"""Optional Armory capability-registry adapter.

Armory (`Mathews-Tom/armory`) is an external, GitHub-hosted catalog: a
``manifest.yaml`` listing package directories, with no MCP server, no
per-package signature, and no checksum, license, or dependency-pin field of
its own. Everything Enginery trusts about an Armory package therefore comes
from this adapter's own fetch-and-hash step, never from anything Armory
asserts.

Every read is bound to one immutable commit. The adapter resolves the
configured ref to a commit sha once (or takes an explicit ``revision``),
then reads the catalog manifest, the recursive file listing, every package
file, and the repository ``LICENSE`` at that sha, all through the injected
fetcher. A package's closure is the complete set of files under its catalog
``path`` -- a symlink, submodule, or other non-regular entry in the pinned
tree is rejected, and the closure builder rejects unsafe paths and
unreviewed asset types, so a ``.pyc``, database, or image shipped inside a
package makes that package unlockable rather than silently hashed.

Armory packages carry no Enginery manifest. For one that does not ship a
``capability.json`` the adapter synthesizes a minimal, deterministic one
(identity from the catalog row, kind ``instruction``, no permissions, no
dependencies, no license) and includes it in the closure; it grants no
authority and declares no command, so an Armory package is installed as
inert files. Armory content is unauthenticated: it is never executed on
discovery and never without exact-digest human approval. A package's
license is decided by the license files it ships at its root (a name whose
stem is ``license``, ``licence`` or ``copying``, in any case, with no
extension or ``.md``/``.txt``/``.rst``) when it carries any, else by the
repository ``LICENSE`` at the pinned revision. A license file is classified
only by exact match with a canonical MIT, Apache-2.0, BSD-2-Clause,
BSD-3-Clause or ISC text after case-folding, heading-marker removal,
copyright-line removal and whitespace collapsing; any other text (inserted
or appended terms, carve-outs, dual licensing, other licenses) is
``unknown``. Every shipped file must classify to one SPDX id that equals
the repository license, else the package license is ``unknown``. A
``NOTICE`` file never decides the license but makes it ``unknown`` when it
names another license family (GPL/LGPL/AGPL, MPL, SSPL, Creative Commons,
non-commercial, Commons Clause, proprietary, dual licensing, "at your
option") or reserves all rights without a permissive grant. The repository
``LICENSE`` and the catalog ``manifest.yaml`` are verified against the git
blob sha in the pinned tree, as is every package file (a mismatched
``LICENSE`` is ``unknown``; a mismatched manifest is an integrity failure).
All licenses are declared metadata only.

Armory is never a runtime dependency of the engine: importing this module
needs only the standard library, and every method that reaches the
network requires the optional ``armory`` extra (``pyyaml``) -- ``probe()``
reports ``AdapterAvailability.UNAVAILABLE`` when it is missing, exactly
like the OMP and Claude Code adapters report an absent CLI, so the engine
keeps working with Armory disabled.

Every capability this adapter returns reports ``provenance="armory"``,
which ``CapabilityResolver`` classifies as ``unauthenticated`` for any
run-introduced use unless a separate signature verifies -- Armory
supplies none today, so every Armory-sourced capability a run introduces
requires interactive exact-digest human approval before it can execute.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.delivery_ports import CapabilityDescriptor, PackageFile
from enginery.capabilities.closure import (
    MAX_FILE_BYTES,
    MAX_FILE_COUNT,
    MAX_TOTAL_BYTES,
    build_closure,
    is_license_like_name,
    is_notice_file_name,
    normalize_path,
)
from enginery.capabilities.errors import (
    CapabilityClosureLimitError,
    CapabilityIntegrityError,
    CapabilitySymlinkError,
)
from enginery.capabilities.license_text import (
    UNKNOWN_LICENSE,
    classify_license_text,
    notice_conflicts,
)
from enginery.capabilities.manifest import (
    MANIFEST_FILE_NAME,
    PackageKind,
    PackageManifest,
    render_manifest,
    validate_name,
    validate_version,
)
from enginery.domain.errors import (
    InvalidInputError,
    MissingPrerequisiteError,
    TransientProviderFailureError,
)

_DEFAULT_REPOSITORY = "Mathews-Tom/armory"
_DEFAULT_REF = "main"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_PROVENANCE_LABEL = "armory"
_PACKAGE_SECTIONS = ("skills", "agents", "hooks", "rules", "commands", "utilities", "presets")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REGULAR_MODES = {"100644": False, "100755": True}


@dataclass(frozen=True, slots=True)
class ArmoryPackageEntry:
    """One catalog row from ``manifest.yaml``."""

    name: str
    version: str
    kind: str
    path: str


_Bundle = tuple[CapabilityDescriptor, tuple[PackageFile, ...]]


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    mode: str
    kind: str
    size: int
    sha: str


def _default_fetcher(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "enginery-armory-adapter"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return bytes(response.read())


_CATALOG_MANIFEST = "manifest.yaml"


def _decode(item: PackageFile) -> str:
    return item.content.decode("utf-8", errors="replace")


def _git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def _parse_manifest(document: object) -> tuple[ArmoryPackageEntry, ...]:
    if not isinstance(document, dict):
        raise MissingPrerequisiteError("Armory manifest root must be a mapping")
    packages = document.get("packages")
    if not isinstance(packages, dict):
        raise MissingPrerequisiteError("Armory manifest is missing a 'packages' mapping")
    entries: list[ArmoryPackageEntry] = []
    for kind, items in packages.items():
        if kind not in _PACKAGE_SECTIONS or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name, version, path = item.get("name"), item.get("version"), item.get("path")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(path, str) or not path.strip():
                continue
            version_text = str(version).strip() if version is not None else ""
            if not version_text:
                continue
            entries.append(
                ArmoryPackageEntry(name=name, version=version_text, kind=str(kind), path=path)
            )
    return tuple(entries)


class ArmoryCapabilitySource:
    """Discover, resolve, and fetch package closures from the Armory catalog at one commit."""

    def __init__(
        self,
        *,
        repository: str = _DEFAULT_REPOSITORY,
        ref: str = _DEFAULT_REF,
        revision: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        fetcher: Callable[[str, float], bytes] = _default_fetcher,
    ) -> None:
        if not _REPOSITORY_PATTERN.fullmatch(repository):
            raise InvalidInputError(
                "Armory repository must be 'owner/name'", details={"repository": repository}
            )
        if not _REF_PATTERN.fullmatch(ref) or ".." in ref:
            raise InvalidInputError("Armory ref is not a plain git ref", details={"ref": ref})
        if revision is not None and not _SHA_PATTERN.fullmatch(revision):
            raise InvalidInputError(
                "Armory revision must be a full 40-character commit sha",
                details={"revision": revision},
            )
        self._repository = repository
        self._ref = ref
        self._revision = revision
        self._timeout_seconds = timeout_seconds
        self._fetcher = fetcher
        self._license_cache: str | None = None
        self._tree_cache: dict[str, _TreeEntry] | None = None
        self._package_cache: dict[tuple[str, str], _Bundle] = {}

    def probe(self) -> AdapterStatus:
        try:
            import yaml  # noqa: F401 -- see module docstring: optional `armory` extra
        except ImportError:
            return AdapterStatus(
                kind=ProviderKind.CAPABILITY_SOURCE,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="pyyaml is not installed; run `uv sync --extra armory` to enable Armory",
            )
        try:
            self._entries()
        except (
            TransientProviderFailureError,
            MissingPrerequisiteError,
            CapabilityIntegrityError,
        ) as error:
            return AdapterStatus(
                kind=ProviderKind.CAPABILITY_SOURCE,
                availability=AdapterAvailability.MISCONFIGURED,
                fingerprint=None,
                detail=str(error),
            )
        return AdapterStatus(
            kind=ProviderKind.CAPABILITY_SOURCE,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="armory-capability-source",
                provider_version="2.0.0",
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability(name="discover", version=1),
                    AdapterCapability(name="resolve", version=1),
                    AdapterCapability(name="fetch_closure", version=1),
                ),
            ),
            detail=f"armory catalog reachable at {self._repository}@{self._pinned_revision()}",
        )

    def discover(self) -> tuple[CapabilityDescriptor, ...]:
        """Every catalog row's descriptor; fails loudly on a package that cannot be locked."""

        return tuple(self._package(entry)[0] for entry in self._entries())

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        entry = self._find(name, version)
        return None if entry is None else self._package(entry)[0]

    def fetch_closure(self, name: str, version: str) -> tuple[PackageFile, ...]:
        entry = self._find(name, version)
        if entry is None:
            raise MissingPrerequisiteError(
                "capability was not found in the Armory manifest",
                details={"name": name, "version": version},
            )
        return self._package(entry)[1]

    def _find(self, name: str, version: str) -> ArmoryPackageEntry | None:
        return next(
            (item for item in self._entries() if item.name == name and item.version == version),
            None,
        )

    def _package(self, entry: ArmoryPackageEntry) -> _Bundle:
        key = (entry.name, entry.version)
        cached = self._package_cache.get(key)
        if cached is not None:
            return cached
        files = self._package_files(entry)
        closure = build_closure(files)
        descriptor = CapabilityDescriptor(
            name=entry.name,
            version=entry.version,
            digest=closure.digest,
            provenance=_PROVENANCE_LABEL,
            revision=self._pinned_revision(),
            license=self._package_license(files),
        )
        self._package_cache[key] = (descriptor, files)
        return descriptor, files

    def _package_files(self, entry: ArmoryPackageEntry) -> tuple[PackageFile, ...]:
        validate_name(entry.name)
        validate_version(entry.version)
        package_path = normalize_path(entry.path)
        prefix = f"{package_path}/"
        tree = self._tree()
        selected = [tree[path] for path in sorted(tree) if path.startswith(prefix)]
        if not selected:
            raise MissingPrerequisiteError(
                "Armory package path has no files at the pinned revision",
                details={"name": entry.name, "path": package_path},
            )
        if len(selected) > MAX_FILE_COUNT:
            raise CapabilityClosureLimitError(
                "package has too many files", details={"name": entry.name, "limit": MAX_FILE_COUNT}
            )
        total = 0
        for item in selected:
            if item.mode not in _REGULAR_MODES or item.kind != "blob":
                raise CapabilitySymlinkError(
                    "Armory package contains a symlink, submodule, or non-regular entry",
                    details={"name": entry.name, "path": item.path, "mode": item.mode},
                )
            if item.size > MAX_FILE_BYTES:
                raise CapabilityClosureLimitError(
                    "package file is too large",
                    details={"path": item.path, "limit": MAX_FILE_BYTES},
                )
            total += item.size
        if total > MAX_TOTAL_BYTES:
            raise CapabilityClosureLimitError(
                "package is too large", details={"name": entry.name, "limit": MAX_TOTAL_BYTES}
            )
        files: list[PackageFile] = []
        for item in selected:
            content = self._fetch_bytes(self._raw_url(item.path))
            if _git_blob_sha(content) != item.sha:
                raise CapabilityIntegrityError(
                    "fetched Armory file does not match the pinned tree",
                    details={"name": entry.name, "path": item.path},
                )
            files.append(
                PackageFile(
                    path=item.path[len(prefix) :],
                    content=content,
                    executable=_REGULAR_MODES[item.mode],
                )
            )
        if all(item.path != MANIFEST_FILE_NAME for item in files):
            files.append(
                PackageFile(
                    path=MANIFEST_FILE_NAME,
                    content=render_manifest(
                        PackageManifest(
                            name=entry.name, version=entry.version, kind=PackageKind.INSTRUCTION
                        )
                    ),
                )
            )
        return tuple(files)

    def _pinned_revision(self) -> str:
        if self._revision is not None:
            return self._revision
        document = self._fetch_json(
            f"https://api.github.com/repos/{self._repository}/commits/{self._ref}"
        )
        sha = document.get("sha") if isinstance(document, dict) else None
        if not isinstance(sha, str) or not _SHA_PATTERN.fullmatch(sha):
            raise MissingPrerequisiteError(
                "Armory ref did not resolve to a commit sha",
                details={"repository": self._repository, "ref": self._ref},
            )
        self._revision = sha
        return sha

    def _raw_url(self, path: str) -> str:
        revision = self._pinned_revision()
        return (
            f"https://raw.githubusercontent.com/{self._repository}/{revision}/"
            f"{quote(path, safe='/')}"
        )

    def _tree(self) -> dict[str, _TreeEntry]:
        if self._tree_cache is not None:
            return self._tree_cache
        document = self._fetch_json(
            f"https://api.github.com/repos/{self._repository}/git/trees/"
            f"{self._pinned_revision()}?recursive=1"
        )
        if not isinstance(document, dict):
            raise MissingPrerequisiteError("Armory file listing is not a JSON object")
        raw_tree = document.get("tree")
        if not isinstance(raw_tree, list) or document.get("truncated") is True:
            raise MissingPrerequisiteError(
                "Armory file listing is missing or truncated at the pinned revision",
                details={"repository": self._repository},
            )
        tree: dict[str, _TreeEntry] = {}
        for item in raw_tree:
            if not isinstance(item, dict) or item.get("type") == "tree":
                continue
            path, mode, kind = item.get("path"), item.get("mode"), item.get("type")
            size, sha = item.get("size", 0), item.get("sha")
            if not isinstance(path, str) or not isinstance(mode, str) or not isinstance(kind, str):
                raise MissingPrerequisiteError("Armory file listing has a malformed entry")
            if not isinstance(size, int) or isinstance(size, bool):
                raise MissingPrerequisiteError("Armory file listing has a malformed size")
            if not isinstance(sha, str) or not _SHA_PATTERN.fullmatch(sha):
                raise MissingPrerequisiteError("Armory file listing has a malformed blob sha")
            tree[path] = _TreeEntry(path=path, mode=mode, kind=kind, size=size, sha=sha)
        self._tree_cache = tree
        return tree

    def _package_license(self, files: Sequence[PackageFile]) -> str:
        """The package's own license file when it ships one, else the repository license.

        Every root license-like file decides the license: a canonical text
        equal to a template classifies it, any other text makes it ``unknown``.
        Canonical ``NOTICE`` files never decide it; they make it ``unknown``
        when they name another license family or reserve all rights.
        """

        shipped = [item for item in files if is_license_like_name(item.path)]
        repository = self._license()
        notices = [item for item in shipped if is_notice_file_name(item.path)]
        if any(notice_conflicts(_decode(item)) for item in notices):
            return UNKNOWN_LICENSE
        deciding = [item for item in shipped if item not in notices]
        if not deciding:
            return repository
        found = {classify_license_text(_decode(item)) for item in deciding}
        if len(found) != 1:
            return UNKNOWN_LICENSE
        (package_license,) = found
        return package_license if package_license == repository else UNKNOWN_LICENSE

    def _license(self) -> str:
        """The repository license, trusted only when it matches its pinned tree blob."""

        if self._license_cache is not None:
            return self._license_cache
        entry = self._tree().get("LICENSE")
        try:
            content = self._fetch_bytes(self._raw_url("LICENSE"))
        except TransientProviderFailureError:
            self._license_cache = UNKNOWN_LICENSE
        else:
            if entry is None or _git_blob_sha(content) != entry.sha:
                self._license_cache = UNKNOWN_LICENSE
            else:
                text = content.decode("utf-8", errors="replace")
                self._license_cache = classify_license_text(text)
        return self._license_cache

    def _fetch_bytes(self, url: str) -> bytes:
        try:
            return self._fetcher(url, self._timeout_seconds)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TransientProviderFailureError(
                "failed to fetch Armory content", details={"url": url}
            ) from error

    def _fetch_json(self, url: str) -> object:
        raw = self._fetch_bytes(url)
        try:
            return json.loads(raw)
        except ValueError as error:
            raise MissingPrerequisiteError(
                "Armory response is not valid JSON", details={"url": url}
            ) from error

    def _entries(self) -> tuple[ArmoryPackageEntry, ...]:
        import yaml

        raw = self._fetch_bytes(self._raw_url(_CATALOG_MANIFEST))
        entry = self._tree().get(_CATALOG_MANIFEST)
        if entry is None:
            raise MissingPrerequisiteError(
                "Armory catalog manifest is not in the pinned tree",
                details={"path": _CATALOG_MANIFEST},
            )
        if _git_blob_sha(raw) != entry.sha:
            raise CapabilityIntegrityError(
                "fetched Armory catalog manifest does not match the pinned tree",
                details={"path": _CATALOG_MANIFEST},
            )
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise MissingPrerequisiteError("Armory manifest is not valid YAML") from error
        return _parse_manifest(document)


__all__ = ["ArmoryCapabilitySource", "ArmoryPackageEntry"]
