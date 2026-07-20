from __future__ import annotations

import builtins
from collections.abc import Callable

import pytest
import yaml

from enginery.adapters.armory import ArmoryCapabilitySource
from enginery.application.adapter_types import AdapterAvailability
from enginery.capabilities.lock import ProvenanceStatus
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.domain.digests import Digest
from enginery.domain.errors import MissingPrerequisiteError, TransientProviderFailureError

_MANIFEST_URL = "https://raw.githubusercontent.com/Mathews-Tom/armory/main/manifest.yaml"
_LICENSE_URL = "https://raw.githubusercontent.com/Mathews-Tom/armory/main/LICENSE"
_SKILL_SOURCE = "https://github.com/Mathews-Tom/armory/blob/main/skills/adr-writer/SKILL.md"
_SKILL_RAW_URL = (
    "https://raw.githubusercontent.com/Mathews-Tom/armory/main/skills/adr-writer/SKILL.md"
)
_SKILL_CONTENT = b"# ADR writer skill body"
_LICENSE_TEXT = b"MIT License\n\nCopyright (c) 2026 Tom\n"


def _manifest_yaml() -> bytes:
    document = {
        "packages": {
            "skills": [
                {
                    "name": "adr-writer",
                    "version": "1.1.1",
                    "path": "skills/adr-writer",
                    "source": _SKILL_SOURCE,
                },
                {"name": "missing-source", "version": "1.0.0"},
            ]
        }
    }
    return yaml.safe_dump(document).encode("utf-8")


def _fetcher(routes: dict[str, bytes]) -> Callable[[str, float], bytes]:
    def fetch(url: str, timeout: float) -> bytes:
        del timeout
        if url not in routes:
            raise TimeoutError(f"no fixture route for {url}")
        return routes[url]

    return fetch


def _source(routes: dict[str, bytes] | None = None) -> ArmoryCapabilitySource:
    all_routes = {
        _MANIFEST_URL: _manifest_yaml(),
        _SKILL_RAW_URL: _SKILL_CONTENT,
        _LICENSE_URL: _LICENSE_TEXT,
    }
    if routes:
        all_routes.update(routes)
    return ArmoryCapabilitySource(manifest_url=_MANIFEST_URL, fetcher=_fetcher(all_routes))


def test_probe_reports_available_when_manifest_is_reachable() -> None:
    status = _source().probe()

    assert status.availability is AdapterAvailability.AVAILABLE
    assert status.fingerprint is not None


def test_probe_reports_unavailable_without_pyyaml(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "yaml":
            raise ImportError("no module named yaml")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    status = _source().probe()

    assert status.availability is AdapterAvailability.UNAVAILABLE
    assert "armory" in status.detail.lower()


def test_probe_reports_misconfigured_for_unreachable_manifest() -> None:
    source = ArmoryCapabilitySource(manifest_url=_MANIFEST_URL, fetcher=_fetcher({}))

    status = source.probe()

    assert status.availability is AdapterAvailability.MISCONFIGURED


def test_resolve_returns_a_content_addressed_descriptor() -> None:
    descriptor = _source().resolve("adr-writer", "1.1.1")

    assert descriptor is not None
    assert descriptor.digest == Digest.of_bytes(_SKILL_CONTENT)
    assert descriptor.provenance == "armory"
    assert descriptor.license == "MIT"


def test_resolve_skips_entries_missing_a_source_url() -> None:
    assert _source().resolve("missing-source", "1.0.0") is None


def test_resolve_returns_none_for_unknown_capability() -> None:
    assert _source().resolve("does-not-exist", "1.0.0") is None


def test_fetch_returns_the_exact_bytes_backing_the_descriptor() -> None:
    source = _source()
    descriptor = source.resolve("adr-writer", "1.1.1")
    assert descriptor is not None

    content = source.fetch("adr-writer", "1.1.1")

    assert Digest.of_bytes(content) == descriptor.digest


def test_fetch_raises_for_unknown_capability() -> None:
    with pytest.raises(MissingPrerequisiteError):
        _source().fetch("does-not-exist", "1.0.0")


def test_discover_returns_every_resolvable_entry() -> None:
    descriptors = _source().discover()

    assert [d.name for d in descriptors] == ["adr-writer"]


def test_non_github_blob_source_is_rejected() -> None:
    routes = {
        _MANIFEST_URL: yaml.safe_dump(
            {
                "packages": {
                    "skills": [
                        {
                            "name": "bad-source",
                            "version": "1.0.0",
                            "source": "https://example.com/not-a-blob-url",
                        }
                    ]
                }
            }
        ).encode("utf-8")
    }
    source = _source(routes)

    with pytest.raises(MissingPrerequisiteError):
        source.resolve("bad-source", "1.0.0")


def test_unreachable_content_raises_transient_failure() -> None:
    source = ArmoryCapabilitySource(
        manifest_url=_MANIFEST_URL, fetcher=_fetcher({_MANIFEST_URL: _manifest_yaml()})
    )

    with pytest.raises(TransientProviderFailureError):
        source.resolve("adr-writer", "1.1.1")


def test_armory_sourced_capability_is_always_unauthenticated_when_run_introduced() -> None:
    """No per-package signature exists today; every Armory capability a run
    introduces must stay unauthenticated regardless of a clean digest fetch."""

    resolver = CapabilityResolver([_source()])

    lock = resolver.resolve([CapabilityRequest(name="adr-writer", version="1.1.1")])

    entry = lock.get("adr-writer")
    assert entry is not None
    assert entry.introduced_by_run is True
    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.requires_human_approval() is True


def test_engine_resolves_capabilities_without_armory_installed() -> None:
    """The reviewed-base, local-only resolution path must not require Armory at all."""

    from enginery.adapters.local import LocalCapabilitySource
    from enginery.application.delivery_ports import CapabilityDescriptor

    descriptor = CapabilityDescriptor(
        name="repository-read", version="1", digest=Digest.of_bytes(b"content"), provenance="local"
    )
    local_source = LocalCapabilitySource(
        capabilities=(descriptor,), content_by_key={("repository-read", "1"): b"content"}
    )
    resolver = CapabilityResolver(
        [local_source], reviewed_base=frozenset({("repository-read", "1")})
    )

    lock = resolver.resolve([CapabilityRequest(name="repository-read", version="1")])

    entry = lock.get("repository-read")
    assert entry is not None
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.requires_human_approval() is False
