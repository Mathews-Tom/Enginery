"""Coordinator epoch ownership and transactional command consumption."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from enginery.domain.errors import (
    ExternalConflictError,
    InternalInvariantViolationError,
    InvalidInputError,
)
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.inbox import InboxRecord
from enginery.ledger.process_manager import ProcessManagerStateRecord, ProcessManagerStateWrite
from enginery.ledger.service import LedgerService

_COORDINATOR_NAME = "workflow-coordinator"
_COORDINATOR_STATE_KEY = "ledger"
_COORDINATOR_AGGREGATE_TYPE = "coordinator"
_COORDINATOR_AGGREGATE_ID = "ledger"


@dataclass(frozen=True, slots=True)
class CoordinatorEpoch:
    """The sole writer's durable epoch and heartbeat deadline."""

    epoch: int
    owner: str
    heartbeat_deadline: datetime
    state_version: int

    def __post_init__(self) -> None:
        if self.epoch < 1:
            raise InvalidInputError(
                "coordinator epoch must be positive", details={"epoch": self.epoch}
            )
        if not self.owner.strip():
            raise InvalidInputError("coordinator owner must be a non-blank string")
        if self.heartbeat_deadline.tzinfo is None:
            raise InvalidInputError("coordinator heartbeat deadline must be timezone-aware")
        if self.state_version < 1:
            raise InvalidInputError(
                "coordinator state version must be positive",
                details={"state_version": self.state_version},
            )

    def active_at(self, now: datetime) -> bool:
        _require_aware(now, field_name="now")
        return now < self.heartbeat_deadline


@dataclass(frozen=True, slots=True)
class CommandConsumption:
    command_id: str
    status: str


class Coordinator:
    """The application service allowed to transition workflow aggregates."""

    def __init__(self, ledger: LedgerService, *, owner: str) -> None:
        if not owner.strip():
            raise InvalidInputError("coordinator owner must be a non-blank string")
        self._ledger = ledger
        self._owner = owner

    @property
    def owner(self) -> str:
        return self._owner

    def current_epoch(self) -> CoordinatorEpoch | None:
        record = self._ledger.read_process_manager_state(
            process_manager_name=_COORDINATOR_NAME, state_key=_COORDINATOR_STATE_KEY
        )
        return _epoch_from_record(record) if record is not None else None

    def acquire(self, *, now: datetime, heartbeat_window: timedelta) -> CoordinatorEpoch:
        """Acquire a newer epoch after the prior epoch is absent or expired."""
        _require_aware(now, field_name="now")
        _require_positive_window(heartbeat_window)
        current = self.current_epoch()
        if current is not None and current.active_at(now):
            raise ExternalConflictError(
                "an active coordinator already owns this ledger",
                details={"epoch": current.epoch, "owner": current.owner},
            )
        next_epoch = 1 if current is None else current.epoch + 1
        expected_state_version = 0 if current is None else current.state_version
        deadline = now + heartbeat_window
        state = _epoch_state(epoch=next_epoch, owner=self._owner, heartbeat_deadline=deadline)
        result = self._ledger.append(
            AppendCommand(
                correlation_id=f"coordinator-acquire:{next_epoch}",
                events=(
                    _coordinator_event(
                        self._ledger,
                        event_type="coordinator.epoch_acquired",
                        payload=state,
                    ),
                ),
                process_manager_updates=(
                    ProcessManagerStateWrite(
                        process_manager_name=_COORDINATOR_NAME,
                        state_key=_COORDINATOR_STATE_KEY,
                        expected_version=expected_state_version,
                        state=state,
                    ),
                ),
            )
        )
        state_version = result.process_manager_states[0].state_version
        return CoordinatorEpoch(next_epoch, self._owner, deadline, state_version)

    def renew(self, *, epoch: int, now: datetime, heartbeat_window: timedelta) -> CoordinatorEpoch:
        """Extend the active owner heartbeat without changing its epoch."""
        _require_aware(now, field_name="now")
        _require_positive_window(heartbeat_window)
        current = self._require_current_owner(epoch=epoch, now=now)
        deadline = now + heartbeat_window
        state = _epoch_state(epoch=epoch, owner=self._owner, heartbeat_deadline=deadline)
        result = self._ledger.append(
            AppendCommand(
                correlation_id=f"coordinator-heartbeat:{epoch}",
                events=(
                    _coordinator_event(
                        self._ledger,
                        event_type="coordinator.heartbeat_recorded",
                        payload=state,
                    ),
                ),
                process_manager_updates=(
                    ProcessManagerStateWrite(
                        process_manager_name=_COORDINATOR_NAME,
                        state_key=_COORDINATOR_STATE_KEY,
                        expected_version=current.state_version,
                        state=state,
                    ),
                ),
            )
        )
        state_version = result.process_manager_states[0].state_version
        return CoordinatorEpoch(epoch, self._owner, deadline, state_version)

    def epoch_guard(self, *, epoch: int, now: datetime) -> ProcessManagerStateWrite:
        """Return the transactional compare-and-swap guard for this epoch.

        Every coordinator-owned append carries this update. A takeover that
        commits between the caller's read and append changes the expected
        state version and rolls back the stale coordinator's entire command.
        """
        _require_aware(now, field_name="now")
        current = self._require_current_owner(epoch=epoch, now=now)
        return ProcessManagerStateWrite(
            process_manager_name=_COORDINATOR_NAME,
            state_key=_COORDINATOR_STATE_KEY,
            expected_version=current.state_version,
            state=_epoch_state(
                epoch=current.epoch,
                owner=current.owner,
                heartbeat_deadline=current.heartbeat_deadline,
            ),
        )

    def consume_pending(
        self,
        *,
        epoch: int,
        now: datetime,
        heartbeat_window: timedelta,
        limit: int = 100,
    ) -> tuple[CommandConsumption, ...]:
        """Consume pending commands under the supplied active coordinator epoch."""
        _require_aware(now, field_name="now")
        _require_positive_window(heartbeat_window)
        if limit < 1:
            raise InvalidInputError("limit must be positive", details={"limit": limit})
        results: list[CommandConsumption] = []
        for command in self._ledger.list_pending_inbox_commands(limit=limit):
            current = self._require_current_owner(epoch=epoch, now=now)
            deadline = now + heartbeat_window
            state = _epoch_state(epoch=epoch, owner=self._owner, heartbeat_deadline=deadline)
            try:
                run_event = _run_command_event(command)
            except InvalidInputError as error:
                self._ledger.append(
                    AppendCommand(
                        correlation_id=command.correlation_id,
                        events=(
                            _coordinator_event(
                                self._ledger,
                                event_type="coordinator.command_rejected",
                                payload={
                                    "command_id": command.command_id,
                                    "command_type": command.command_type,
                                    "reason": str(error),
                                },
                            ),
                        ),
                        inbox_command_id=command.command_id,
                        inbox_status="rejected",
                        process_manager_updates=(
                            ProcessManagerStateWrite(
                                process_manager_name=_COORDINATOR_NAME,
                                state_key=_COORDINATOR_STATE_KEY,
                                expected_version=current.state_version,
                                state=state,
                            ),
                        ),
                    )
                )
                results.append(CommandConsumption(command.command_id, "rejected"))
                continue
            self._ledger.append(
                AppendCommand(
                    correlation_id=command.correlation_id,
                    events=(
                        run_event,
                        _coordinator_event(
                            self._ledger,
                            event_type="coordinator.command_processed",
                            payload={
                                "command_id": command.command_id,
                                "command_type": command.command_type,
                                "epoch": epoch,
                            },
                        ),
                    ),
                    inbox_command_id=command.command_id,
                    process_manager_updates=(
                        ProcessManagerStateWrite(
                            process_manager_name=_COORDINATOR_NAME,
                            state_key=_COORDINATOR_STATE_KEY,
                            expected_version=current.state_version,
                            state=state,
                        ),
                    ),
                )
            )
            results.append(CommandConsumption(command.command_id, "processed"))
        return tuple(results)

    def _require_current_owner(self, *, epoch: int, now: datetime) -> CoordinatorEpoch:
        current = self.current_epoch()
        if current is None or current.epoch != epoch or current.owner != self._owner:
            raise ExternalConflictError(
                "coordinator epoch is not the current owner",
                details={"epoch": epoch, "owner": self._owner},
            )
        if not current.active_at(now):
            raise ExternalConflictError(
                "coordinator heartbeat has expired",
                details={"epoch": epoch, "owner": self._owner},
            )
        return current


