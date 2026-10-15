from __future__ import annotations

import json

import pytest

from enginery.capabilities.errors import CapabilityManifestError
from enginery.capabilities.manifest import (
    CapabilityPermission,
    DependencyPin,
    PackageCompatibility,
    PackageKind,
    PackageManifest,
    parse_manifest,
    render_manifest,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "name": "skill-a",
        "version": "1.2.0",
        "kind": "instruction",
    }
    document.update(overrides)
    return document


def _parse(document: dict[str, object]) -> PackageManifest:
    return parse_manifest(json.dumps(document).encode("utf-8"))


def test_minimal_manifest_parses_with_empty_declarations() -> None:
    manifest = _parse(_document())

    assert manifest.name == "skill-a"
    assert manifest.kind is PackageKind.INSTRUCTION
    assert manifest.license is None
    assert manifest.dependencies == ()
    assert manifest.permissions == ()


def test_full_manifest_round_trips_through_its_canonical_rendering() -> None:
    pin = DependencyPin("dep-a", "2", Digest.of_bytes(b"dep-a"))
    manifest = PackageManifest(
        name="skill-a",
        version="1.2.0",
        kind=PackageKind.COMMAND,
        license="Apache-2.0",
        dependencies=(pin,),
        permissions=(CapabilityPermission.REPOSITORY_READ, CapabilityPermission.PROCESS_EXECUTE),
        compatibility=PackageCompatibility(
            adapter_api_min=1, adapter_api_max=2, platforms=("linux",)
        ),
    )

    assert parse_manifest(render_manifest(manifest)) == manifest


def test_rendering_is_canonical_regardless_of_declaration_order() -> None:
    first = PackageManifest(
        name="skill-a",
        version="1",
        kind=PackageKind.HOOK,
        permissions=(CapabilityPermission.NETWORK_ACCESS, CapabilityPermission.REPOSITORY_READ),
    )
    second = PackageManifest(
        name="skill-a",
        version="1",
        kind=PackageKind.HOOK,
        permissions=(CapabilityPermission.REPOSITORY_READ, CapabilityPermission.NETWORK_ACCESS),
    )

    assert render_manifest(first) == render_manifest(second)


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"kind": "plugin"},
        {"name": "../escape"},
        {"name": ""},
        {"version": "1 0"},
        {"license": 7},
        {"permissions": ["repository.read", "root.everything"]},
        {"permissions": "repository.read"},
        {"dependencies": [{"name": "dep-a", "version": "1"}]},
        {"dependencies": [{"name": "dep-a", "version": "1", "closure_digest": "sha256:zz"}]},
        {"dependencies": [{"name": "skill-a", "version": "1", "closure_digest": "x"}]},
        {"compatibility": {"adapter_api_min": 0}},
        {"compatibility": {"adapter_api_min": 3, "adapter_api_max": 2}},
        {"compatibility": {"platforms": ["Linux!"]}},
        {"compatibility": {"surprise": True}},
        {"surprise": "unreviewed field"},
    ],
)
def test_invalid_manifests_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(CapabilityManifestError):
        _parse(_document(**overrides))


def test_duplicate_dependency_names_are_rejected() -> None:
    pin = {"name": "dep-a", "version": "1", "closure_digest": str(Digest.of_bytes(b"x"))}

    with pytest.raises(CapabilityManifestError) as excinfo:
        _parse(_document(dependencies=[pin, pin]))

    reason = str(excinfo.value.details["reason"])
    assert "must be unique" in reason
    assert "dep-a" in reason


def test_duplicate_json_keys_are_rejected() -> None:
    raw = b'{"schema_version": 1, "name": "a", "name": "b", "version": "1", "kind": "instruction"}'

    with pytest.raises(CapabilityManifestError) as excinfo:
        parse_manifest(raw)

    assert "duplicate key" in str(excinfo.value.details["reason"])


@pytest.mark.parametrize("raw", [b"not json", b"[]", b"\xff\xfe", b'"text"'])
def test_non_object_or_non_utf8_manifests_are_rejected(raw: bytes) -> None:
    with pytest.raises(CapabilityManifestError):
        parse_manifest(raw)


def test_manifest_dataclass_rejects_a_self_dependency() -> None:
    pin = DependencyPin("skill-a", "1", Digest.of_bytes(b"x"))

    with pytest.raises(InvalidInputError, match="itself"):
        PackageManifest(
            name="skill-a", version="1", kind=PackageKind.INSTRUCTION, dependencies=(pin,)
        )
