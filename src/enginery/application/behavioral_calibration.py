"""Immutable input bindings and deliberate-fault calibration records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from enginery.application.behavioral_checks import FaultClass
from enginery.domain.behavioral_evidence import (
    BuildIdentity,
    EnvironmentFingerprint,
    EvaluatorIdentity,
    EvidenceReceipt,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.node_attempt import EvidenceResult

type CalibrationClassification = Literal["caught", "escaped", "not_injected", "inconclusive"]
type CalibrationRung = Literal["binding_identity", "observer", "evaluator"]

CALIBRATION_DRIVER = "enginery-protected-calibration"


def _digest(value: object) -> Digest:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise InvalidInputError("calibration digest must be canonical sha256 text")
    return Digest("sha256", value[7:])


@dataclass(frozen=True, slots=True)
class AcceptanceInputs:
    """Every current input that a behavioral observation and calibration must bind."""

    source_revision: str
    base_revision: str
    head_revision: str
    build: BuildIdentity
    configuration_snapshot_digest: Digest
    evaluator: EvaluatorIdentity
    environment: EnvironmentFingerprint

    def to_state(self) -> dict[str, object]:
        return {
            "source_revision": self.source_revision,
            "base_revision": self.base_revision,
            "head_revision": self.head_revision,
            "build": self.build.to_state(),
            "configuration_snapshot_digest": str(self.configuration_snapshot_digest),
            "evaluator": self.evaluator.to_state(),
            "environment": self.environment.to_state(),
        }

    def matches(self, receipt: EvidenceReceipt) -> bool:
        return (
            receipt.source_revision == self.source_revision
            and receipt.base_revision == self.base_revision
            and receipt.head_revision == self.head_revision
            and receipt.build == self.build
            and receipt.configuration_snapshot_digest == self.configuration_snapshot_digest
            and receipt.evaluator == self.evaluator
            and receipt.environment == self.environment
        )


@dataclass(frozen=True, slots=True)
class CalibrationOutcome:
    """One classified (assertion, fault), including whether the fault really ran."""

    assertion_id: str
    fault_class: FaultClass
    classification: CalibrationClassification
    rung: CalibrationRung | None
    applied: bool
    baseline_result: EvidenceResult | None
    fault_result: EvidenceResult | None
    baseline_receipt_digest: Digest | None = None
    observation_receipt_digest: Digest | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.assertion_id or len(self.assertion_id) > 256:
            raise InvalidInputError("calibration requires a bounded assertion identity")
        if self.fault_class not in {"constant_result", "skipped_persistence", "wrong_build"}:
            raise InvalidInputError("unknown calibration fault")
        if self.classification not in {"caught", "escaped", "not_injected", "inconclusive"}:
            raise InvalidInputError("unknown calibration classification")
        if self.rung not in {None, "binding_identity", "observer", "evaluator"}:
            raise InvalidInputError("unknown calibration rung")
        if type(self.applied) is not bool:
            raise InvalidInputError("calibration application marker must be boolean")
        if self.classification in {"caught", "escaped"} and not self.applied:
            raise InvalidInputError("an unapplied fault cannot be caught or escape")
        if self.classification == "not_injected" and self.applied:
            raise InvalidInputError("an applied fault cannot be not_injected")
        if (self.classification == "caught") != (self.rung is not None):
            raise InvalidInputError("only caught faults name a catching rung")
        if self.reason is not None and (len(self.reason) > 512 or not self.reason):
            raise InvalidInputError("calibration reason must be bounded nonempty text")

    def to_state(self) -> dict[str, object]:
        return {
            "assertion_id": self.assertion_id,
            "fault_class": self.fault_class,
            "classification": self.classification,
            "rung": self.rung,
            "applied": self.applied,
            "baseline_result": None if self.baseline_result is None else self.baseline_result.value,
            "fault_result": None if self.fault_result is None else self.fault_result.value,
            "baseline_receipt_digest": (
                None if self.baseline_receipt_digest is None else str(self.baseline_receipt_digest)
            ),
            "observation_receipt_digest": (
                None if self.observation_receipt_digest is None else str(self.observation_receipt_digest)
            ),
            "reason": self.reason,
        }

    @classmethod
    def from_state(cls, state: object) -> CalibrationOutcome:
        fields = {
            "assertion_id", "fault_class", "classification", "rung", "applied",
            "baseline_result", "fault_result", "baseline_receipt_digest",
            "observation_receipt_digest", "reason",
        }
        if not isinstance(state, dict) or set(state) != fields:
            raise InvalidInputError("calibration outcome has missing or unknown fields")
        if not isinstance(state["assertion_id"], str) or type(state["applied"]) is not bool:
            raise InvalidInputError("invalid calibration assertion or application marker")
        reason = state["reason"]
        if reason is not None and not isinstance(reason, str):
            raise InvalidInputError("invalid calibration reason")
        try:
            baseline = (
                None if state["baseline_result"] is None else EvidenceResult(state["baseline_result"])
            )
            fault = None if state["fault_result"] is None else EvidenceResult(state["fault_result"])
        except (TypeError, ValueError) as error:
            raise InvalidInputError("invalid calibration evidence result") from error
        return cls(
            assertion_id=state["assertion_id"],
            fault_class=cast(FaultClass, state["fault_class"]),
            classification=cast(CalibrationClassification, state["classification"]),
            rung=cast(CalibrationRung | None, state["rung"]),
            applied=state["applied"],
            baseline_result=baseline,
            fault_result=fault,
            baseline_receipt_digest=(
                None if state["baseline_receipt_digest"] is None
                else _digest(state["baseline_receipt_digest"])
            ),
            observation_receipt_digest=(
                None if state["observation_receipt_digest"] is None
                else _digest(state["observation_receipt_digest"])
            ),
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class CalibrationReceipt:
    """A captured matrix bound by a persisted instrument receipt, never a verdict."""

    receipt: EvidenceReceipt
    results: tuple[CalibrationOutcome, ...]
    fixture_digest: Digest

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, EvidenceReceipt) or not isinstance(self.fixture_digest, Digest):
            raise InvalidInputError("calibration requires a receipt and protected-fixture digest")
        if not isinstance(self.results, tuple) or any(
            not isinstance(item, CalibrationOutcome) for item in self.results
        ):
            raise InvalidInputError("calibration results must be immutable typed outcomes")
        keys = [(item.assertion_id, item.fault_class) for item in self.results]
        if len(keys) != len(set(keys)):
            raise InvalidInputError("calibration cannot classify the same assertion and fault twice")

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "fixture_digest": str(self.fixture_digest),
            "results": [item.to_state() for item in self.results],
        }

    @property
    def digest(self) -> Digest:
        return self.receipt.digest

    @property
    def receipt_digest(self) -> Digest:
        return self.receipt.digest

    def to_state(self) -> dict[str, object]:
        return {**self.payload(), "receipt": self.receipt.to_state()}

    @classmethod
    def from_state(cls, state: object) -> CalibrationReceipt:
        if not isinstance(state, dict) or set(state) != {
            "schema_version", "fixture_digest", "results", "receipt",
        }:
            raise InvalidInputError("calibration receipt has missing or unknown fields")
        if type(state["schema_version"]) is not int or state["schema_version"] != 1:
            raise InvalidInputError("unsupported calibration receipt schema")
        if not isinstance(state["results"], list):
            raise InvalidInputError("calibration results must be an array")
        return cls(
            EvidenceReceipt.from_state(state["receipt"]),
            tuple(CalibrationOutcome.from_state(item) for item in state["results"]),
            _digest(state["fixture_digest"]),
        )