def _epoch_state(*, epoch: int, owner: str, heartbeat_deadline: datetime) -> dict[str, object]:
    return {
        "epoch": epoch,
        "owner": owner,
        "heartbeat_deadline": heartbeat_deadline.isoformat(),
    }


def _epoch_from_record(record: ProcessManagerStateRecord) -> CoordinatorEpoch:
    state = record.state
    epoch = state.get("epoch")
    owner = state.get("owner")
    deadline_raw = state.get("heartbeat_deadline")
    if (
        not isinstance(epoch, int)
        or not isinstance(owner, str)
        or not isinstance(deadline_raw, str)
    ):
        raise InternalInvariantViolationError(
            "stored coordinator state has an invalid shape",
            details={"state_key": record.state_key},
        )
    try:
        deadline = datetime.fromisoformat(deadline_raw)
    except ValueError as error:
        raise InternalInvariantViolationError(
            "stored coordinator heartbeat deadline is invalid",
            details={"heartbeat_deadline": deadline_raw},
        ) from error
    return CoordinatorEpoch(epoch, owner, deadline, record.state_version)


def _coordinator_event(
    ledger: LedgerService, *, event_type: str, payload: dict[str, object]
) -> EventWrite:
    projection = ledger.read_projection(
        aggregate_type=_COORDINATOR_AGGREGATE_TYPE, aggregate_id=_COORDINATOR_AGGREGATE_ID
    )
    return EventWrite(
        aggregate_type=_COORDINATOR_AGGREGATE_TYPE,
        aggregate_id=_COORDINATOR_AGGREGATE_ID,
        expected_version=0 if projection is None else projection.aggregate_version,
        event_type=event_type,
        schema_version=1,
        payload=payload,
    )


def _run_command_event(command: InboxRecord) -> EventWrite:
    if command.command_type not in {"run.cancel", "run.resume"}:
        raise InvalidInputError(
            "unsupported coordinator command type",
            details={"command_type": command.command_type},
        )
    run_id = command.payload.get("run_id")
    expected_version = command.payload.get("expected_run_version")
    if not isinstance(run_id, str) or not run_id.strip():
        raise InvalidInputError("run command payload requires a non-blank run_id")
    if not isinstance(expected_version, int) or expected_version < 0:
        raise InvalidInputError("run command payload requires a non-negative expected_run_version")
    event_type = (
        "run.cancellation_requested"
        if command.command_type == "run.cancel"
        else "run.resume_requested"
    )
    return EventWrite(
        aggregate_type="run",
        aggregate_id=run_id,
        expected_version=expected_version,
        event_type=event_type,
        schema_version=1,
        payload={"requested_by_command": command.command_id},
    )


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None:
        raise InvalidInputError(f"{field_name} must be timezone-aware")


def _require_positive_window(window: timedelta) -> None:
    if window <= timedelta():
        raise InvalidInputError("heartbeat window must be positive")


__all__ = ["CommandConsumption", "Coordinator", "CoordinatorEpoch"]
