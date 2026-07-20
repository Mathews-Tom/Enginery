"""Optional Armory capability-registry adapter.

Armory (`Mathews-Tom/armory`) is an external, GitHub-hosted catalog: a
single ``manifest.yaml`` fetched over HTTPS, with no MCP server, no
per-package signature, and no checksum field of its own. Content
addressing here comes entirely from this adapter's own fetch-and-hash
step, never from anything Armory asserts.

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

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.delivery_ports import CapabilityDescriptor
from enginery.domain.digests import Digest
from enginery.domain.errors import MissingPrerequisiteError, TransientProviderFailureError

_DEFAULT_MANIFEST_URL = "https://raw.githubusercontent.com/Mathews-Tom/armory/main/manifest.yaml"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_PROVENANCE_LABEL = "armory"
_PACKAGE_SECTIONS = ("skills", "agents", "hooks", "rules", "commands", "utilities", "presets")


@dataclass(frozen=True, slots=True)
class ArmoryPackageEntry:
    """One catalog row from ``manifest.yaml``."""

    name: str
    version: str
    kind: str
    source: str


def _default_fetcher(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "enginery-armory-adapter"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return bytes(response.read())


def _raw_content_url(blob_url: str) -> str:
    """Turn a ``github.com/.../blob/<ref>/<path>`` URL into a raw-content URL."""

    marker = "github.com/"
    if marker not in blob_url or "/blob/" not in blob_url:
        raise MissingPrerequisiteError(
            "Armory manifest source is not a recognized GitHub blob URL",
            details={"source": blob_url},
        )
    rest = blob_url.split(marker, 1)[1]
    parts = rest.split("/", 3)
    if len(parts) != 4 or parts[2] != "blob":
        raise MissingPrerequisiteError(
            "Armory manifest source is not a recognized GitHub blob URL",
            details={"source": blob_url},
        )
    owner, repo, _blob, ref_and_path = parts
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref_and_path}"


def _license_url(manifest_url: str) -> str | None:
    suffix = "manifest.yaml"
    if not manifest_url.endswith(suffix):
        return None
    return manifest_url[: -len(suffix)] + "LICENSE"


def _detect_spdx_license(text: str) -> str:
    head = text[:400]
    if "MIT License" in head:
        return "MIT"
    if "Apache License" in head and "Version 2.0" in head:
        return "Apache-2.0"
    return "unknown"


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
            name, version, source = item.get("name"), item.get("version"), item.get("source")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(source, str) or not source.strip():
                continue
            version_text = str(version).strip() if version is not None else ""
            if not version_text:
                continue
            entries.append(
                ArmoryPackageEntry(name=name, version=version_text, kind=str(kind), source=source)
            )
    return tuple(entries)


class ArmoryCapabilitySource:
    """Discover, resolve, and fetch capabilities from the Armory catalog."""

    def __init__(
        self,
        *,
        manifest_url: str = _DEFAULT_MANIFEST_URL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        fetcher: Callable[[str, float], bytes] = _default_fetcher,
    ) -> None:
        self._manifest_url = manifest_url
        self._timeout_seconds = timeout_seconds
        self._fetcher = fetcher
        self._license_cache: str | None = None

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
        except (TransientProviderFailureError, MissingPrerequisiteError) as error:
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
                provider_version="1.0.0",
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability(name="discover", version=1),
                    AdapterCapability(name="resolve", version=1),
                    AdapterCapability(name="fetch", version=1),
                ),
            ),
            detail=f"armory manifest reachable at {self._manifest_url}",
        )

    def discover(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._descriptor(entry) for entry in self._entries())

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        entry = self._find(name, version)
        return None if entry is None else self._descriptor(entry)

    def fetch(self, name: str, version: str) -> bytes:
        entry = self._find(name, version)
        if entry is None:
            raise MissingPrerequisiteError(
                "capability was not found in the Armory manifest",
                details={"name": name, "version": version},
            )
        return self._fetch_bytes(_raw_content_url(entry.source))

    def _find(self, name: str, version: str) -> ArmoryPackageEntry | None:
        return next(
            (item for item in self._entries() if item.name == name and item.version == version),
            None,
        )

    def _descriptor(self, entry: ArmoryPackageEntry) -> CapabilityDescriptor:
        content = self._fetch_bytes(_raw_content_url(entry.source))
        return CapabilityDescriptor(
            name=entry.name,
            version=entry.version,
            digest=Digest.of_bytes(content),
            provenance=_PROVENANCE_LABEL,
            license=self._license(),
        )

    def _license(self) -> str:
        if self._license_cache is not None:
            return self._license_cache
        url = _license_url(self._manifest_url)
        if url is None:
            self._license_cache = "unknown"
            return self._license_cache
        try:
            text = self._fetch_bytes(url).decode("utf-8", errors="replace")
        except TransientProviderFailureError:
            self._license_cache = "unknown"
        else:
            self._license_cache = _detect_spdx_license(text)
        return self._license_cache

    def _fetch_bytes(self, url: str) -> bytes:
        try:
            return self._fetcher(url, self._timeout_seconds)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TransientProviderFailureError(
                "failed to fetch Armory content", details={"url": url}
            ) from error

    def _entries(self) -> tuple[ArmoryPackageEntry, ...]:
        import yaml

        raw = self._fetch_bytes(self._manifest_url)
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as error:
            raise MissingPrerequisiteError("Armory manifest is not valid YAML") from error
        return _parse_manifest(document)


__all__ = ["ArmoryCapabilitySource", "ArmoryPackageEntry"]
