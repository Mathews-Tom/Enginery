from __future__ import annotations

import pytest

from enginery.application.delivery_ports import CapabilityDescriptor, PackageFile
from enginery.capabilities.errors import (
    CapabilityAssetTypeError,
    CapabilityDependencyError,
    CapabilityDigestMismatchError,
    CapabilityLicenseMismatchError,
    CapabilityLockDriftError,
    CapabilityManifestError,
)
from enginery.capabilities.lock import CapabilityLock, ProvenanceStatus
from enginery.capabilities.manifest import PackageKind
from enginery.capabilities.resolver import (
    CapabilityNotFoundError,
    CapabilityRequest,
    CapabilityResolver,
)
from enginery.domain.digests import Digest
from tests.support.capabilities import (
    FakeCapabilitySource,
    closure_digest_of,
    descriptor_for,
    package_files,
    pin_for,
    replace_file,
)


def _request(name: str = "skill-a", version: str = "1", **kwargs: str) -> CapabilityRequest:
    return CapabilityRequest(name=name, version=version, **kwargs)


def _source_serving(
    descriptor_files: tuple[PackageFile, ...], served_files: tuple[PackageFile, ...]
) -> FakeCapabilitySource:
    descriptor = descriptor_for(descriptor_files)
    key = (descriptor.name, descriptor.version)
    return FakeCapabilitySource({key: descriptor}, {key: served_files})


def test_reviewed_base_capability_resolves_as_local_trusted() -> None:
    source = FakeCapabilitySource.of(package_files("skill-a"))
    resolver = CapabilityResolver([source], reviewed_base=frozenset({("skill-a", "1")}))

    lock = resolver.resolve([_request()])

    entry = lock.get("skill-a")
    assert entry is not None
    assert entry.introduced_by_run is False
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.requires_human_approval() is False


def test_capability_outside_reviewed_base_is_introduced_by_run_and_unauthenticated() -> None:
    resolver = CapabilityResolver([FakeCapabilitySource.of(package_files("skill-a"))])

    lock = resolver.resolve([_request()])

    entry = lock.get("skill-a")
    assert entry is not None
    assert entry.introduced_by_run is True
    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.requires_human_approval() is True


def test_a_matching_digest_never_proves_publisher_authenticity() -> None:
    """The source's closure digest verifies the files, not who published them."""

    files = package_files("skill-a")
    resolver = CapabilityResolver([FakeCapabilitySource.of(files, provenance="armory")])

    entry = resolver.resolve([_request()]).entries[0]

    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.provenance.signer_key_id is None


def test_signature_verifier_upgrades_run_introduced_capability_to_authenticated() -> None:
    source = FakeCapabilitySource.of(package_files("skill-a"))
    resolver = CapabilityResolver([source], signature_verifier=lambda descriptor: "signer-1")

    lock = resolver.resolve([_request()])

    entry = lock.get("skill-a")
    assert entry is not None
    assert entry.provenance.status is ProvenanceStatus.AUTHENTICATED
    assert entry.provenance.signer_key_id == "signer-1"
    # Authenticated provenance still does not exempt a run-introduced capability.
    assert entry.requires_human_approval() is True


def test_signature_verifier_sees_the_verified_closure_digest() -> None:
    files = package_files("skill-a")
    seen: list[Digest] = []

    def verifier(descriptor: CapabilityDescriptor) -> str | None:
        seen.append(descriptor.digest)
        return None

    CapabilityResolver([FakeCapabilitySource.of(files)], signature_verifier=verifier).resolve(
        [_request()]
    )

    assert seen == [closure_digest_of(files)]


def test_resolved_entry_records_origin_closure_and_license() -> None:
    files = package_files("skill-a", extra={"scripts/run.py": b"print(1)\n"})
    descriptor = descriptor_for(files, provenance="armory", revision="a" * 40)
    key = (descriptor.name, descriptor.version)
    source = FakeCapabilitySource({key: descriptor}, {key: files})

    entry = CapabilityResolver([source]).resolve([_request()]).entries[0]

    assert entry.revision == "a" * 40
    assert entry.provenance.source_label == "armory"
    assert entry.digest == closure_digest_of(files)
    assert entry.license == "MIT"
    assert entry.kind is PackageKind.INSTRUCTION
    assert {record.path for record in entry.files} >= {"SKILL.md", "scripts/run.py"}


def test_unknown_capability_raises_not_found() -> None:
    resolver = CapabilityResolver([FakeCapabilitySource({}, {})])

    with pytest.raises(CapabilityNotFoundError):
        resolver.resolve([_request("missing")])


def test_license_mismatch_is_rejected_before_lock_construction() -> None:
    """A caller-declared expected license that disagrees with what the package
    declares must fail closed rather than silently lock under another license."""

    files = package_files("skill-a", license_id="GPL-3.0")
    resolver = CapabilityResolver([FakeCapabilitySource.of(files)])

    with pytest.raises(CapabilityLicenseMismatchError):
        resolver.resolve([_request(expected_license="MIT")])


