from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from enginery.application import execution_catalog
from enginery.application.execution_catalog import (
    CURRENT_CATALOG_VERSION,
    REGISTERED_CATALOG_DIGESTS,
    ExecutionCatalogError,
    load_execution_catalog,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError
from enginery.domain.execution_catalog import (
    ExactHostVersion,
    ExecutionCatalog,
    InstalledHost,
    RangedHostVersion,
    SupportTier,
    catalog_from_mapping,
)
from enginery.domain.version_range import Version, VersionRange


def test_the_packaged_catalog_binds_the_digest_of_its_exact_bytes() -> None:
    catalog = load_execution_catalog()
    raw = (
        Path(execution_catalog.__file__).parent.parent
        / "config"
        / f"execution-catalog-v{CURRENT_CATALOG_VERSION}.toml"
    ).read_bytes()

    assert catalog.version == CURRENT_CATALOG_VERSION
    assert catalog.digest == Digest.of_bytes(raw)
    assert catalog.digest.hex_value == REGISTERED_CATALOG_DIGESTS[CURRENT_CATALOG_VERSION]


def test_both_hosts_are_experimental_and_none_claims_qualification() -> None:
    catalog = load_execution_catalog()

    assert catalog.harnesses == ("omp", "claude-code")
    assert {host.support_tier for host in catalog.hosts} == {SupportTier.EXPERIMENTAL}


def test_each_host_records_its_protocol_fingerprint_and_rejects_another_protocol() -> None:
    catalog = load_execution_catalog()
    omp = catalog.host("omp")
    claude = catalog.host("claude-code")

    assert omp.protocol_fingerprint == "json-protocol=3"
    assert claude.protocol_fingerprint == "stream-json-schema=1"
    assert omp.matches(InstalledHost("18.10.3", "json-protocol=3"))
    assert not omp.matches(InstalledHost("18.10.3", "json-protocol=4"))
    with pytest.raises(MissingPrerequisiteError, match="native controls are unsupported"):
        omp.require_installed(InstalledHost("18.10.2", "json-protocol=3"))
    with pytest.raises(MissingPrerequisiteError, match="could not be probed"):
        omp.require_installed(None)
    omp.require_installed(InstalledHost("18.10.3", "json-protocol=3"))


def test_the_first_catalog_version_stays_registered_and_frozen_at_its_tested_omp() -> None:
    first = load_execution_catalog("1")
    current = load_execution_catalog()

    assert first.version == "1"
    assert first.host("omp").version == ExactHostVersion("18.10.2")
    assert first.digest.hex_value == (
        "23598e0c044269fddefc073583fddf2432d940626fad6130d2b9e64d805196a3"
    )
    assert first.digest != current.digest
    assert replace(first.host("claude-code"), version=current.host("claude-code").version) == (
        current.host("claude-code")
    )


def test_each_host_maps_controls_to_its_own_native_flags_and_vocabulary() -> None:
    catalog = load_execution_catalog()
    omp = catalog.host("omp")
    claude = catalog.host("claude-code")

    assert (omp.model.flag, omp.effort.flag) == ("--model", "--thinking")
    assert (claude.model.flag, claude.effort.flag) == ("--model", "--effort")
    assert "off" in omp.effort.levels
    assert "off" not in claude.effort.levels
    assert "auto" in omp.effort.unpinnable
    assert omp.effort.levels != claude.effort.levels


def test_the_catalog_lists_no_model_identifiers() -> None:
    raw = (
        Path(execution_catalog.__file__).parent.parent
        / "config"
        / f"execution-catalog-v{CURRENT_CATALOG_VERSION}.toml"
    ).read_text(encoding="utf-8")

    assert "gpt-" not in raw
    assert "claude-opus" not in raw
    assert "claude-sonnet" not in raw


def test_a_catalog_file_edited_without_its_registration_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execution_catalog,
        "REGISTERED_CATALOG_DIGESTS",
        {CURRENT_CATALOG_VERSION: "0" * 64},
    )

    with pytest.raises(ExecutionCatalogError, match="registered digest"):
        load_execution_catalog()


def test_an_unregistered_catalog_version_is_rejected() -> None:
    with pytest.raises(ExecutionCatalogError, match="not registered"):
        load_execution_catalog("99")


