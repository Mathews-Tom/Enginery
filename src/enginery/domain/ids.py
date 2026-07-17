"""Provider-neutral aggregate and value identifiers.

Every aggregate and cross-aggregate reference in the domain layer is
identified by a dedicated frozen value type rather than a bare ``str``. This
keeps a ``WorkItemId`` from being accidentally substituted for a ``RunId`` at
a call site and gives every identifier one place to enforce the shared
non-empty, printable, unpadded format contract.

Identifier *generation* is a port introduced in a later milestone (see
``tests/support/ids.py``); this module only validates and carries whatever
opaque token that future generator produces.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from enginery.domain.errors import InvalidInputError

_MAX_LENGTH = 128


def _validate_identifier_value(value: str, *, type_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInputError(
            f"{type_name} value must be a non-empty string",
            details={"type": type_name, "value": value},
        )
    if len(value) > _MAX_LENGTH:
        raise InvalidInputError(
            f"{type_name} value exceeds {_MAX_LENGTH} characters",
            details={"type": type_name, "length": len(value)},
        )
    if value != value.strip():
        raise InvalidInputError(
            f"{type_name} value must not have leading or trailing whitespace",
            details={"type": type_name, "value": value},
        )
    if any(not character.isprintable() for character in value):
        raise InvalidInputError(
            f"{type_name} value must contain only printable characters",
            details={"type": type_name, "value": value},
        )
    return value


@dataclass(frozen=True, slots=True)
class _Identifier:
    """Shared validated-string base for every dedicated identifier type."""

    value: str

    def __post_init__(self) -> None:
        _validate_identifier_value(self.value, type_name=type(self).__name__)

    def __str__(self) -> str:
        return self.value


class WorkItemId(_Identifier):
    """Identifies a :class:`enginery.domain.work_item.WorkItem` aggregate."""


class WorkflowDefinitionId(_Identifier):
    """Identifies an immutable, versioned workflow manifest."""


class RunId(_Identifier):
    """Identifies one workflow-instance aggregate."""


class NodeId(_Identifier):
    """Identifies a node declaration within one workflow manifest."""


class NodeAttemptId(_Identifier):
    """Identifies one attempt to execute a node."""


class ArtifactId(_Identifier):
    """Identifies one content-addressed artifact."""


class PolicyDecisionId(_Identifier):
    """Identifies one durable policy decision record."""


class InterventionId(_Identifier):
    """Identifies one human intervention record."""


class OutcomeId(_Identifier):
    """Identifies one post-execution outcome observation."""


class FactoryChangeId(_Identifier):
    """Identifies one candidate factory-asset change."""


@dataclass(frozen=True, slots=True)
class OperationId:
    """A stable side-effect identity, reused across every retry.

    Derived once from the run ID, node ID, logical side-effect kind, target
    scope, and operation ordinal *before* the first attempt. The attempt
    number is never part of the identity, so retries of the same logical
    side effect reuse this exact ID and adapters can pass it straight
    through to a provider's native idempotency key.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_identifier_value(self.value, type_name=type(self).__name__)

    def __str__(self) -> str:
        return self.value

    @classmethod
    def derive(
        cls,
        *,
        run_id: RunId,
        node_id: NodeId,
        side_effect_kind: str,
        target_scope: str,
        ordinal: int,
    ) -> OperationId:
        if not side_effect_kind or not side_effect_kind.strip():
            raise InvalidInputError("side_effect_kind must be a non-blank string")
        if not target_scope or not target_scope.strip():
            raise InvalidInputError("target_scope must be a non-blank string")
        if ordinal < 0:
            raise InvalidInputError(
                "operation ordinal cannot be negative", details={"ordinal": ordinal}
            )
        payload = "\x1f".join(
            (str(run_id), str(node_id), side_effect_kind, target_scope, str(ordinal))
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return cls(value=digest)


__all__ = [
    "ArtifactId",
    "FactoryChangeId",
    "InterventionId",
    "NodeAttemptId",
    "NodeId",
    "OperationId",
    "OutcomeId",
    "PolicyDecisionId",
    "RunId",
    "WorkItemId",
    "WorkflowDefinitionId",
]
