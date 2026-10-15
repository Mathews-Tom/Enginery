from __future__ import annotations

import pytest

from enginery.application.adapter_types import ADAPTER_API_VERSION, AdapterStatus
from enginery.application.delivery_ports import (
    CapabilityDescriptor,
    CapabilityListing,
    PackageFile,
)
from enginery.capabilities.compatibility import CompatibilityStatus, HostProfile
from enginery.capabilities import license_text
from enginery.capabilities.errors import (
    CapabilityAssetTypeError,
    CapabilityDependencyError,
    CapabilityDigestMismatchError,
    CapabilityError,
    CapabilityLicenseMismatchError,
    CapabilityLockDriftError,
    CapabilityManifestError,
)
from enginery.capabilities.lock import CapabilityLock, ProvenanceStatus
from enginery.capabilities.manifest import (
    CapabilityPermission,
    PackageCompatibility,
    PackageKind,
)
from enginery.capabilities.resolver import (
    CapabilityNotFoundError,
    CapabilityRequest,
    CapabilityResolver,
)
from enginery.capabilities.sources import (
    CapabilitySource,
    SourceKind,
    order_sources,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from tests.support.capabilities import (
    HOST,
    FakeCapabilitySource,
    capability_source,
    closure_digest_of,
    descriptor_for,
    package_files,
    pin_for,
    project_source,
    replace_file,
    user_source,
    wrap_source,
)


def _request(name: str = "skill-a", version: str = "1", **kwargs: str) -> CapabilityRequest:
    return CapabilityRequest(name=name, version=version, **kwargs)


def _source_serving(
    descriptor_files: tuple[PackageFile, ...], served_files: tuple[PackageFile, ...]
) -> CapabilitySource:
    descriptor = descriptor_for(descriptor_files)
    key = (descriptor.name, descriptor.version)
    return wrap_source(FakeCapabilitySource({key: descriptor}, {key: served_files}))


class _RecordingSource:
    """A source port that records every ``resolve`` and can fail on demand."""

    def __init__(
        self, inner: FakeCapabilitySource, *, failure: CapabilityError | None = None
    ) -> None:
        self._inner = inner
        self._failure = failure
        self.resolved: list[tuple[str, str]] = []

    def probe(self) -> AdapterStatus:  # pragma: no cover - unused by the resolver
        raise NotImplementedError

    def discover(self) -> tuple[CapabilityListing, ...]:  # pragma: no cover - unused
        return self._inner.discover()

    def resolve(self, name: str, version: str) -> CapabilityDescriptor | None:
        self.resolved.append((name, version))
        if self._failure is not None:
            raise self._failure
        return self._inner.resolve(name, version)

    def fetch_closure(self, name: str, version: str) -> tuple[PackageFile, ...]:
        return self._inner.fetch_closure(name, version)


def test_reviewed_base_capability_resolves_as_local_trusted() -> None:
    resolver = CapabilityResolver([project_source(package_files("skill-a"))])

    lock = resolver.resolve([_request()])

    entry = lock.get("skill-a")
    assert entry is not None
    assert entry.introduced_by_run is False
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.requires_human_approval() is False


def test_capability_outside_reviewed_base_is_introduced_by_run_and_unauthenticated() -> None:
    resolver = CapabilityResolver([user_source(package_files("skill-a"))])

    lock = resolver.resolve([_request()])

    entry = lock.get("skill-a")
    assert entry is not None
    assert entry.introduced_by_run is True
    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.requires_human_approval() is True


def test_a_matching_digest_never_proves_publisher_authenticity() -> None:
    """The source's closure digest verifies the files, not who published them."""

    files = package_files("skill-a")
    source = wrap_source(FakeCapabilitySource.of(files, provenance="armory"), SourceKind.ARMORY)
    resolver = CapabilityResolver([source])

    entry = resolver.resolve([_request()]).entries[0]

    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.provenance.signer_key_id is None


def test_signature_verifier_upgrades_run_introduced_capability_to_authenticated() -> None:
    source = user_source(package_files("skill-a"))
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

    CapabilityResolver([user_source(files)], signature_verifier=verifier).resolve([_request()])

    assert seen == [closure_digest_of(files)]


def test_resolved_entry_records_origin_closure_and_license() -> None:
    files = package_files("skill-a", extra={"scripts/run.py": b"print(1)\n"})
    descriptor = descriptor_for(files, provenance="armory", revision="a" * 40)
    key = (descriptor.name, descriptor.version)
    source = wrap_source(FakeCapabilitySource({key: descriptor}, {key: files}))

    entry = CapabilityResolver([source]).resolve([_request()]).entries[0]

    assert entry.revision == "a" * 40
    assert entry.provenance.source_label == "armory"
    assert entry.digest == closure_digest_of(files)
    assert entry.license == "MIT"
    assert entry.kind is PackageKind.INSTRUCTION
    assert {record.path for record in entry.files} >= {"SKILL.md", "scripts/run.py"}


def test_unknown_capability_raises_not_found() -> None:
    resolver = CapabilityResolver([wrap_source(FakeCapabilitySource({}, {}))])

    with pytest.raises(CapabilityNotFoundError):
        resolver.resolve([_request("missing")])


def test_license_mismatch_is_rejected_before_lock_construction() -> None:
    """A caller-declared expected license that disagrees with what the package
    declares must fail closed rather than silently lock under another license."""

    files = package_files("skill-a", license_id="GPL-3.0")
    resolver = CapabilityResolver([user_source(files)])

    with pytest.raises(CapabilityLicenseMismatchError):
        resolver.resolve([_request(expected_license="MIT")])


def test_source_declared_license_fills_a_package_without_one() -> None:
    files = package_files("skill-a", license_id=None)
    descriptor = descriptor_for(files, license_id="Apache-2.0")
    key = (descriptor.name, descriptor.version)
    resolver = CapabilityResolver(
        [wrap_source(FakeCapabilitySource({key: descriptor}, {key: files}))]
    )

    entry = resolver.resolve([_request(expected_license="Apache-2.0")]).entries[0]

    assert entry.license == "Apache-2.0"


def test_source_license_that_contradicts_the_manifest_is_rejected() -> None:
    files = package_files("skill-a", license_id="MIT")
    descriptor = descriptor_for(files, license_id="GPL-3.0")
    key = (descriptor.name, descriptor.version)
    resolver = CapabilityResolver(
        [wrap_source(FakeCapabilitySource({key: descriptor}, {key: files}))]
    )

    with pytest.raises(CapabilityManifestError, match="contradicts"):
        resolver.resolve([_request()])


def test_missing_license_is_locked_but_recorded_as_absent() -> None:
    resolver = CapabilityResolver([user_source(package_files("skill-a", license_id=None))])

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
    source = wrap_source(
        FakeCapabilitySource({("skill-a", "1"): descriptor}, {("skill-a", "1"): other})
    )

    with pytest.raises(CapabilityManifestError, match="identity"):
        CapabilityResolver([source]).resolve([_request()])


def test_mutable_reference_drift_against_an_in_flight_lock_is_rejected() -> None:
    """A source that later resolves the same locked version to a different closure
    must never silently mutate an in-flight lock."""

    original = project_source(package_files("skill-a", body=b"payload-v1"))
    previous_lock: CapabilityLock = CapabilityResolver([original]).resolve([_request()])
    drifted = project_source(package_files("skill-a", body=b"payload-v1-mutated"))

    with pytest.raises(CapabilityLockDriftError):
        CapabilityResolver([drifted]).resolve([_request()], previous_lock=previous_lock)


def test_a_sibling_script_change_is_drift_against_an_in_flight_lock() -> None:
    base = package_files("skill-a", extra={"scripts/run.py": b"print(1)\n"})
    previous_lock = CapabilityResolver([project_source(base)]).resolve([_request()])
    changed = replace_file(base, "scripts/run.py", b"print(2)\n")

    with pytest.raises(CapabilityLockDriftError):
        CapabilityResolver([project_source(changed)]).resolve(
            [_request()], previous_lock=previous_lock
        )


def test_stable_reference_re_resolution_does_not_trip_drift_detection() -> None:
    resolver = CapabilityResolver([project_source(package_files("skill-a"))])
    previous_lock = resolver.resolve([_request()])

    lock_again = resolver.resolve([_request()], previous_lock=previous_lock)

    assert lock_again.digest() == previous_lock.digest()


def test_a_new_version_is_not_drift_against_a_prior_version() -> None:
    first = CapabilityResolver([project_source(package_files("skill-a", "1"))]).resolve(
        [_request()]
    )
    second_source = project_source(package_files("skill-a", "2", body=b"new"))

    lock = CapabilityResolver([second_source]).resolve([_request(version="2")], previous_lock=first)

    assert lock.entries[0].version == "2"


def test_declared_dependencies_are_resolved_and_locked_as_their_own_entries() -> None:
    dependency = package_files("dep-a", extra={"scripts/helper.py": b"print('dep')\n"})
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    resolver = CapabilityResolver([user_source(parent, dependency)])

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
    resolver = CapabilityResolver([project_source(parent, dependency)])

    lock = resolver.resolve([_request()])

    assert [entry.provenance.status for entry in lock.entries] == [
        ProvenanceStatus.LOCAL_TRUSTED,
        ProvenanceStatus.LOCAL_TRUSTED,
    ]


def test_a_reviewed_parent_does_not_make_a_user_served_dependency_trusted() -> None:
    dependency = package_files("dep-a")
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    resolver = CapabilityResolver([project_source(parent), user_source(dependency)])

    lock = resolver.resolve([_request()])

    parent_entry, dependency_entry = lock.get("skill-a"), lock.get("dep-a")
    assert parent_entry is not None
    assert dependency_entry is not None
    assert parent_entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert dependency_entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert dependency_entry.introduced_by_run is True


def test_a_dependency_that_resolves_to_a_changed_closure_is_rejected() -> None:
    pinned = package_files("dep-a", body=b"# pinned\n")
    served = package_files("dep-a", body=b"# changed\n")
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", pinned)])
    resolver = CapabilityResolver([user_source(parent, served)])

    with pytest.raises(CapabilityDependencyError, match="pinned digest"):
        resolver.resolve([_request()])


def test_a_changed_dependency_script_no_longer_satisfies_the_pin() -> None:
    dependency = package_files("dep-a", extra={"scripts/helper.py": b"print('v1')\n"})
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    changed_dependency = replace_file(dependency, "scripts/helper.py", b"print('v2')\n")
    resolver = CapabilityResolver([user_source(parent, changed_dependency)])

    with pytest.raises(CapabilityDependencyError):
        resolver.resolve([_request()])


def test_a_missing_dependency_is_not_found() -> None:
    dependency = package_files("dep-a")
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    resolver = CapabilityResolver([user_source(parent)])

    with pytest.raises(CapabilityNotFoundError):
        resolver.resolve([_request()])


def test_cyclic_dependencies_are_rejected() -> None:
    placeholder = package_files("skill-b")
    first = package_files("skill-a", dependencies=[pin_for("skill-b", placeholder)])
    second = package_files("skill-b", dependencies=[pin_for("skill-a", first)])
    resolver = CapabilityResolver([user_source(first, second)])

    with pytest.raises(CapabilityDependencyError, match="cyclic"):
        resolver.resolve([_request()])


def test_two_requests_for_different_versions_of_one_name_conflict() -> None:
    resolver = CapabilityResolver(
        [user_source(package_files("skill-a", "1"), package_files("skill-a", "2", body=b"two"))]
    )

    with pytest.raises(CapabilityDependencyError, match="different versions"):
        resolver.resolve([_request(version="1"), _request(version="2")])


def test_no_sources_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CapabilityResolver([])


def test_source_precedence_is_project_then_user_then_git_then_armory() -> None:
    project_files = package_files("skill-a", body=b"project")
    user_files = package_files("skill-a", body=b"user")
    git_files = package_files("skill-a", body=b"git")
    armory_files = package_files("skill-a", body=b"armory")
    # Registered out of precedence order: the kind decides, not the registration order.
    resolver = CapabilityResolver(
        [
            capability_source(SourceKind.ARMORY, armory_files),
            capability_source(SourceKind.GIT, git_files),
            capability_source(SourceKind.USER, user_files),
            capability_source(SourceKind.PROJECT, project_files),
        ]
    )

    resolved = resolver.resolve_with_files([_request()], report_shadowed=True)

    selection = resolved.selections["skill-a"]
    assert selection.selected.kind is SourceKind.PROJECT
    assert [candidate.kind for candidate in selection.shadowed] == [
        SourceKind.USER,
        SourceKind.GIT,
        SourceKind.ARMORY,
    ]
    assert resolved.files_by_name["skill-a"] == project_files
    entry = resolved.lock.entries[0]
    assert entry.digest == closure_digest_of(project_files)


def test_shadowed_candidates_report_whether_they_share_the_selected_closure() -> None:
    same = package_files("skill-a", body=b"same")
    different = package_files("skill-a", body=b"different")
    resolver = CapabilityResolver(
        [
            capability_source(SourceKind.PROJECT, same),
            capability_source(SourceKind.USER, same),
            capability_source(SourceKind.GIT, different),
        ]
    )

    resolved = resolver.resolve_with_files([_request()], report_shadowed=True)

    document = resolved.selections["skill-a"].to_json()
    shadowed = document["shadowed"]
    assert isinstance(shadowed, list)
    assert [(item["kind"], item["same_closure"]) for item in shadowed] == [
        ("user", True),
        ("git", False),
    ]
    selected = document["selected"]
    assert isinstance(selected, dict)
    assert selected["kind"] == "project"
    assert selected["closure_digest"] == str(closure_digest_of(same))


def test_lower_precedence_sources_are_not_queried_without_report_shadowed() -> None:
    files = package_files("skill-a")
    lower = _RecordingSource(FakeCapabilitySource.of(files))
    resolver = CapabilityResolver(
        [
            project_source(files),
            CapabilitySource(kind=SourceKind.USER, label="user-source", port=lower),
        ]
    )

    quiet = resolver.resolve_with_files([_request()])
    assert lower.resolved == []
    assert quiet.selections["skill-a"].shadowed == ()

    loud = resolver.resolve_with_files([_request()], report_shadowed=True)
    assert lower.resolved == [("skill-a", "1")]
    assert [candidate.kind for candidate in loud.selections["skill-a"].shadowed] == [
        SourceKind.USER
    ]


def test_a_failing_shadowed_source_is_reported_with_its_problem() -> None:
    files = package_files("skill-a")
    broken = _RecordingSource(
        FakeCapabilitySource.of(files), failure=CapabilityAssetTypeError("unlockable asset")
    )
    resolver = CapabilityResolver(
        [
            project_source(files),
            CapabilitySource(kind=SourceKind.ARMORY, label="armory-source", port=broken),
        ]
    )

    resolved = resolver.resolve_with_files([_request()], report_shadowed=True)

    (candidate,) = resolved.selections["skill-a"].shadowed
    assert candidate.kind is SourceKind.ARMORY
    assert candidate.descriptor is None
    assert candidate.problem is not None
    assert "unlockable asset" in candidate.problem
    assert resolved.lock.entries[0].digest == closure_digest_of(files)


def test_a_failing_selected_source_propagates() -> None:
    files = package_files("skill-a")
    broken = _RecordingSource(
        FakeCapabilitySource.of(files), failure=CapabilityAssetTypeError("unlockable asset")
    )
    resolver = CapabilityResolver(
        [
            CapabilitySource(kind=SourceKind.USER, label="user-source", port=broken),
            capability_source(SourceKind.ARMORY, files),
        ]
    )

    with pytest.raises(CapabilityAssetTypeError, match="unlockable asset"):
        resolver.resolve_with_files([_request()], report_shadowed=True)


@pytest.mark.parametrize("kind", [SourceKind.USER, SourceKind.GIT, SourceKind.ARMORY])
def test_only_the_project_source_is_trusted_with_identical_permission_typing(
    kind: SourceKind,
) -> None:
    permissions = [CapabilityPermission.REPOSITORY_READ, CapabilityPermission.NETWORK_ACCESS]
    files = package_files("skill-a", permissions=permissions)

    trusted = CapabilityResolver([project_source(files)]).resolve([_request()]).entries[0]
    foreign = CapabilityResolver([capability_source(kind, files)]).resolve([_request()]).entries[0]

    assert trusted.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert trusted.introduced_by_run is False
    assert trusted.requires_human_approval() is False
    assert foreign.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert foreign.introduced_by_run is True
    assert foreign.requires_human_approval() is True
    assert foreign.permissions == trusted.permissions
    assert set(foreign.permissions) == set(permissions)
    assert foreign.digest == trusted.digest


def test_two_project_sources_are_rejected() -> None:
    files = package_files("skill-a")

    with pytest.raises(InvalidInputError, match="at most one project"):
        order_sources([project_source(files), project_source(files)])
    with pytest.raises(InvalidInputError, match="at most one project"):
        CapabilityResolver([project_source(files), project_source(files)])


def test_the_compatibility_receipt_is_bound_into_the_lock_digest() -> None:
    files = package_files("skill-a", compatibility=PackageCompatibility(platforms=("linux",)))
    foreign_host = HostProfile(adapter_api_version=ADAPTER_API_VERSION, platform="darwin")

    compatible = CapabilityResolver([user_source(files)], host=HOST).resolve([_request()])
    incompatible = CapabilityResolver([user_source(files)], host=foreign_host).resolve([_request()])

    compatible_receipt = compatible.entries[0].receipt
    incompatible_receipt = incompatible.entries[0].receipt
    assert compatible_receipt.status is CompatibilityStatus.COMPATIBLE
    assert incompatible_receipt.status is CompatibilityStatus.INCOMPATIBLE
    assert incompatible_receipt.reasons
    assert any("linux" in reason for reason in incompatible_receipt.reasons)
    assert compatible.entries[0].digest == incompatible.entries[0].digest
    assert compatible.digest() != incompatible.digest()


def test_expected_license_is_enforced_for_a_name_a_dependency_already_resolved() -> None:
    dependency = package_files("dep-a", license_id="GPL-3.0")
    parent = package_files("skill-a", dependencies=[pin_for("dep-a", dependency)])
    resolver = CapabilityResolver([user_source(parent, dependency)])

    with pytest.raises(CapabilityLicenseMismatchError):
        resolver.resolve([_request("skill-a"), _request("dep-a", expected_license="MIT")])


def _license_source(manifest_license: str | None, text: bytes) -> FakeCapabilitySource:
    files = package_files("skill-a", license_id=manifest_license, extra={"LICENSE": text})
    return FakeCapabilitySource.of(files)


def test_a_declared_mit_package_shipping_gpl_text_resolves_to_unknown() -> None:
    gpl = b"GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"

    entry = CapabilityResolver([_license_source("MIT", gpl)]).resolve([_request()]).entries[0]

    assert entry.license == "unknown"


def test_a_declared_mit_package_shipping_the_exact_mit_text_stays_mit() -> None:
    text = f"MIT License\n\nCopyright (c) 2024 Foo\n\n{license_text._MIT_BODY}\n".encode()

    entry = CapabilityResolver([_license_source("MIT", text)]).resolve([_request()]).entries[0]

    assert entry.license == "MIT"


def test_a_conflicting_notice_makes_the_declared_license_unknown() -> None:
    files = package_files(
        "skill-a",
        license_id="MIT",
        extra={
            "LICENSE": license_text._MIT_BODY.encode(),
            "NOTICE": b"vendor/ is licensed under the GPLv3",
        },
    )

    entry = CapabilityResolver([FakeCapabilitySource.of(files)]).resolve([_request()]).entries[0]

    assert entry.license == "unknown"


@pytest.mark.parametrize(
    ("license_id", "body"),
    [
        ("Unlicense", license_text._UNLICENSE_BODY),
        ("0BSD", license_text._ZERO_BSD_BODY),
        ("Zlib", license_text._ZLIB_BODY),
        ("CC0-1.0", license_text._CC0_BODY),
    ],
)
def test_declared_unlicense_zero_bsd_zlib_and_cc0_survive_their_own_shipped_text(
    license_id: str, body: str
) -> None:
    source = _license_source(license_id, body.encode())

    entry = CapabilityResolver([source]).resolve([_request()]).entries[0]

    assert entry.license == license_id


def test_a_declared_license_without_a_template_is_unknown_when_its_text_ships() -> None:
    source = _license_source("MPL-2.0", b"Mozilla Public License Version 2.0\n")

    entry = CapabilityResolver([source]).resolve([_request()]).entries[0]

    assert entry.license == "unknown"


def test_a_declared_unlicense_with_a_mismatching_text_is_unknown() -> None:
    source = _license_source("Unlicense", license_text._MIT_BODY.encode())

    entry = CapabilityResolver([source]).resolve([_request()]).entries[0]

    assert entry.license == "unknown"
