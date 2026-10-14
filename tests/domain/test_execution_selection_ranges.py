"""Range-bound execution selections: what they record, how they match, and when they hold."""

from __future__ import annotations

from dataclasses import replace

import pytest

from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError
from enginery.domain.execution_catalog import ExactHostVersion, InstalledHost, RangedHostVersion
from enginery.domain.execution_selection import (
    ExecutionSelection,
    HoldReason,
    LayerControls,
    SelectionLayer,
    detect_host_drift,
    resolve_execution_selection,
    validate_against_catalog,
)
from enginery.domain.version_range import Version, VersionRange

OMP_MODEL = "openai/gpt-5.2"
CLAUDE_MODEL = "claude-opus-4-8"
OMP = "json-protocol=3"
CLAUDE = "stream-json-schema=1"


def _resolve(
    harness: str = "omp",
    *,
    model: str | None = None,
    probe: InstalledHost | None = None,
    catalog_version: str | None = None,
    calls: list[str] | None = None,
) -> ExecutionSelection:
    def probe_host(name: str) -> InstalledHost | None:
        if calls is not None:
            calls.append(name)
        return probe

    return resolve_execution_selection(
        (
            LayerControls(
                layer=SelectionLayer.RUN,
                harness=harness,
                model=model,
                model_pinned=model is not None,
            ),
        ),
        catalog=load_execution_catalog(catalog_version or "3"),
        probe_host=probe_host,
    )


def test_exact_bound_selections_keep_their_schema_and_digests_byte_for_byte() -> None:
    omp = _resolve("omp", model=OMP_MODEL, catalog_version="2")
    claude = _resolve("claude-code", model=CLAUDE_MODEL, catalog_version="2")

    assert omp.to_state()["schema_version"] == 1
    assert omp.to_state()["host"] == {
        "executable_version": "18.10.3",
        "protocol_fingerprint": OMP,
        "support_tier": "experimental",
    }
    assert str(omp.digest) == (
        "sha256:74ad3d2d62471fcf09bfbfd123bfcaf5b03def0199c5b263e2d5abc4ff157119"
    )
    assert str(claude.digest) == (
        "sha256:d414eb815f9ddcb26b69738281cd2376c7f50eec9f6fcfc7ea73ab5bce2544e4"
    )
    assert ExecutionSelection.from_state(omp.to_state()) == omp
    assert str(ExecutionSelection.legacy_host_default("omp").digest) == (
        "sha256:321b295d1b0d6cc0f16bfb610a11df803ec6a82c7603eff3b3bb20dec9bca9b3"
    )


def test_an_exact_bound_selection_records_no_observation_even_when_a_probe_is_supplied() -> None:
    selection = _resolve(
        "omp", model=OMP_MODEL, catalog_version="2", probe=InstalledHost("18.10.3", OMP)
    )

    assert selection.observed_host_version is None


def test_a_range_bound_selection_records_the_range_the_tested_version_and_the_observed_one() -> (
    None
):
    selection = _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.0", OMP))

    assert selection.host_version == RangedHostVersion(
        accepted=VersionRange.parse(">=18.10.3,<19.0.0"), tested=Version.parse("18.10.3")
    )
    assert selection.observed_host_version == "18.11.0"
    assert selection.to_state()["schema_version"] == 2
    assert selection.to_state()["host"] == {
        "executable_versions": ">=18.10.3,<19.0.0",
        "tested_executable_version": "18.10.3",
        "observed_executable_version": "18.11.0",
        "protocol_fingerprint": OMP,
        "support_tier": "experimental",
    }
    assert ExecutionSelection.from_state(selection.to_state()) == selection


def test_the_observed_version_is_part_of_the_selection_identity() -> None:
    first = _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.0", OMP))
    second = _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.1", OMP))
    unobserved = _resolve("omp", model=OMP_MODEL)

    assert len({first.digest, second.digest, unobserved.digest}) == 3


