from __future__ import annotations

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
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import SupportTier, catalog_from_mapping


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


def test_each_host_records_its_tested_executable_and_protocol_fingerprint() -> None:
    catalog = load_execution_catalog()
    omp = catalog.host("omp")
    claude = catalog.host("claude-code")

    assert (omp.executable_version, omp.protocol_fingerprint) == ("18.10.2", "json-protocol=3")
    assert (claude.executable_version, claude.protocol_fingerprint) == (
        "2.1.296",
        "stream-json-schema=1",
    )
    assert omp.matches_installed(
        executable_version="18.10.2", protocol_fingerprint="json-protocol=3"
    )
    assert not omp.matches_installed(
        executable_version="18.10.3", protocol_fingerprint="json-protocol=3"
    )
    assert not omp.matches_installed(
        executable_version="18.10.2", protocol_fingerprint="json-protocol=4"
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
        Path(execution_catalog.__file__).parent.parent / "config" / "execution-catalog-v1.toml"
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
