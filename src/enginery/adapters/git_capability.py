"""Git-backed capability source: a repository pinned to one immutable commit.

The source is configured with a repository (a local path, or a URL fetched
into a private bare cache) and a full 40-hex commit sha. Everything it
reports is read from that commit's *tracked tree* with git plumbing
(``ls-tree -r -z -l`` and ``cat-file blob``); the working tree, the index and
untracked files are never consulted, so local junk such as ``__pycache__``
cannot enter a closure. Nothing it reads is ever executed or imported.

A package is one directory ``<package_root>/<name>/`` of the tree holding a
``capability.json`` manifest. Tree entries with mode ``120000`` (symlink) or
``160000`` (submodule), and any other non-regular mode, make the package
unlockable and fail loudly with the offending path; the closure builder then
rejects unsafe paths and unreviewed asset types the same way it does for every
other source. The configured sha must resolve to exactly that commit; an
abbreviated sha, a ref name, or an unknown commit is refused.

Every git subprocess uses an argument vector (never a shell), a timeout, and an
environment built from an allowlist (``PATH``, ``HOME``, ``LANG``/``LC_*``,
``SSH_AUTH_SOCK``) plus no system or global configuration, no replacement
objects (``--no-replace-objects`` and ``GIT_NO_REPLACE_OBJECTS``, so ``git
replace`` cannot change what a pinned commit contains), ``core.fsmonitor``
off, and no terminal prompt. The adapter never writes git configuration and
never touches a local repository it was pointed at; a URL is fetched only into
the adapter's own cache directory. A package whose tree sizes already exceed
the package-wide byte limit is refused before any blob is read.

A Git source's packages are unauthenticated unless a pinned signature over the
closure digest verifies; ``signatures`` is the only way to supply one. The
reviewed project source is also a Git source: pinned to the run's base commit
and registered with the ``project`` kind (see ``pinned_commit``).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    MAX_FILE_COUNT,
    MAX_TOTAL_BYTES,
    build_closure,
    normalize_path,
)
from enginery.capabilities.errors import (
    CapabilityClosureLimitError,
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
from enginery.domain.errors import InvalidInputError

SYMLINK_MODE = "120000"
SUBMODULE_MODE = "160000"
_REGULAR_MODES = {"100644": False, "100755": True}
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_URL_PATTERN = re.compile(r"^(https://|ssh://|file://|git@[A-Za-z0-9._-]+:)[^\s]+$")
_DEFAULT_TIMEOUT_SECONDS = 30.0
_PROVENANCE_LABEL = "git"
_ALLOWED_PROTOCOLS = "file:https:ssh"
_INHERITED_VARIABLES = frozenset({"PATH", "HOME", "LANG", "SSH_AUTH_SOCK"})


def git_environment(*, ceiling: Path | None = None) -> dict[str, str]:
    """A scrubbed environment for one git invocation.

    Built from an allowlist of the caller's environment (``PATH``, ``HOME``,
    ``LANG``/``LC_*`` and ``SSH_AUTH_SOCK``), so no proxy, askpass, ssh-command,
    exec-path, token or repository-redirecting variable is inherited. System
    and global configuration are ignored, replacement objects are ignored,
    prompting and unreviewed transports are forbidden, and (optionally) git is
    stopped from discovering a repository above ``ceiling``.
    """

    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _INHERITED_VARIABLES or key.startswith("LC_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ALLOW_PROTOCOL": _ALLOWED_PROTOCOLS,
            "LC_ALL": "C",
        }
    )
    if ceiling is not None:
        environment["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    return environment


@dataclass(frozen=True, slots=True)
class _TreeItem:
    path: str
    mode: str
    kind: str
    object_id: str
    size: int


class GitCapabilitySource:
    """Packages read from one immutable commit of a Git repository."""

    def __init__(
        self,
        *,
        repository: str,
        revision: str,
        package_root: str = "",
        cache_directory: Path | None = None,
        signatures: Mapping[tuple[str, str], tuple[bytes, str]] | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not _SHA_PATTERN.fullmatch(revision):
            raise InvalidInputError(
                "a Git capability source needs a full 40-character lowercase commit sha",
                details={"revision": revision},
            )
        if timeout_seconds <= 0:
            raise InvalidInputError("git timeout_seconds must be positive")
        local = Path(repository).expanduser()
        self._local: Path | None = None
        if local.is_dir():
            self._local = local.resolve()
        elif not _URL_PATTERN.fullmatch(repository):
            raise InvalidInputError(
                "a Git capability repository must be an existing directory or an "
                "https://, ssh://, file:// or git@host: URL",
                details={"repository": repository},
            )
        elif cache_directory is None:
            raise InvalidInputError("a URL Git capability source needs a cache_directory")
        self._repository = repository
        self._revision = revision
        self._package_root = normalize_path(package_root) if package_root else ""
        self._cache_directory = cache_directory
        self._signatures = dict(signatures or {})
        self._timeout_seconds = timeout_seconds
        self._verified_repository: Path | None = None

    @property
    def pinned_commit(self) -> str:
        """The full commit sha every read is pinned to."""

        return self._revision

    @property
    def repository(self) -> str:
        return self._repository

    # ------------------------------------------------------------------ port

    def probe(self) -> AdapterStatus:
        if shutil.which("git") is None:
            return AdapterStatus(
                kind=ProviderKind.CAPABILITY_SOURCE,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="git is not installed",
            )
        try:
            self._repo()
        except CapabilitySourceError as error:
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
                provider_id="git-capability-source",
                provider_version="1.0.0",
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability(name="discover", version=1),
                    AdapterCapability(name="resolve", version=1),
                    AdapterCapability(name="fetch_closure", version=1),
                ),
            ),
            detail=f"commit {self._revision} resolves in {self._repository}",
        )

    def discover(self) -> tuple[CapabilityListing, ...]:
        """Every package directory with a manifest at the pinned commit; no closure is built."""

        listings: list[CapabilityListing] = []
        for name, items in sorted(self._packages().items()):
            manifest = self._manifest_of(name, items)
            if manifest is None:
                continue
            listings.append(
                CapabilityListing(
                    name=manifest.name,
                    version=manifest.version,
                    provenance=_PROVENANCE_LABEL,
                    revision=self._revision,
                )
            )
        return tuple(listings)

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        validate_name(name)
        items = self._package_items(name)
        if not items:
            return None
        manifest = self._manifest_of(name, items)
        if manifest is None or (manifest.name, manifest.version) != (name, version):
            return None
        closure = build_closure(self._files_of(name, items))
        signature = self._signatures.get((name, version))
        return CapabilityDescriptor(
            name=name,
            version=version,
            digest=closure.digest,
            provenance=_PROVENANCE_LABEL,
            revision=self._revision,
            license=None,
            signature=None if signature is None else signature[0],
            signer_key_id=None if signature is None else signature[1],
        )

    def fetch_closure(self, name: str, version: str) -> tuple[PackageFile, ...]:
        validate_name(name)
        items = self._package_items(name)
        manifest = self._manifest_of(name, items) if items else None
        if manifest is None or (manifest.name, manifest.version) != (name, version):
            raise CapabilitySourceError(
                "capability is not present in the Git source at the pinned commit",
                details={"name": name, "version": version, "revision": self._revision},
            )
        return self._files_of(name, items)

    # ------------------------------------------------------------ git plumbing

    def _prefix(self) -> str:
        return f"{self._package_root}/" if self._package_root else ""

    def _packages(self) -> dict[str, list[_TreeItem]]:
        """Tree items under the package root, grouped by package directory name."""

        prefix = self._prefix()
        arguments = ["ls-tree", "-r", "-z", "-l", "--full-tree", self._revision]
        if self._package_root:
            arguments += ["--", self._package_root]
        grouped: dict[str, list[_TreeItem]] = {}
        for item in self._parse_listing(self._git(arguments)):
            if not item.path.startswith(prefix):
                continue
            name, separator, _ = item.path[len(prefix) :].partition("/")
            if separator:
                grouped.setdefault(name, []).append(item)
        return grouped

    def _package_items(self, name: str) -> list[_TreeItem]:
        prefix = f"{self._prefix()}{name}"
        listing = self._parse_listing(
            self._git(["ls-tree", "-r", "-z", "-l", "--full-tree", self._revision, "--", prefix])
        )
        items: list[_TreeItem] = []
        for item in listing:
            if item.path == prefix:
                raise CapabilitySymlinkError(
                    "a package path is a file, symlink, or submodule instead of a directory",
                    details={"path": item.path, "mode": item.mode},
                )
            if item.path.startswith(f"{prefix}/"):
                items.append(item)
        return items

    def _manifest_of(self, name: str, items: Sequence[_TreeItem]) -> PackageManifest | None:
        """The package's manifest, or ``None`` when the directory is not a package."""

        path = f"{self._prefix()}{name}/{MANIFEST_FILE_NAME}"
        found = next((item for item in items if item.path == path), None)
        if found is None:
            return None
        self._require_regular(found)
        manifest = parse_manifest(self._blob(found))
        if manifest.name != name:
            raise CapabilityManifestError(
                "package manifest name does not match its directory",
                details={"path": path, "manifest_name": manifest.name},
            )
        return manifest

    def _files_of(self, name: str, items: Sequence[_TreeItem]) -> tuple[PackageFile, ...]:
        prefix = f"{self._prefix()}{name}/"
        if len(items) > MAX_FILE_COUNT:
            raise CapabilityClosureLimitError(
                "package has too many files", details={"name": name, "limit": MAX_FILE_COUNT}
            )
        total = 0
        for item in items:
            self._require_regular(item)
            total += item.size
            if total > MAX_TOTAL_BYTES:
                raise CapabilityClosureLimitError(
                    "package is too large", details={"name": name, "limit": MAX_TOTAL_BYTES}
                )
        files: list[PackageFile] = []
        for item in sorted(items, key=lambda entry: entry.path):
            files.append(
                PackageFile(
                    path=item.path[len(prefix) :],
                    content=self._blob(item),
                    executable=_REGULAR_MODES[item.mode],
                )
            )
        return tuple(files)

    def _require_regular(self, item: _TreeItem) -> None:
        if item.mode == SYMLINK_MODE:
            what = "a symlink"
        elif item.mode == SUBMODULE_MODE or item.kind == "commit":
            what = "a submodule"
        elif item.mode not in _REGULAR_MODES or item.kind != "blob":
            what = "a non-regular entry"
        else:
            if item.size > MAX_FILE_BYTES:
                raise CapabilityClosureLimitError(
                    "package file is too large",
                    details={"path": item.path, "limit": MAX_FILE_BYTES},
                )
            return
        raise CapabilitySymlinkError(
            f"package contains {what} in the pinned tree",
            details={"path": item.path, "mode": item.mode},
        )

    def _blob(self, item: _TreeItem) -> bytes:
        return self._git(["cat-file", "blob", item.object_id])

    @staticmethod
    def _parse_listing(raw: bytes) -> list[_TreeItem]:
        items: list[_TreeItem] = []
        for record in raw.split(b"\x00"):
            if not record:
                continue
            meta, separator, path_bytes = record.partition(b"\t")
            fields = meta.decode("ascii", errors="replace").split()
            if not separator or len(fields) != 4:
                raise CapabilitySourceError("git returned a malformed tree listing")
            try:
                path = path_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise CapabilityPathError(
                    "a tracked path is not valid UTF-8", details={"path": repr(path_bytes)}
                ) from error
            mode, kind, object_id, size_text = fields
            size = int(size_text) if size_text.isdigit() else 0
            items.append(_TreeItem(path=path, mode=mode, kind=kind, object_id=object_id, size=size))
        return items

    def _git(self, arguments: Sequence[str]) -> bytes:
        completed = self._run(self._repo(), arguments)
        if completed.returncode != 0:
            raise CapabilitySourceError(
                "git could not read the pinned commit",
                details={
                    "command": arguments[0],
                    "revision": self._revision,
                    "stderr": completed.stderr.decode("utf-8", errors="replace")[:1000],
                },
            )
        return completed.stdout

    def _run(
        self, repository: Path, arguments: Sequence[str]
    ) -> subprocess.CompletedProcess[bytes]:
        executable = shutil.which("git")
        if executable is None:
            raise CapabilitySourceError("git is not installed")
        try:
            return subprocess.run(
                [
                    executable,
                    "--no-replace-objects",
                    "-c",
                    "core.fsmonitor=false",
                    "-C",
                    str(repository),
                    *arguments,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self._timeout_seconds,
                env=git_environment(ceiling=repository.parent),
            )
        except subprocess.TimeoutExpired as error:
            raise CapabilitySourceError(
                "git timed out", details={"command": arguments[0]}
            ) from error
        except OSError as error:
            raise CapabilitySourceError(
                "git could not be started", details={"error": str(error)}
            ) from error

    def _repo(self) -> Path:
        """The repository holding the commit, verified once to resolve to exactly the sha."""

        if self._verified_repository is not None:
            return self._verified_repository
        repository = self._local if self._local is not None else self._mirror()
        completed = self._run(
            repository, ["rev-parse", "--verify", "--quiet", f"{self._revision}^{{commit}}"]
        )
        resolved = completed.stdout.decode("ascii", errors="replace").strip()
        if completed.returncode != 0 or resolved != self._revision:
            raise CapabilitySourceError(
                "the pinned sha does not resolve to exactly that commit",
                details={"repository": self._repository, "revision": self._revision},
            )
        self._verified_repository = repository
        return repository

    def _mirror(self) -> Path:
        """A bare cache of a URL repository, fetched only when the commit is not yet present."""

        assert self._cache_directory is not None
        key = hashlib.sha256(self._repository.encode("utf-8")).hexdigest()[:16]
        mirror = self._cache_directory / key
        if not (mirror / "HEAD").exists():
            mirror.mkdir(parents=True, exist_ok=True)
            self._must_run(mirror, ["init", "--bare", "--quiet", str(mirror)])
        present = self._run(mirror, ["cat-file", "-e", f"{self._revision}^{{commit}}"])
        if present.returncode != 0:
            self._must_run(
                mirror,
                [
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--",
                    self._repository,
                    "+refs/heads/*:refs/mirror/heads/*",
                    "+refs/tags/*:refs/mirror/tags/*",
                ],
            )
        return mirror

    def _must_run(self, repository: Path, arguments: Sequence[str]) -> None:
        completed = self._run(repository, arguments)
        if completed.returncode != 0:
            raise CapabilitySourceError(
                "git could not prepare the repository cache",
                details={
                    "command": arguments[0],
                    "stderr": completed.stderr.decode("utf-8", errors="replace")[:1000],
                },
            )


__all__ = ["SUBMODULE_MODE", "SYMLINK_MODE", "GitCapabilitySource", "git_environment"]