@pytest.mark.parametrize(
    ("harness", "model", "installed"),
    [("omp", OMP_MODEL, "18.11.0"), ("claude-code", CLAUDE_MODEL, "2.1.300")],
)
def test_the_observed_version_is_never_reported_as_the_tested_version(
    harness: str, model: str, installed: str
) -> None:
    protocol = OMP if harness == "omp" else CLAUDE
    selection = _resolve(harness, model=model, probe=InstalledHost(installed, protocol))

    assert isinstance(selection.host_version, RangedHostVersion)
    assert selection.observed_host_version == installed
    assert str(selection.host_version.tested) != installed


def test_a_probe_is_made_only_for_a_selection_that_needs_native_controls() -> None:
    calls: list[str] = []

    host_default = _resolve("omp", probe=InstalledHost("18.11.0", OMP), calls=calls)
    assert calls == []
    assert host_default.observed_host_version is None

    _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.0", OMP), calls=calls)
    assert calls == ["omp"]


def test_an_unobservable_host_records_no_observation_and_is_left_to_the_launch_check() -> None:
    assert _resolve("omp", model=OMP_MODEL, probe=None).observed_host_version is None


@pytest.mark.parametrize(
    ("harness", "model", "banner", "protocol"),
    [
        ("omp", OMP_MODEL, "19.0.0", OMP),
        ("omp", OMP_MODEL, "18.10.2", OMP),
        ("omp", OMP_MODEL, "18.11.0-beta.1", OMP),
        ("omp", OMP_MODEL, "18.11.0", "json-protocol=4"),
        ("claude-code", CLAUDE_MODEL, "3.0.0", CLAUDE),
        ("claude-code", CLAUDE_MODEL, "2.1.295", CLAUDE),
        ("claude-code", CLAUDE_MODEL, "2.1.300-beta.1", CLAUDE),
    ],
)
def test_an_observed_host_outside_the_catalog_is_rejected_at_bind_time(
    harness: str, model: str, banner: str, protocol: str
) -> None:
    with pytest.raises(MissingPrerequisiteError, match="native controls are unsupported"):
        _resolve(harness, model=model, probe=InstalledHost(banner, protocol))


def test_an_observed_version_must_be_inside_the_bound_range_and_only_for_a_range() -> None:
    ranged = _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.0", OMP))
    exact = _resolve("omp", model=OMP_MODEL, catalog_version="2")

    with pytest.raises(InvalidInputError, match="inside the bound range"):
        replace(ranged, observed_host_version="19.0.0")
    with pytest.raises(InvalidInputError, match="inside the bound range"):
        replace(ranged, observed_host_version="18.11.0-beta.1")
    with pytest.raises(InvalidInputError, match="only for a range-bound"):
        replace(exact, observed_host_version="18.10.3")
    with pytest.raises(InvalidInputError, match="bare host default"):
        replace(ExecutionSelection.legacy_host_default("omp"), observed_host_version="18.10.3")


def test_a_persisted_selection_must_match_the_shape_of_its_schema_version() -> None:
    ranged = _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.0", OMP)).to_state()
    exact = _resolve("omp", model=OMP_MODEL, catalog_version="2").to_state()

    ranged_as_exact = {**ranged, "schema_version": 1}
    exact_as_ranged = {**exact, "schema_version": 2}
    for state in (ranged_as_exact, exact_as_ranged):
        with pytest.raises(InvalidInputError):
            ExecutionSelection.from_state(state)
    with pytest.raises(InvalidInputError, match="schema version is unsupported"):
        ExecutionSelection.from_state({**ranged, "schema_version": 3})


def test_a_persisted_range_must_be_a_plain_numeric_range_with_a_tested_version_inside_it() -> None:
    state = _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.0", OMP)).to_state()
    host = state["host"]
    assert isinstance(host, dict)

    for field, value in (
        ("executable_versions", ">=18.10.3,<=19.0.0"),
        ("executable_versions", "18.10.3"),
        ("tested_executable_version", "19.0.0"),
        ("tested_executable_version", "18.10.3-rc.1"),
    ):
        with pytest.raises(InvalidInputError):
            ExecutionSelection.from_state({**state, "host": {**host, field: value}})