def test_a_malformed_host_table_is_rejected() -> None:
    digest = Digest.of_bytes(b"catalog")

    with pytest.raises(InvalidInputError, match="version and a host list"):
        catalog_from_mapping({"hosts": []}, digest=digest)
    with pytest.raises(InvalidInputError, match="support tier is unknown"):
        catalog_from_mapping(
            {
                "catalog_version": "1",
                "hosts": [
                    {
                        "harness": "omp",
                        "executable_version": "1",
                        "protocol_fingerprint": "p",
                        "support_tier": "gold",
                    }
                ],
            },
            digest=digest,
        )


def _host_entry(**version_fields: str) -> dict[str, object]:
    return {
        "harness": "omp",
        "protocol_fingerprint": "p",
        "support_tier": "experimental",
        "model": {
            "flag": "--model",
            "pin_form": "f",
            "pin_pattern": ".*",
            "effective_evidence": "",
        },
        "effort": {"flag": "--e", "levels": ["low"], "unpinnable": [], "effective_evidence": ""},
        **version_fields,
    }


def _decode(entry: dict[str, object]) -> ExecutionCatalog:
    return catalog_from_mapping(
        {"catalog_version": "9", "hosts": [entry]}, digest=Digest.of_bytes(b"catalog")
    )


def test_a_host_entry_decodes_as_exactly_one_of_an_exact_version_or_a_range() -> None:
    exact = _decode(_host_entry(executable_version="18.10.2")).host("omp")
    ranged = _decode(
        _host_entry(executable_versions=">=18.10.3,<19.0.0", tested_executable_version="18.10.3")
    ).host("omp")

    assert exact.version == ExactHostVersion("18.10.2")
    assert ranged.version == RangedHostVersion(
        accepted=VersionRange.parse(">=18.10.3,<19.0.0"), tested=Version.parse("18.10.3")
    )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"executable_version": "18.10.3", "executable_versions": ">=18.10.3,<19.0.0"},
        {"executable_version": "18.10.3", "tested_executable_version": "18.10.3"},
        {"executable_versions": ">=18.10.3,<19.0.0"},
        {"tested_executable_version": "18.10.3"},
    ],
    ids=["neither", "both", "exact-with-tested", "range-without-tested", "tested-without-range"],
)
def test_a_host_entry_naming_both_or_neither_version_form_is_rejected(
    fields: dict[str, str],
) -> None:
    with pytest.raises(InvalidInputError):
        _decode(_host_entry(**fields))


@pytest.mark.parametrize(
    "accepted",
    [
        "18.10.3",
        ">18.10.3,<19.0.0",
        ">=18.10.3,<=19.0.0",
        "<19.0.0,>=18.10.3",
        ">=18.10.3",
        "~=18.10.3",
        ">=18.10.3,<19.0.0-rc.1",
    ],
)
def test_a_catalog_file_with_any_other_range_operator_is_rejected_at_load(accepted: str) -> None:
    with pytest.raises(InvalidInputError):
        _decode(_host_entry(executable_versions=accepted, tested_executable_version="18.10.3"))


def test_a_tested_version_outside_its_accepted_range_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="inside the accepted range"):
        _decode(
            _host_entry(executable_versions=">=18.10.3,<19.0.0", tested_executable_version="19.0.0")
        )
    with pytest.raises(InvalidInputError, match="plain numeric"):
        _decode(
            _host_entry(
                executable_versions=">=18.10.3,<19.0.0", tested_executable_version="18.10.3-rc.1"
            )
        )


@pytest.mark.parametrize(
    ("harness", "accepted", "tested"),
    [
        ("omp", ">=18.10.3,<19.0.0", "18.10.3"),
        ("claude-code", ">=2.1.296,<3.0.0", "2.1.296"),
    ],
)
def test_the_current_catalog_accepts_a_range_and_records_the_tested_version_separately(
    harness: str, accepted: str, tested: str
) -> None:
    host = load_execution_catalog().host(harness)

    assert host.version == RangedHostVersion(
        accepted=VersionRange.parse(accepted), tested=Version.parse(tested)
    )
    assert host.support_tier is SupportTier.EXPERIMENTAL


