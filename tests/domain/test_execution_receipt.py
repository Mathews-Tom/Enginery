from __future__ import annotations

from dataclasses import replace

import pytest

from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import ExactHostVersion, InstalledHost, RangedHostVersion
from enginery.domain.execution_receipt import (
    EffectiveControl,
    EffectiveState,
    ExecutionReceipt,
    evaluate_effective_requirement,
)
from enginery.domain.execution_selection import (
    HoldReason,
    detect_catalog_drift,
    detect_host_drift,
    validate_against_catalog,
)
from enginery.domain.version_range import Version, VersionRange
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
    selection = replace(selection_for("omp", model=MODEL, effort="high"), strict_effective=True)
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
    bound = InstalledHost("18.10.3", "json-protocol=3")

    assert detect_host_drift(pinned, installed=bound) is None
    assert detect_host_drift(default, installed=InstalledHost("9.9.9", "other")) is None
    for installed in (InstalledHost("18.10.2", "json-protocol=3"), None):
        hold = detect_host_drift(pinned, installed=installed)
        assert hold is not None
        assert hold.reason is HoldReason.HOST_FINGERPRINT_DRIFT
        assert hold.details["bound_executable_versions"] == ">=18.10.3,<19.0.0"
        assert hold.details["bound_tested_executable_version"] == "18.10.3"


def test_a_run_bound_to_the_first_catalog_holds_against_the_omp_the_current_catalog_names() -> None:
    bound = selection_for("omp", model=MODEL, catalog=load_execution_catalog("1"))

    host_hold = detect_host_drift(bound, installed=InstalledHost("18.10.3", "json-protocol=3"))
    catalog_hold = detect_catalog_drift(bound, current=load_execution_catalog())

    assert host_hold is not None
    assert host_hold.reason is HoldReason.HOST_FINGERPRINT_DRIFT
    assert host_hold.details["bound_executable_version"] == "18.10.2"
    assert host_hold.details["installed_executable_version"] == "18.10.3"
    assert catalog_hold is not None
    assert catalog_hold.reason is HoldReason.CATALOG_DRIFT
    assert catalog_hold.details["bound_catalog_version"] == "1"
    assert catalog_hold.details["current_catalog_version"] == "3"


def test_validation_against_the_catalog_rejects_forged_tier_fingerprint_or_catalog() -> None:
    catalog = load_execution_catalog()
    pinned = selection_for("omp", model=MODEL)

    assert validate_against_catalog(pinned, catalog=catalog).harness == "omp"
    with pytest.raises(InvalidInputError, match="tested host entry"):
        validate_against_catalog(
            replace(pinned, host_version=ExactHostVersion("1.0")), catalog=catalog
        )
    widened = RangedHostVersion(
        accepted=VersionRange.parse(">=18.0.0,<20.0.0"), tested=Version.parse("18.10.3")
    )
    with pytest.raises(InvalidInputError, match="tested host entry"):
        validate_against_catalog(replace(pinned, host_version=widened), catalog=catalog)
    with pytest.raises(InvalidInputError, match="different catalog"):
        validate_against_catalog(
            replace(pinned, catalog_digest=Digest.of_bytes(b"x")), catalog=catalog
        )


def test_a_receipt_without_a_runner_keeps_the_pre_runner_shape_and_decodes_unchanged() -> None:
    receipt = _receipt(None)

    state = receipt.to_state()

    assert set(state) == {
        "schema_version",
        "selection_digest",
        "harness",
        "submitted_arguments",
        "effective_model",
        "effective_effort",
        "host",
    }
    assert ExecutionReceipt.from_state(state).runner is None


def test_a_receipt_records_and_round_trips_the_runner_receipt_it_ran_under() -> None:
    from enginery.domain.execution_receipt import RunnerReceiptRef

    ref = RunnerReceiptRef(
        trust_mode="isolated-oci",
        receipt_digest=Digest.of_bytes(b"runner receipt"),
        image_id="sha256:" + "a" * 64,
        profile_digest=Digest.of_bytes(b"profile"),
    )

    decoded = ExecutionReceipt.from_state(replace(_receipt(None), runner=ref).to_state())

    assert decoded.runner == ref


