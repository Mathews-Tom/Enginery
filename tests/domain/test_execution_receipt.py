from __future__ import annotations

from dataclasses import replace

import pytest

from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.execution_receipt import (
    EffectiveControl,
    EffectiveState,
    ExecutionReceipt,
    evaluate_effective_requirement,
)
from enginery.domain.execution_selection import (
    HoldReason,
    detect_host_drift,
    validate_against_catalog,
)
from tests.support.execution import selection_for

MODEL = "openai/gpt-5.2"


def _receipt(
    selection_digest: Digest | None,
    *,
    model: EffectiveControl | None = None,
    effort: EffectiveControl | None = None,
) -> ExecutionReceipt:
    return ExecutionReceipt(
        selection_digest=selection_digest,
        harness="omp",
        submitted_arguments=("--model", MODEL),
        effective_model=model or EffectiveControl.unknown(reason="not reported"),
        effective_effort=effort or EffectiveControl.unknown(reason="not reported"),
    )


def test_an_effective_control_is_observed_with_evidence_or_explicitly_unknown() -> None:
    assert EffectiveControl.observed(MODEL, source="event").state is EffectiveState.OBSERVED
    unknown = EffectiveControl.unknown(reason="host does not report it")
    assert unknown.state is EffectiveState.UNKNOWN and unknown.value is None
    with pytest.raises(InvalidInputError, match="requires a value"):
        EffectiveControl(state=EffectiveState.OBSERVED, value=None, source="event")
    with pytest.raises(InvalidInputError, match="cannot carry a value"):
        EffectiveControl(state=EffectiveState.UNKNOWN, value=MODEL, source="event")
    with pytest.raises(InvalidInputError, match="evidence or its reason"):
        EffectiveControl.unknown(reason=" ")


def test_a_receipt_round_trips_and_rejects_an_unknown_schema_version() -> None:
    selection = selection_for("omp", model=MODEL)
    receipt = _receipt(
        selection.digest, model=EffectiveControl.observed(MODEL, source="message_end")
    )

    assert ExecutionReceipt.from_state(receipt.to_state()) == receipt
    legacy = _receipt(None)
    assert ExecutionReceipt.from_state(legacy.to_state()) == legacy
    state = receipt.to_state()
    state["schema_version"] = 9
    with pytest.raises(InvalidInputError, match="schema version is unsupported"):
        ExecutionReceipt.from_state(state)


def test_without_a_strict_requirement_nothing_is_demanded_of_the_receipt() -> None:
    selection = selection_for("omp", model=MODEL)

    assert evaluate_effective_requirement(selection, _receipt(selection.digest)) is None
    assert evaluate_effective_requirement(selection, None) is None


def test_a_strict_requirement_with_an_unknown_effective_model_holds() -> None:
    selection = selection_for("omp", model=MODEL, strict_effective=True)

    hold = evaluate_effective_requirement(selection, _receipt(selection.digest))

    assert hold is not None
    assert hold.reason is HoldReason.EFFECTIVE_CONTROL_UNVERIFIED
    assert hold.details["control"] == "model"


def test_a_strict_requirement_with_no_receipt_holds_like_an_unknown_one() -> None:
    selection = selection_for("omp", model=MODEL, strict_effective=True)

    hold = evaluate_effective_requirement(selection, None)

    assert hold is not None
    assert hold.reason is HoldReason.EFFECTIVE_CONTROL_UNVERIFIED


def test_a_strict_requirement_passes_only_for_an_observed_exact_match() -> None:
    selection = selection_for("omp", model=MODEL, strict_effective=True)

    matching = _receipt(
        selection.digest, model=EffectiveControl.observed(MODEL, source="message_end")
    )
    differing = _receipt(
        selection.digest, model=EffectiveControl.observed("openai/gpt-5.3", source="message_end")
    )

    assert evaluate_effective_requirement(selection, matching) is None
    hold = evaluate_effective_requirement(selection, differing)
    assert hold is not None and hold.reason is HoldReason.EFFECTIVE_CONTROL_MISMATCH


def test_requested_values_are_never_a_substitute_for_an_effective_one() -> None:
    selection = selection_for("omp", model=MODEL, effort="high", strict_effective=True)
    model_only = _receipt(
        selection.digest, model=EffectiveControl.observed(MODEL, source="message_end")
    )

    hold = evaluate_effective_requirement(selection, model_only)

    assert hold is not None
    assert hold.details["control"] == "effort"
    assert hold.reason is HoldReason.EFFECTIVE_CONTROL_UNVERIFIED


def test_a_receipt_for_a_different_selection_does_not_satisfy_a_strict_requirement() -> None:
    selection = selection_for("omp", model=MODEL, strict_effective=True)
    other = selection_for("omp", model="openai/gpt-5.3", strict_effective=True)
    receipt = _receipt(other.digest, model=EffectiveControl.observed(MODEL, source="message_end"))

    hold = evaluate_effective_requirement(selection, receipt)

    assert hold is not None and hold.reason is HoldReason.EFFECTIVE_CONTROL_MISMATCH


def test_host_drift_holds_only_a_selection_that_needs_native_controls() -> None:
    pinned = selection_for("omp", model=MODEL)
    default = selection_for("omp")
    bound = InstalledHost("18.10.2", "json-protocol=3")

    assert detect_host_drift(pinned, installed=bound) is None
    assert detect_host_drift(default, installed=InstalledHost("9.9.9", "other")) is None
    for installed in (InstalledHost("18.10.3", "json-protocol=3"), None):
        hold = detect_host_drift(pinned, installed=installed)
        assert hold is not None
        assert hold.reason is HoldReason.HOST_FINGERPRINT_DRIFT
        assert hold.details["bound_executable_version"] == "18.10.2"


def test_validation_against_the_catalog_rejects_forged_tier_fingerprint_or_catalog() -> None:
    catalog = load_execution_catalog()
    pinned = selection_for("omp", model=MODEL)

    assert validate_against_catalog(pinned, catalog=catalog).harness == "omp"
    with pytest.raises(InvalidInputError, match="tested host entry"):
        validate_against_catalog(replace(pinned, host_executable_version="1.0"), catalog=catalog)
    with pytest.raises(InvalidInputError, match="different catalog"):
        validate_against_catalog(
            replace(pinned, catalog_digest=Digest.of_bytes(b"x")), catalog=catalog
        )
