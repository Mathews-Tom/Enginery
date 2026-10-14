"""What a harness was actually sent and what it was observed to run.

A receipt is a runtime observation, kept apart from the selection it answers. Its
submitted arguments are the native control tokens the adapter emitted; its effective
controls are what the host's own output reported, with the evidence it came from, or an
explicit UNKNOWN. An effective value is never copied from, defaulted to, or inferred from
the request: a host that does not report a control leaves it unknown.

A receipt also records the host versions it answers: the range the catalog accepted, the
one version the catalog tested, and the version observed when the dispatch command was
built (for queued work, when it was queued). The observed version is never presented as tested.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import (
    ExecutionHold,
    ExecutionSelection,
    HoldReason,
)

EXECUTION_RECEIPT_SCHEMA_VERSION = 1


class EffectiveState(enum.Enum):
    """Whether a control's effective value was observed or is not known."""

    OBSERVED = "observed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EffectiveControl:
    """One effective control: an observed value with its evidence, or an explicit UNKNOWN.

    ``source`` names where an observed value came from, or why an unknown one could not
    be observed.
    """

    state: EffectiveState
    value: str | None
    source: str

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise InvalidInputError("an effective control must name its evidence or its reason")
        if self.state is EffectiveState.OBSERVED:
            if self.value is None or not self.value.strip():
                raise InvalidInputError("an observed effective control requires a value")
        elif self.value is not None:
            raise InvalidInputError("an unknown effective control cannot carry a value")

    @classmethod
    def observed(cls, value: str, *, source: str) -> EffectiveControl:
        return cls(state=EffectiveState.OBSERVED, value=value, source=source)

    @classmethod
    def unknown(cls, *, reason: str) -> EffectiveControl:
        return cls(state=EffectiveState.UNKNOWN, value=None, source=reason)

    def to_state(self) -> dict[str, object]:
        return {"state": self.state.value, "value": self.value, "source": self.source}

    @classmethod
    def from_state(cls, state: object, *, name: str) -> EffectiveControl:
        if not isinstance(state, dict):
            raise InvalidInputError(f"execution receipt {name} must be a mapping")
        value = state.get("value")
        source = state.get("source")
        raw_state = state.get("state")
        if (
            (value is not None and not isinstance(value, str))
            or not isinstance(source, str)
            or not isinstance(raw_state, str)
        ):
            raise InvalidInputError(f"execution receipt {name} is malformed")
        try:
            effective_state = EffectiveState(raw_state)
        except ValueError as error:
            raise InvalidInputError(f"execution receipt {name} state is unknown") from error
        return cls(state=effective_state, value=value, source=source)