@pytest.mark.parametrize(
    ("harness", "fingerprint", "matching", "holding"),
    [
        (
            "omp",
            "json-protocol=3",
            ["18.10.3", "18.11.0", "18.99.99"],
            [
                "18.10.2",
                "19.0.0",
                "19.0.1",
                "18.11.0-beta.1",
                "18.11.0+build.1",
                "18.11",
                "v18.11.0",
            ],
        ),
        (
            "claude-code",
            "stream-json-schema=1",
            ["2.1.296", "2.1.300", "2.10.0"],
            ["2.1.295", "3.0.0", "2.1.300-beta.1", "2.1.300 (Claude Code)", "garbage"],
        ),
    ],
)
def test_the_current_catalog_matches_inside_the_range_and_rejects_everything_else(
    harness: str, fingerprint: str, matching: list[str], holding: list[str]
) -> None:
    host = load_execution_catalog().host(harness)

    for version in matching:
        assert host.matches(InstalledHost(version, fingerprint)), version
        host.require_installed(InstalledHost(version, fingerprint))
    for version in holding:
        assert not host.matches(InstalledHost(version, fingerprint)), version
        with pytest.raises(MissingPrerequisiteError, match="native controls are unsupported"):
            host.require_installed(InstalledHost(version, fingerprint))
    assert not host.matches(InstalledHost(matching[1], "other-protocol=1"))


@pytest.mark.parametrize(
    ("harness", "fingerprint", "tested", "untested"),
    [
        ("omp", "json-protocol=3", "18.10.3", "18.10.4"),
        ("claude-code", "stream-json-schema=1", "2.1.296", "2.1.297"),
    ],
)
def test_a_pinned_image_matches_only_the_tested_version_not_the_whole_range(
    harness: str, fingerprint: str, tested: str, untested: str
) -> None:
    host = load_execution_catalog().host(harness)

    assert host.tested_executable_version == tested
    assert host.matches_tested(InstalledHost(tested, fingerprint))
    assert host.matches(InstalledHost(untested, fingerprint))  # accepted, never tested
    assert not host.matches_tested(InstalledHost(untested, fingerprint))
    assert not host.matches_tested(InstalledHost(tested, "other-protocol=1"))
    assert not host.matches_tested(InstalledHost("garbage", fingerprint))


def test_an_exact_frozen_host_reports_its_one_version_as_the_tested_one() -> None:
    second = load_execution_catalog("2").host("omp")

    assert second.tested_executable_version == "18.10.3"
    assert second.matches_tested(InstalledHost("18.10.3", "json-protocol=3"))
    assert not second.matches_tested(InstalledHost("18.10.4", "json-protocol=3"))


def test_a_version_rejection_names_the_accepted_range_and_the_tested_version() -> None:
    host = load_execution_catalog().host("omp")

    with pytest.raises(MissingPrerequisiteError) as raised:
        host.require_installed(InstalledHost("19.0.0", "json-protocol=3"))

    assert raised.value.details["accepted_executable_versions"] == ">=18.10.3,<19.0.0"
    assert raised.value.details["tested_executable_version"] == "18.10.3"
    assert raised.value.details["installed_executable_version"] == "19.0.0"


def test_the_second_catalog_stays_frozen_at_its_exact_versions() -> None:
    second = load_execution_catalog("2")

    assert second.version == "2"
    assert second.digest.hex_value == (
        "de74499de75b1bcf9e06ebce0932d3944c5dda9e5bee7c3eaa8dfd2769eb3653"
    )
    assert second.host("omp").version == ExactHostVersion("18.10.3")
    assert second.host("claude-code").version == ExactHostVersion("2.1.296")
    assert not second.host("omp").matches(InstalledHost("18.11.0", "json-protocol=3"))
    assert not second.host("claude-code").matches(InstalledHost("2.1.300", "stream-json-schema=1"))


def test_the_third_catalog_digest_is_pinned_to_its_literal_bytes_hash() -> None:
    third = load_execution_catalog("3")

    assert third.digest.hex_value == (
        "7e8b15191bf283cf5721d8d5384a8391191874a4bdc6754fec73b8f1ddc6c105"
    )
