"""Pure semantic evaluation over current, captured instrument observations.

No transport or filesystem access belongs here. A receipt records provenance; only
this evaluator computes an acceptance result from the custody-owned assertions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from enginery.application.behavioral_calibration import (
    CALIBRATION_DRIVER,
    AcceptanceInputs,
    CalibrationClassification,
    CalibrationReceipt,
    CalibrationRung,
)
from enginery.application.behavioral_checks import (
    EVALUATOR_VERSION,
    BehavioralAssertion,
    BehavioralCheck,
    FaultClass,
)
from enginery.application.behavioral_observation import ObservationData
from enginery.domain.behavioral_evidence import (
    MAX_EVIDENCE_BYTES,
    AssertionOutcome,
    DecodeStatus,
    EvidenceReceipt,
    ObserverKind,
    ReceiptCategory,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.node_attempt import EvidenceResult


@dataclass(frozen=True, slots=True)
class ObservedCheck:
    check_name: str
    data: ObservationData
    receipt: EvidenceReceipt


@dataclass(frozen=True, slots=True)
class BehavioralEvaluation:
    result: EvidenceResult
    assertion_outcomes: tuple[AssertionOutcome, ...]
    holds: tuple[str, ...]


def canonical_bytes(payload: object) -> bytes:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if len(data) > MAX_EVIDENCE_BYTES:
        raise InvalidInputError("behavioral evidence document exceeds the 1 MiB limit")
    return data


def observation_payload(data: ObservationData) -> dict[str, object]:
    """The exact semantic inputs captured alongside, not instead of, raw media."""
    return {
        "observer": data.observer,
        "returncode": data.returncode,
        "stdout": data.stdout,
        "stderr": data.stderr,
        "body": data.body,
        "produced_files": [[path, content] for path, content in data.produced_files],
        "dom_results": [[index, result] for index, result in data.dom_results],
        "captures": [
            {
                "media_type": media_type,
                "digest": str(Digest.of_bytes(content)),
                "size_bytes": len(content),
            }
            for media_type, content in data.captures
        ],
        "started_at": data.started_at,
        "ended_at": data.ended_at,
        "infrastructure_error": data.infrastructure_error,
        "mock": data.mock,
        "ran": data.ran,
        "skipped": data.skipped,
        "runner_receipt_digest": (
            None if data.runner_receipt_digest is None else str(data.runner_receipt_digest)
        ),
    }


def assertion_id(check: BehavioralCheck, index: int) -> str:
    return f"{check.name}:{index}"


def fault_applies(assertion: BehavioralAssertion, fault: FaultClass) -> bool:
    if fault == "wrong_build":
        return True
    if fault == "skipped_persistence":
        return assertion.kind == "produced_file" and not (
            assertion.operator == "exists" and assertion.expected is False
        )
    return assertion.kind in {"exit_status", "stdout", "stderr", "body", "dom"}


def evaluate_assertions(
    check: BehavioralCheck,
    data: ObservationData,
) -> tuple[AssertionOutcome, ...]:
    """Compute each comparison; absent instrumentation is indeterminate, not a pass."""
    available = (
        data.ran
        and not data.skipped
        and not data.mock
        and data.infrastructure_error is None
        and data.observer == check.observer
        and len(dict(data.produced_files)) == len(data.produced_files)
        and len(dict(data.dom_results)) == len(data.dom_results)
    )
    outcomes: list[AssertionOutcome] = []
    for index, assertion in enumerate(check.assertions):
        result = EvidenceResult.INDETERMINATE
        ran = available
        if available:
            present = True
            value: str | int | bool | None = None
            match assertion.kind:
                case "exit_status":
                    value = data.returncode
                    present = value is not None
                case "stdout":
                    value = data.stdout
                case "stderr":
                    value = data.stderr
                case "body":
                    value = data.body
                case "produced_file":
                    files = dict(data.produced_files)
                    present = assertion.path in files
                    value = files.get(assertion.path or "")
                case "dom":
                    comparisons = dict(data.dom_results)
                    present = index in comparisons
                    value = comparisons.get(index)
            if assertion.kind == "dom":
                result = (
                    (EvidenceResult.PASS if value is True else EvidenceResult.FAIL)
                    if present
                    else EvidenceResult.INDETERMINATE
                )
                ran = present
            elif assertion.operator == "exists":
                result = (
                    EvidenceResult.PASS if present == assertion.expected else EvidenceResult.FAIL
                )
            elif not present:
                result = (
                    EvidenceResult.FAIL
                    if assertion.kind == "produced_file"
                    else EvidenceResult.INDETERMINATE
                )
                ran = assertion.kind == "produced_file"
            elif assertion.operator == "equals":
                result = EvidenceResult.PASS if value == assertion.expected else EvidenceResult.FAIL
            else:
                result = (
                    EvidenceResult.PASS
                    if isinstance(value, str)
                    and isinstance(assertion.expected, str)
                    and assertion.expected in value
                    else EvidenceResult.FAIL
                )
        outcomes.append(
            AssertionOutcome(
                assertion_id(check, index),
                result,
                ran=ran,
                skipped=data.skipped,
            )
        )
    return tuple(outcomes)


def classify_calibration(
    *,
    applied: bool,
    baseline_result: EvidenceResult | None,
    fault_result: EvidenceResult | None = None,
    infrastructure_error: str | None = None,
    binding_rejected: bool = False,
    binding_accepted: bool = False,
    observer_rejected: bool = False,
) -> tuple[CalibrationClassification, CalibrationRung | None]:
    """Attribute only demonstrated catches, never infrastructure or unapplied faults."""
    if not applied:
        return "not_injected", None
    if infrastructure_error is not None or baseline_result is not EvidenceResult.PASS:
        return "inconclusive", None
    if binding_rejected:
        return "caught", "binding_identity"
    if binding_accepted:
        return "escaped", None
    if observer_rejected:
        return "caught", "observer"
    if fault_result is EvidenceResult.FAIL:
        return "caught", "evaluator"
    if fault_result is EvidenceResult.PASS:
        return "escaped", None
    return "inconclusive", None


def calibration_holds(
    checks: tuple[BehavioralCheck, ...],
    inputs: AcceptanceInputs,
    calibration: CalibrationReceipt | None,
) -> tuple[str, ...]:
    if calibration is None:
        return ("behavioral_calibration_required",)
    receipt = calibration.receipt
    if (
        not inputs.matches(receipt)
        or receipt.category is not ReceiptCategory.INSTRUMENT_OBSERVATION
        or receipt.observer.identity != CALIBRATION_DRIVER
        or receipt.observer.kind is not ObserverKind.COMMAND
        or receipt.observer.mock
        or receipt.observer.version != inputs.evaluator.version
    ):
        return ("behavioral_calibration_stale",)
    captures = {item.digest for item in receipt.capture_manifest.artifacts}
    if (
        Digest.of_json(calibration.payload()) not in captures
        or calibration.fixture_digest not in captures
    ):
        return ("behavioral_calibration_capture_mismatch",)
    expected = {
        (assertion_id(check, index), fault)
        for check in checks
        for index in range(len(check.assertions))
        for fault in check.fault_classes
    }
    outcomes = {(item.assertion_id, item.fault_class): item for item in calibration.results}
    if set(outcomes) != expected or len(outcomes) != len(calibration.results):
        return ("behavioral_calibration_incomplete",)
    holds: list[str] = []
    references = set(receipt.input_receipt_digests)
    for check in checks:
        for fault in check.fault_classes:
            caught = False
            for index, assertion in enumerate(check.assertions):
                outcome = outcomes[assertion_id(check, index), fault]
                if not fault_applies(assertion, fault):
                    if outcome.classification != "not_injected" or outcome.applied:
                        holds.append("behavioral_calibration_invalid_injection")
                    continue
                required_rung = "binding_identity" if fault == "wrong_build" else "evaluator"
                if (
                    outcome.classification != "caught"
                    or not outcome.applied
                    or outcome.rung != required_rung
                    or outcome.baseline_result is not EvidenceResult.PASS
                    or outcome.baseline_receipt_digest not in references
                    or outcome.observation_receipt_digest not in references
                    or (fault != "wrong_build" and outcome.fault_result is not EvidenceResult.FAIL)
                ):
                    holds.append(f"behavioral_calibration_{outcome.classification}")
                else:
                    caught = True
            if not caught:
                holds.append("behavioral_calibration_no_relevant_catch")
        for index in range(len(check.assertions)):
            if not any(
                outcome.assertion_id == assertion_id(check, index)
                and outcome.fault_class in {"constant_result", "skipped_persistence"}
                and outcome.classification == "caught"
                and outcome.rung == "evaluator"
                and outcome.applied
                and outcome.baseline_result is EvidenceResult.PASS
                and outcome.fault_result is EvidenceResult.FAIL
                for outcome in calibration.results
            ):
                holds.append("behavioral_calibration_semantic_catch_required")
    return tuple(dict.fromkeys(holds))


def _observation_holds(
    check: BehavioralCheck,
    item: ObservedCheck,
    inputs: AcceptanceInputs,
) -> tuple[str, ...]:
    receipt, data = item.receipt, item.data
    holds: list[str] = []
    if receipt.category is not ReceiptCategory.INSTRUMENT_OBSERVATION:
        holds.append("behavioral_worker_claim_not_evidence")
    if not inputs.matches(receipt):
        holds.append("behavioral_observation_stale")
    if (
        receipt.observer.kind.value != check.observer
        or data.observer != check.observer
        or receipt.observer.mock
        or data.mock
    ):
        holds.append("behavioral_observer_untrusted")
    if not data.ran or data.skipped:
        holds.append("behavioral_assertion_not_run")
    if data.infrastructure_error is not None:
        holds.append(data.infrastructure_error.partition(":")[0])
    if (
        receipt.observer.identity != f"enginery-{check.observer}-observer"
        or receipt.observer.version != EVALUATOR_VERSION
    ):
        holds.append("behavioral_observer_untrusted")
    try:
        start = datetime.fromisoformat(data.started_at)
        end = datetime.fromisoformat(data.ended_at)
        if start != receipt.started_at or end != receipt.ended_at:
            holds.append("behavioral_observation_time_mismatch")
    except ValueError:
        holds.append("behavioral_observation_time_mismatch")
    try:
        captured = Digest.of_bytes(canonical_bytes(observation_payload(data)))
    except InvalidInputError:
        holds.append("behavioral_capture_exceeds_bound")
    else:
        if captured not in {artifact.digest for artifact in receipt.capture_manifest.artifacts}:
            holds.append("behavioral_observation_capture_mismatch")
    artifacts = receipt.capture_manifest.artifacts
    if any(artifact.decode.status is DecodeStatus.INVALID for artifact in artifacts):
        holds.append("behavioral_capture_not_decodable")
    if any(
        not any(
            artifact.digest == Digest.of_bytes(content)
            and artifact.media_type == media_type
            and artifact.size_bytes == len(content)
            for artifact in artifacts
        )
        for media_type, content in data.captures
    ):
        holds.append("behavioral_capture_missing")
    if check.observer == "browser" and not any(
        artifact.media_type == "image/png" and artifact.decode.status is DecodeStatus.DECODABLE
        for artifact in receipt.capture_manifest.artifacts
    ):
        holds.append("behavioral_browser_capture_missing")
    if len(dict(data.produced_files)) != len(data.produced_files) or (
        len(dict(data.dom_results)) != len(data.dom_results)
    ):
        holds.append("behavioral_observation_ambiguous")
    return tuple(holds)


def evaluate_observation(
    *,
    check: BehavioralCheck,
    observation: ObservedCheck,
    inputs: AcceptanceInputs,
) -> BehavioralEvaluation:
    """Evaluate one bound observation without assuming it is calibrated already."""
    holds = _observation_holds(check, observation, inputs)
    outcomes = (
        tuple(
            AssertionOutcome(assertion_id(check, index), EvidenceResult.INDETERMINATE, False)
            for index in range(len(check.assertions))
        )
        if holds
        else evaluate_assertions(check, observation.data)
    )
    result = (
        EvidenceResult.INDETERMINATE
        if holds
        or not outcomes
        or any(item.result is EvidenceResult.INDETERMINATE for item in outcomes)
        else EvidenceResult.FAIL
        if any(item.result is EvidenceResult.FAIL for item in outcomes)
        else EvidenceResult.PASS
    )
    return BehavioralEvaluation(result, outcomes, holds)


def evaluate_behavioral(
    *,
    checks: tuple[BehavioralCheck, ...],
    inputs: AcceptanceInputs,
    observations: tuple[ObservedCheck, ...],
    calibration: CalibrationReceipt | None,
) -> BehavioralEvaluation:
    """Only a complete, current, non-mock, calibrated semantic result can pass."""
    holds: list[str] = []
    if not checks or not any(check.assertions for check in checks):
        holds.append("no_behavioral_checks")
    if any(not check.assertions for check in checks):
        holds.append("behavioral_zero_assertions")
    if any(not check.fault_classes for check in checks):
        holds.append("behavioral_calibration_faults_missing")
    if len({check.name for check in checks}) != len(checks):
        holds.append("behavioral_check_identity_ambiguous")
    if (
        inputs.evaluator.version != EVALUATOR_VERSION
        or inputs.evaluator.assertion_set_digest
        != Digest.of_json([check.to_state() for check in checks])
    ):
        holds.append("behavioral_evaluator_identity_mismatch")
    by_name = {item.check_name: item for item in observations}
    if len(by_name) != len(observations) or set(by_name) != {check.name for check in checks}:
        holds.append("behavioral_observations_incomplete")
    outcomes: list[AssertionOutcome] = []
    for check in checks:
        item = by_name.get(check.name)
        if item is None:
            holds.append("behavioral_observation_missing")
            outcomes.extend(
                AssertionOutcome(assertion_id(check, index), EvidenceResult.INDETERMINATE, False)
                for index in range(len(check.assertions))
            )
        else:
            observed_result = evaluate_observation(check=check, observation=item, inputs=inputs)
            holds.extend(observed_result.holds)
            outcomes.extend(observed_result.assertion_outcomes)
    holds.extend(calibration_holds(checks, inputs, calibration))
    if any(not item.ran or item.skipped for item in outcomes):
        holds.append("behavioral_assertion_not_run")
    result = (
        EvidenceResult.INDETERMINATE
        if holds
        or not outcomes
        or any(item.result is EvidenceResult.INDETERMINATE for item in outcomes)
        else EvidenceResult.FAIL
        if any(item.result is EvidenceResult.FAIL for item in outcomes)
        else EvidenceResult.PASS
    )
    reasons = tuple(dict.fromkeys(holds))
    if result is EvidenceResult.FAIL:
        reasons = tuple(
            f"behavioral_assertion_failed:{item.assertion_id}"
            for item in outcomes
            if item.result is EvidenceResult.FAIL
        )
    return BehavioralEvaluation(result, tuple(outcomes), reasons)