@pytest.mark.parametrize(
    "state",
    [
        "not a mapping",
        {"trust_mode": "isolated-oci", "image_id": "sha256:" + "a" * 64},
        {
            "trust_mode": "isolated-oci",
            "receipt_digest": "not-a-digest",
            "image_id": "sha256:" + "a" * 64,
            "profile_digest": str(Digest.of_bytes(b"p")),
        },
        {
            "trust_mode": 7,
            "receipt_digest": str(Digest.of_bytes(b"r")),
            "image_id": "sha256:" + "a" * 64,
            "profile_digest": str(Digest.of_bytes(b"p")),
        },
    ],
)
def test_a_malformed_runner_receipt_reference_is_rejected(state: object) -> None:
    from enginery.domain.execution_receipt import RunnerReceiptRef

    with pytest.raises(InvalidInputError):
        RunnerReceiptRef.from_state(state)


_IMAGE = "sha256:" + "a" * 64


def _reference(**overrides: str) -> dict[str, str]:
    state = {
        "trust_mode": "isolated-oci",
        "receipt_digest": str(Digest.of_bytes(b"r")),
        "image_id": _IMAGE,
        "profile_digest": str(Digest.of_bytes(b"p")),
    }
    state.update(overrides)
    return state


@pytest.mark.parametrize("trust_mode", ["trusted-local", "isolated-oci"])
def test_a_runner_receipt_reference_accepts_every_trust_mode_value(trust_mode: str) -> None:
    from enginery.domain.execution_receipt import RunnerReceiptRef

    assert RunnerReceiptRef.from_state(_reference(trust_mode=trust_mode)).trust_mode == trust_mode


@pytest.mark.parametrize("trust_mode", ["mystery", "Isolated-OCI", " isolated-oci", ""])
def test_a_runner_receipt_reference_refuses_a_trust_mode_that_is_not_a_trust_mode(
    trust_mode: str,
) -> None:
    from enginery.domain.execution_receipt import RunnerReceiptRef

    with pytest.raises(InvalidInputError, match="trust mode"):
        RunnerReceiptRef.from_state(_reference(trust_mode=trust_mode))


@pytest.mark.parametrize(
    "image_id",
    ["latest", "sha256:abc", "sha256:" + "A" * 64, "sha256:" + "a" * 65, "sha1:" + "a" * 64, " "],
)
def test_a_runner_receipt_reference_refuses_an_image_id_that_is_not_an_exact_sha256_id(
    image_id: str,
) -> None:
    from enginery.domain.execution_receipt import RunnerReceiptRef

    with pytest.raises(InvalidInputError, match="image id"):
        RunnerReceiptRef.from_state(_reference(image_id=image_id))


def test_a_worker_reported_model_is_labelled_and_the_label_round_trips() -> None:
    observed = EffectiveControl.observed(MODEL, source="message_end.message.model")

    labelled = observed.worker_reported()

    assert labelled.reported_by == "worker"
    assert labelled.value == MODEL and labelled.source == observed.source
    assert EffectiveControl.from_state(labelled.to_state(), name="effective_model") == labelled
    assert labelled.to_state()["reported_by"] == "worker"


def test_the_worker_reported_label_is_omitted_by_default_so_legacy_receipts_are_unchanged() -> None:
    observed = EffectiveControl.observed(MODEL, source="evidence")
    legacy_state = {"state": "observed", "value": MODEL, "source": "evidence"}

    assert observed.to_state() == legacy_state
    decoded = EffectiveControl.from_state(legacy_state, name="effective_model")
    assert decoded == observed and decoded.reported_by is None
    legacy = replace(_receipt(None), effective_model=observed)
    assert "reported_by" not in legacy.to_state()["effective_model"]  # type: ignore[operator]
    assert ExecutionReceipt.from_state(legacy.to_state()) == legacy


def test_an_unknown_control_asserts_nothing_so_it_is_never_labelled() -> None:
    unknown = EffectiveControl.unknown(reason="no event reported a model")

    assert unknown.worker_reported() is unknown
    with pytest.raises(InvalidInputError, match="observed"):
        EffectiveControl(state=unknown.state, value=None, source="reason", reported_by="worker")


@pytest.mark.parametrize("reported_by", ["", "  ", 7])
def test_a_malformed_reported_by_is_refused(reported_by: object) -> None:
    state = {"state": "observed", "value": MODEL, "source": "evidence", "reported_by": reported_by}

    with pytest.raises(InvalidInputError):
        EffectiveControl.from_state(state, name="effective_model")


def test_a_worker_reported_model_still_satisfies_an_exact_strict_match() -> None:
    selection = selection_for("omp", model=MODEL, strict_effective=True)
    receipt = _receipt(
        selection.digest,
        model=EffectiveControl.observed(MODEL, source="evidence").worker_reported(),
    )

    assert evaluate_effective_requirement(selection, receipt) is None
