"""The directory capability source reads real package directories without following links."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from tests.support.capabilities import HOST, package_files, replace_file

from enginery.adapters.directory_capability import DirectoryCapabilitySource
from enginery.application.adapter_types import AdapterAvailability
from enginery.application.delivery_ports import PackageFile
from enginery.capabilities.closure import build_closure
from enginery.capabilities.errors import (
    CapabilityAssetTypeError,
    CapabilityManifestError,
    CapabilitySourceError,
    CapabilitySymlinkError,
)
from enginery.capabilities.lock import ProvenanceStatus
from enginery.capabilities.manifest import CommandSpec, PackageKind
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.domain.errors import InvalidInputError


def _write_package(root: Path, name: str, files: Sequence[PackageFile]) -> Path:
    directory = root / name
    for item in files:
        target = directory / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)
        target.chmod(0o755 if item.executable else 0o644)
    return directory


def _as_set(files: Sequence[PackageFile]) -> set[tuple[str, bytes, bool]]:
    return {(item.path, item.content, item.executable) for item in files}


def _resolver(kind: SourceKind, root: Path) -> CapabilityResolver:
    port = DirectoryCapabilitySource(root)
    return CapabilityResolver([CapabilitySource(kind, kind.value, port)], host=HOST)


def test_discover_lists_manifest_packages_and_skips_plain_directories(tmp_path: Path) -> None:
    _write_package(tmp_path, "skill-b", package_files("skill-b", "2"))
    _write_package(tmp_path, "skill-a", package_files("skill-a", "1"))
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "readme.txt").write_bytes(b"not a package")
    (tmp_path / "stray-file").write_bytes(b"ignored")

    listings = DirectoryCapabilitySource(tmp_path, provenance="team-dir").discover()

    assert [(item.name, item.version, item.provenance) for item in listings] == [
        ("skill-a", "1", "team-dir"),
        ("skill-b", "2", "team-dir"),
    ]


def test_discover_rejects_an_invalid_manifest(tmp_path: Path) -> None:
    _write_package(tmp_path, "skill-a", package_files("skill-a"))
    (tmp_path / "skill-a" / "capability.json").write_bytes(b"not json")

    with pytest.raises(CapabilityManifestError):
        DirectoryCapabilitySource(tmp_path).discover()


def test_discover_rejects_a_manifest_named_differently_from_its_directory(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, "skill-a", package_files("other-name"))

    with pytest.raises(CapabilityManifestError) as caught:
        DirectoryCapabilitySource(tmp_path).discover()

    assert caught.value.details["manifest_name"] == "other-name"


def test_discover_builds_no_closure_so_resolve_is_where_asset_types_are_refused(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, "skill-a", package_files("skill-a", extra={"payload.bin": b"\x00"}))
    source = DirectoryCapabilitySource(tmp_path)

    assert [item.name for item in source.discover()] == ["skill-a"]
    with pytest.raises(CapabilityAssetTypeError):
        source.resolve("skill-a", "1")


def test_resolve_describes_the_closure_digest_and_uses_it_as_the_revision(
    tmp_path: Path,
) -> None:
    files = package_files("skill-a")
    _write_package(tmp_path, "skill-a", files)

    descriptor = DirectoryCapabilitySource(tmp_path, provenance="team-dir").resolve("skill-a", "1")

    assert descriptor is not None
    assert descriptor.digest == build_closure(files).digest
    assert descriptor.revision == str(descriptor.digest)
    assert descriptor.provenance == "team-dir"
    assert descriptor.signature is None
    assert descriptor.signer_key_id is None


def test_resolve_returns_none_for_a_version_mismatch_or_unknown_name(tmp_path: Path) -> None:
    _write_package(tmp_path, "skill-a", package_files("skill-a", "1"))
    source = DirectoryCapabilitySource(tmp_path)

    assert source.resolve("skill-a", "2") is None
    assert source.resolve("ghost", "1") is None
    with pytest.raises(CapabilitySourceError):
        source.fetch_closure("skill-a", "2")
    with pytest.raises(CapabilitySourceError):
        source.fetch_closure("ghost", "1")


@pytest.mark.parametrize("name", ["../x", "a/b", "..", "", "/abs"])
def test_names_that_could_traverse_are_rejected(tmp_path: Path, name: str) -> None:
    source = DirectoryCapabilitySource(tmp_path / "root")

    with pytest.raises(InvalidInputError):
        source.resolve(name, "1")
    with pytest.raises(InvalidInputError):
        source.fetch_closure(name, "1")


def test_a_symlinked_package_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write_package(tmp_path / "elsewhere", "real", package_files("skill-a"))
    os.symlink(tmp_path / "elsewhere" / "real", root / "skill-a")
    source = DirectoryCapabilitySource(root)

    with pytest.raises(CapabilitySymlinkError):
        source.discover()
    with pytest.raises(CapabilitySymlinkError):
        source.resolve("skill-a", "1")
    with pytest.raises(CapabilitySymlinkError):
        source.fetch_closure("skill-a", "1")


def test_a_symlinked_file_inside_a_package_is_refused(tmp_path: Path) -> None:
    package = _write_package(tmp_path, "skill-a", package_files("skill-a"))
    os.symlink(package / "SKILL.md", package / "references" / "link.md")
    source = DirectoryCapabilitySource(tmp_path)

    with pytest.raises(CapabilitySymlinkError):
        source.resolve("skill-a", "1")
    with pytest.raises(CapabilitySymlinkError):
        source.fetch_closure("skill-a", "1")


def test_a_symlinked_manifest_is_refused(tmp_path: Path) -> None:
    package = _write_package(tmp_path, "skill-a", package_files("skill-a"))
    real = tmp_path / "real-manifest.json"
    (package / "capability.json").rename(real)
    os.symlink(real, package / "capability.json")
    source = DirectoryCapabilitySource(tmp_path)

    with pytest.raises(CapabilitySymlinkError):
        source.discover()
    with pytest.raises(CapabilitySymlinkError):
        source.resolve("skill-a", "1")


def test_a_hardlinked_file_is_refused(tmp_path: Path) -> None:
    package = _write_package(tmp_path, "skill-a", package_files("skill-a"))
    os.link(package / "SKILL.md", tmp_path / "second-name")

    with pytest.raises(CapabilitySymlinkError):
        DirectoryCapabilitySource(tmp_path).resolve("skill-a", "1")


def test_fetch_closure_returns_the_exact_files_and_preserves_the_executable_bit(
    tmp_path: Path,
) -> None:
    files = package_files(
        "tool-a",
        kind=PackageKind.COMMAND,
        command=CommandSpec.create(argv=("scripts/run.sh",), input_schema={"type": "object"}),
    )
    assert any(item.executable for item in files)
    _write_package(tmp_path, "tool-a", files)
    source = DirectoryCapabilitySource(tmp_path)

    fetched = source.fetch_closure("tool-a", "1")

    assert _as_set(fetched) == _as_set(files)
    descriptor = source.resolve("tool-a", "1")
    assert descriptor is not None
    assert descriptor.digest == build_closure(fetched).digest


@pytest.mark.parametrize("path", ["references/guide.md", "SKILL.md"])
def test_changing_any_file_changes_the_digest_and_revision(tmp_path: Path, path: str) -> None:
    files = package_files("skill-a")
    package = _write_package(tmp_path, "skill-a", files)
    source = DirectoryCapabilitySource(tmp_path)
    before = source.resolve("skill-a", "1")

    (package / path).write_bytes(b"changed content\n")
    after = source.resolve("skill-a", "1")

    assert before is not None
    assert after is not None
    assert after.digest != before.digest
    assert after.revision != before.revision


def test_changing_a_sibling_script_changes_the_digest(tmp_path: Path) -> None:
    files = package_files("tool-a", kind=PackageKind.COMMAND)
    _write_package(tmp_path, "tool-a", files)
    source = DirectoryCapabilitySource(tmp_path)
    before = source.resolve("tool-a", "1")

    changed = replace_file(files, "scripts/run.py", b"print('changed')\n", executable=True)
    _write_package(tmp_path, "tool-a", changed)
    after = source.resolve("tool-a", "1")

    assert before is not None
    assert after is not None
    assert after.digest != before.digest


def test_a_signature_claim_is_carried_but_stays_unauthenticated_without_a_verifier(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, "skill-a", package_files("skill-a"))
    port = DirectoryCapabilitySource(tmp_path, signatures={("skill-a", "1"): (b"sig", "key-1")})

    descriptor = port.resolve("skill-a", "1")
    lock = CapabilityResolver([CapabilitySource(SourceKind.USER, "user", port)], host=HOST).resolve(
        [CapabilityRequest(name="skill-a", version="1")]
    )

    assert descriptor is not None
    assert descriptor.signature == b"sig"
    assert descriptor.signer_key_id == "key-1"
    assert lock.entries[0].provenance.status is ProvenanceStatus.UNAUTHENTICATED


def test_probe_reports_available_and_misconfigured_roots(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    (tmp_path / "a-file").write_bytes(b"x")

    assert DirectoryCapabilitySource(tmp_path / "root").probe().availability is (
        AdapterAvailability.AVAILABLE
    )
    assert DirectoryCapabilitySource(tmp_path / "missing").probe().availability is (
        AdapterAvailability.MISCONFIGURED
    )
    assert DirectoryCapabilitySource(tmp_path / "a-file").probe().availability is (
        AdapterAvailability.MISCONFIGURED
    )


def test_discover_on_a_missing_root_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(CapabilitySourceError):
        DirectoryCapabilitySource(tmp_path / "missing").discover()


def test_a_directory_can_never_be_registered_as_the_reviewed_project_source(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, "skill-a", package_files("skill-a"))

    with pytest.raises(InvalidInputError, match="never a reviewed base"):
        CapabilitySource(SourceKind.PROJECT, "project", DirectoryCapabilitySource(tmp_path))


def test_a_user_registered_directory_resolves_as_run_introduced_and_unauthenticated(
    tmp_path: Path,
) -> None:
    _write_package(tmp_path, "skill-a", package_files("skill-a"))

    lock = _resolver(SourceKind.USER, tmp_path).resolve(
        [CapabilityRequest(name="skill-a", version="1")]
    )

    entry = lock.entries[0]
    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.introduced_by_run is True
    assert entry.requires_human_approval()