@dataclass(frozen=True, slots=True)
class HostObservation:
    """The host versions an execution answers, kept apart so none is mistaken for another.

    ``accepted_executable_versions`` is the bound range (absent for an exact-bound
    selection), ``tested_executable_version`` the version the catalog tested, and
    ``observed_executable_version_at_launch`` the installed version seen when the dispatch
    command was built (for queued work, when it was queued; not necessarily the moment the
    process started), never one probed at collection, or None when none was recorded.
    """

    accepted_executable_versions: str | None
    tested_executable_version: str
    observed_executable_version_at_launch: str | None

    def __post_init__(self) -> None:
        if not self.tested_executable_version.strip():
            raise InvalidInputError("execution receipt tested executable version must be non-blank")

    def to_state(self) -> dict[str, object]:
        return {
            "accepted_executable_versions": self.accepted_executable_versions,
            "tested_executable_version": self.tested_executable_version,
            "observed_executable_version_at_launch": self.observed_executable_version_at_launch,
        }

    @classmethod
    def from_state(cls, state: object) -> HostObservation | None:
        if state is None:
            return None
        if not isinstance(state, Mapping):
            raise InvalidInputError("execution receipt host must be a mapping")
        accepted = state.get("accepted_executable_versions")
        tested = state.get("tested_executable_version")
        observed = state.get("observed_executable_version_at_launch")
        if (
            (accepted is not None and not isinstance(accepted, str))
            or not isinstance(tested, str)
            or (observed is not None and not isinstance(observed, str))
        ):
            raise InvalidInputError("execution receipt host is malformed")
        return cls(
            accepted_executable_versions=accepted,
            tested_executable_version=tested,
            observed_executable_version_at_launch=observed,
        )


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """The submitted and effective controls of one harness execution."""

    selection_digest: Digest | None
    harness: str
    submitted_arguments: tuple[str, ...]
    effective_model: EffectiveControl
    effective_effort: EffectiveControl
    host: HostObservation | None = None

    def __post_init__(self) -> None:
        if not self.harness.strip():
            raise InvalidInputError("execution receipt harness must be non-blank")
        if any(not argument.strip() for argument in self.submitted_arguments):
            raise InvalidInputError("execution receipt submitted arguments must be non-blank")

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": EXECUTION_RECEIPT_SCHEMA_VERSION,
            "selection_digest": None
            if self.selection_digest is None
            else str(self.selection_digest),
            "harness": self.harness,
            "submitted_arguments": list(self.submitted_arguments),
            "effective_model": self.effective_model.to_state(),
            "effective_effort": self.effective_effort.to_state(),
            "host": None if self.host is None else self.host.to_state(),
        }

    @classmethod
    def from_state(cls, state: object) -> ExecutionReceipt:
        if not isinstance(state, Mapping):
            raise InvalidInputError("execution receipt must be a mapping")
        if state.get("schema_version") != EXECUTION_RECEIPT_SCHEMA_VERSION:
            raise InvalidInputError("execution receipt schema version is unsupported")
        digest_text = state.get("selection_digest")
        harness = state.get("harness")
        submitted = state.get("submitted_arguments")
        if (
            (digest_text is not None and not isinstance(digest_text, str))
            or not isinstance(harness, str)
            or not isinstance(submitted, list)
            or any(not isinstance(argument, str) for argument in submitted)
        ):
            raise InvalidInputError("execution receipt is malformed")
        digest = None
        if digest_text is not None:
            algorithm, _, hex_value = digest_text.partition(":")
            digest = Digest(algorithm=algorithm, hex_value=hex_value)
        return cls(
            selection_digest=digest,
            harness=harness,
            submitted_arguments=tuple(str(argument) for argument in submitted),
            effective_model=EffectiveControl.from_state(
                state.get("effective_model"), name="effective_model"
            ),
            effective_effort=EffectiveControl.from_state(
                state.get("effective_effort"), name="effective_effort"
            ),
            host=HostObservation.from_state(state.get("host")),
        )


def evaluate_effective_requirement(
    selection: ExecutionSelection, receipt: ExecutionReceipt | None
) -> ExecutionHold | None:
    """Return a hold when a strict effective-control requirement is not demonstrably met.

    Under a strict requirement every selected control must have an observed effective value
    equal to what was selected. An unknown effective state does not pass, and a missing
    receipt is the same as an unknown one. Without the requirement, nothing is demanded and
    the receipt simply reports what was observed.
    """
    if not selection.strict_effective:
        return None
    if receipt is not None and receipt.selection_digest != selection.digest:
        return ExecutionHold(
            reason=HoldReason.EFFECTIVE_CONTROL_MISMATCH,
            message="the execution receipt answers a different selection than the one bound",
            details={
                "bound_selection_digest": str(selection.digest),
                "receipt_selection_digest": str(receipt.selection_digest),
            },
        )
    unobserved = EffectiveControl.unknown(reason="the harness produced no execution receipt")
    for name, selected, effective in (
        ("model", selection.selected_model, None if receipt is None else receipt.effective_model),
        (
            "effort",
            selection.selected_effort,
            None if receipt is None else receipt.effective_effort,
        ),
    ):
        if selected is None:
            continue
        observation = effective or unobserved
        if observation.state is EffectiveState.UNKNOWN:
            return ExecutionHold(
                reason=HoldReason.EFFECTIVE_CONTROL_UNVERIFIED,
                message=(
                    f"a strict effective-control requirement needs the effective {name}, but "
                    f"it is unknown ({observation.source}); the run is held, not passed"
                ),
                details={"control": name, "selected": selected, "reason": observation.source},
            )
        if observation.value != selected:
            return ExecutionHold(
                reason=HoldReason.EFFECTIVE_CONTROL_MISMATCH,
                message=(
                    f"the effective {name} {observation.value!r} differs from the selected "
                    f"{selected!r}; the run is held, not passed"
                ),
                details={
                    "control": name,
                    "selected": selected,
                    "effective": str(observation.value),
                    "evidence": observation.source,
                },
            )
    return None


__all__ = [
    "EXECUTION_RECEIPT_SCHEMA_VERSION",
    "EffectiveControl",
    "EffectiveState",
    "ExecutionReceipt",
    "HostObservation",
    "evaluate_effective_requirement",
]
