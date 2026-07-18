"""Typed worker-result envelope validated by the coordinator."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from enginery.domain.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class WorkerResultEnvelope:
    """A worker's immutable report for one fenced operation.

    Workers transport this value to the coordinator. They never append it to
    the ledger themselves; the coordinator validates every identifier against
    the current durable lease and supervisor record before ingestion.
    """

    run_id: str
    node_id: str
    attempt_id: str
    epoch: int
    fencing_token: int
    operation_id: str
    terminal_result: str
    artifact_references: tuple[str, ...]
    result: Mapping[str, object]

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.run_id, self.node_id, self.attempt_id, self.operation_id)
        ):
            raise InvalidInputError("worker result identifiers must be non-blank")
        if self.epoch < 1 or self.fencing_token < 1:
            raise InvalidInputError("worker result epoch and fencing token must be positive")
        if self.terminal_result not in {"passed", "failed", "cancelled"}:
            raise InvalidInputError("worker result must be passed, failed, or cancelled")
        if any(not reference.strip() for reference in self.artifact_references):
            raise InvalidInputError("worker artifact references must be non-blank")


__all__ = ["WorkerResultEnvelope"]