def test_source_declared_license_fills_a_package_without_one() -> None:
    files = package_files("skill-a", license_id=None)
    descriptor = descriptor_for(files, license_id="Apache-2.0")
    key = (descriptor.name, descriptor.version)
    resolver = CapabilityResolver([FakeCapabilitySource({key: descriptor}, {key: files})])

    entry = resolver.resolve([_request(expected_license="Apache-2.0")]).entries[0]

    assert entry.license == "Apache-2.0"


def test_source_license_that_contradicts_the_manifest_is_rejected() -> None:
    files = package_files("skill-a", license_id="MIT")
    descriptor = descriptor_for(files, license_id="GPL-3.0")
    key = (descriptor.name, descriptor.version)
    resolver = CapabilityResolver([FakeCapabilitySource({key: descriptor}, {key: files})])

    with pytest.raises(CapabilityManifestError, match="contradicts"):
        resolver.resolve([_request()])


def test_missing_license_is_locked_but_recorded_as_absent() -> None:
    resolver = CapabilityResolver(
        [FakeCapabilitySource.of(package_files("skill-a", license_id=None))]
    )

    entry = resolver.resolve([_request()]).entries[0]

    assert entry.license is None
    assert entry.redistribution_blocked() is True


def test_digest_swap_is_rejected_before_lock_construction() -> None:
    """A source that reports one closure digest but serves different files must fail closed."""

    declared = package_files("skill-a", body=b"declared")
    swapped = package_files("skill-a", body=b"swapped")
    resolver = CapabilityResolver([_source_serving(declared, swapped)])

    with pytest.raises(CapabilityDigestMismatchError):
        resolver.resolve([_request()])


def test_a_swapped_sibling_script_is_rejected_even_when_the_instruction_is_unchanged() -> None:
    declared = package_files("skill-a", extra={"scripts/run.py": b"print('reviewed')\n"})
    swapped = replace_file(declared, "scripts/run.py", b"print('malicious')\n")
    resolver = CapabilityResolver([_source_serving(declared, swapped)])

    with pytest.raises(CapabilityDigestMismatchError):
        resolver.resolve([_request()])


def test_a_hidden_extra_file_is_rejected_by_the_closure_digest() -> None:
    declared = package_files("skill-a")
    served = (*declared, PackageFile("references/hidden.md", b"unreviewed"))
    resolver = CapabilityResolver([_source_serving(declared, served)])

    with pytest.raises(CapabilityDigestMismatchError):
        resolver.resolve([_request()])


def test_an_unreviewed_asset_type_fails_resolution() -> None:
    declared = package_files("skill-a")
    hostile = (*declared, PackageFile("scripts/payload.rb", b"puts 1"))
    resolver = CapabilityResolver([_source_serving(declared, hostile)])

    with pytest.raises(CapabilityAssetTypeError):
        resolver.resolve([_request()])


def test_manifest_identity_must_match_the_requested_capability() -> None:
    other = package_files("other-skill")
    descriptor = CapabilityDescriptor(
        name="skill-a",
        version="1",
        digest=closure_digest_of(other),
        provenance="repository-local",
        revision="rev-1",
    )
    source = FakeCapabilitySource({("skill-a", "1"): descriptor}, {("skill-a", "1"): other})

    with pytest.raises(CapabilityManifestError, match="identity"):
        CapabilityResolver([source]).resolve([_request()])


def test_mutable_reference_drift_against_an_in_flight_lock_is_rejected() -> None:
    """A source that later resolves the same locked version to a different closure
    must never silently mutate an in-flight lock."""

    reviewed = frozenset({("skill-a", "1")})
    original = FakeCapabilitySource.of(package_files("skill-a", body=b"payload-v1"))
    previous_lock: CapabilityLock = CapabilityResolver([original], reviewed_base=reviewed).resolve(
        [_request()]
    )
    drifted = FakeCapabilitySource.of(package_files("skill-a", body=b"payload-v1-mutated"))

    with pytest.raises(CapabilityLockDriftError):
        CapabilityResolver([drifted], reviewed_base=reviewed).resolve(
            [_request()], previous_lock=previous_lock
        )


def test_a_sibling_script_change_is_drift_against_an_in_flight_lock() -> None:
    reviewed = frozenset({("skill-a", "1")})
    base = package_files("skill-a", extra={"scripts/run.py": b"print(1)\n"})
    previous_lock = CapabilityResolver(
        [FakeCapabilitySource.of(base)], reviewed_base=reviewed
    ).resolve([_request()])
    changed = replace_file(base, "scripts/run.py", b"print(2)\n")

    with pytest.raises(CapabilityLockDriftError):
        CapabilityResolver([FakeCapabilitySource.of(changed)], reviewed_base=reviewed).resolve(
            [_request()], previous_lock=previous_lock
        )