def test_validation_against_the_catalog_rejects_a_forged_range_or_tested_version() -> None:
    catalog = load_execution_catalog()
    selection = _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.0", OMP))
    assert isinstance(selection.host_version, RangedHostVersion)

    assert validate_against_catalog(selection, catalog=catalog).harness == "omp"
    widened = replace(selection.host_version, accepted=VersionRange.parse(">=18.10.3,<20.0.0"))
    retested = replace(selection.host_version, tested=Version.parse("18.11.0"))
    for forged in (widened, retested, ExactHostVersion("18.11.0")):
        with pytest.raises(InvalidInputError, match="tested host entry"):
            validate_against_catalog(
                replace(selection, host_version=forged, observed_host_version=None),
                catalog=catalog,
            )
    with pytest.raises(InvalidInputError, match="different catalog"):
        validate_against_catalog(
            replace(selection, catalog_digest=Digest.of_bytes(b"x")), catalog=catalog
        )


@pytest.mark.parametrize(
    ("harness", "model", "protocol", "in_range"),
    [
        ("omp", OMP_MODEL, OMP, ["18.10.3", "18.10.9", "18.11.0", "18.99.99"]),
        ("claude-code", CLAUDE_MODEL, CLAUDE, ["2.1.296", "2.1.300", "2.9.0"]),
    ],
)
def test_a_version_change_inside_the_bound_range_is_not_a_host_drift_hold(
    harness: str, model: str, protocol: str, in_range: list[str]
) -> None:
    selection = _resolve(harness, model=model, probe=InstalledHost(in_range[0], protocol))

    for version in in_range:
        assert detect_host_drift(selection, installed=InstalledHost(version, protocol)) is None


@pytest.mark.parametrize(
    ("harness", "model", "protocol", "outside"),
    [
        ("omp", OMP_MODEL, OMP, ["18.10.2", "19.0.0", "19.0.1", "18.11.0-beta.1", "18.11.0+b.2"]),
        ("claude-code", CLAUDE_MODEL, CLAUDE, ["2.1.295", "3.0.0", "2.1.300-beta.1", "garbage"]),
    ],
)
def test_a_version_outside_the_bound_range_or_not_plain_numeric_holds(
    harness: str, model: str, protocol: str, outside: list[str]
) -> None:
    selection = _resolve(harness, model=model)

    for version in outside:
        hold = detect_host_drift(selection, installed=InstalledHost(version, protocol))
        assert hold is not None, version
        assert hold.reason is HoldReason.HOST_FINGERPRINT_DRIFT
        assert hold.details["installed_executable_version"] == version
        assert hold.details["bound_executable_versions"] in (
            ">=18.10.3,<19.0.0",
            ">=2.1.296,<3.0.0",
        )
        assert hold.details["bound_tested_executable_version"] in ("18.10.3", "2.1.296")


def test_a_protocol_change_or_an_unobservable_host_holds_even_inside_the_range() -> None:
    selection = _resolve("omp", model=OMP_MODEL, probe=InstalledHost("18.11.0", OMP))

    protocol = detect_host_drift(selection, installed=InstalledHost("18.11.0", "json-protocol=4"))
    unobservable = detect_host_drift(selection, installed=None)

    assert protocol is not None and protocol.reason is HoldReason.HOST_FINGERPRINT_DRIFT
    assert unobservable is not None
    assert unobservable.details["installed_executable_version"] == "unobservable"


def test_a_host_default_selection_never_holds_on_the_installed_version() -> None:
    selection = _resolve("omp")

    assert detect_host_drift(selection, installed=InstalledHost("99.0.0", "other")) is None
    assert detect_host_drift(selection, installed=None) is None


def test_an_exact_bound_selection_holds_on_any_difference_from_its_bound_version() -> None:
    selection = _resolve("omp", model=OMP_MODEL, catalog_version="2")

    assert detect_host_drift(selection, installed=InstalledHost("18.10.3", OMP)) is None
    for version in ("18.10.2", "18.10.4", "18.11.0"):
        hold = detect_host_drift(selection, installed=InstalledHost(version, OMP))
        assert hold is not None
        assert hold.details["bound_executable_version"] == "18.10.3"