def test_stable_reference_re_resolution_does_not_trip_drift_detection() -> None:
    reviewed = frozenset({("skill-a", "1")})
    resolver = CapabilityResolver(
        [FakeCapabilitySource.of(package_files("skill-a"))], reviewed_base=reviewed
    )
    previous_lock = resolver.resolve([_request()])

    lock_again = resolver.resolve([_request()], previous_lock=previous_lock)

    assert lock_again.digest() == previous_lock.digest()


def test_a_new_version_is_not_drift_against_a_prior_version() -> None:
    reviewed = frozenset({("skill-a", "1"), ("skill-a", "2")})
    first = CapabilityResolver(
        [FakeCapabilitySource.of(package_files("skill-a", "1"))], reviewed_base=reviewed
    ).resolve([_request()])
    second_source = FakeCapabilitySource.of(package_files("skill-a", "2", body=b"new"))

    lock = CapabilityResolver([second_source], reviewed_base=reviewed).resolve(
        [_request(version="2")], previous_lock=first
    )

    assert lock.entries[0].version == "2"


def test_declared_dependencies_are_resolved_and_locked_as_their_own_entries() -> None:
    dependency = package_files("dep-a", extra={"scripts/helper.py": b"print('dep')\n"})
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    resolver = CapabilityResolver([FakeCapabilitySource.of(parent, dependency)])

    resolved = resolver.resolve_with_files([_request()])

    assert [entry.name for entry in resolved.lock.entries] == ["dep-a", "skill-a"]
    assert set(resolved.files_by_name) == {"dep-a", "skill-a"}
    dependency_entry = resolved.lock.get("dep-a")
    assert dependency_entry is not None
    assert dependency_entry.digest == closure_digest_of(dependency)
    assert dependency_entry.introduced_by_run is True


def test_a_dependency_inside_the_reviewed_base_is_local_trusted() -> None:
    dependency = package_files("dep-a")
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    resolver = CapabilityResolver(
        [FakeCapabilitySource.of(parent, dependency)],
        reviewed_base=frozenset({("skill-a", "1"), ("dep-a", "1")}),
    )

    lock = resolver.resolve([_request()])

    assert [entry.provenance.status for entry in lock.entries] == [
        ProvenanceStatus.LOCAL_TRUSTED,
        ProvenanceStatus.LOCAL_TRUSTED,
    ]


def test_a_dependency_that_resolves_to_a_changed_closure_is_rejected() -> None:
    pinned = package_files("dep-a", body=b"# pinned\n")
    served = package_files("dep-a", body=b"# changed\n")
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", pinned)])
    resolver = CapabilityResolver([FakeCapabilitySource.of(parent, served)])

    with pytest.raises(CapabilityDependencyError, match="pinned digest"):
        resolver.resolve([_request()])


def test_a_changed_dependency_script_no_longer_satisfies_the_pin() -> None:
    dependency = package_files("dep-a", extra={"scripts/helper.py": b"print('v1')\n"})
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    changed_dependency = replace_file(dependency, "scripts/helper.py", b"print('v2')\n")
    resolver = CapabilityResolver([FakeCapabilitySource.of(parent, changed_dependency)])

    with pytest.raises(CapabilityDependencyError):
        resolver.resolve([_request()])


def test_a_missing_dependency_is_not_found() -> None:
    dependency = package_files("dep-a")
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    resolver = CapabilityResolver([FakeCapabilitySource.of(parent)])

    with pytest.raises(CapabilityNotFoundError):
        resolver.resolve([_request()])


def test_cyclic_dependencies_are_rejected() -> None:
    placeholder = package_files("skill-b")
    first = package_files("skill-a", dependencies=[pin_for("skill-b", placeholder)])
    second = package_files("skill-b", dependencies=[pin_for("skill-a", first)])
    resolver = CapabilityResolver([FakeCapabilitySource.of(first, second)])

    with pytest.raises(CapabilityDependencyError, match="cyclic"):
        resolver.resolve([_request()])


def test_two_requests_for_different_versions_of_one_name_conflict() -> None:
    resolver = CapabilityResolver(
        [
            FakeCapabilitySource.of(
                package_files("skill-a", "1"), package_files("skill-a", "2", body=b"two")
            )
        ]
    )

    with pytest.raises(CapabilityDependencyError, match="different versions"):
        resolver.resolve([_request(version="1"), _request(version="2")])


def test_no_sources_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CapabilityResolver([])


def test_expected_license_is_enforced_for_a_name_a_dependency_already_resolved() -> None:
    dependency = package_files("dep-a", license_id="GPL-3.0")
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    resolver = CapabilityResolver([FakeCapabilitySource.of(parent, dependency)])

    with pytest.raises(CapabilityLicenseMismatchError):
        resolver.resolve([_request("skill-a"), _request("dep-a", expected_license="MIT")])
