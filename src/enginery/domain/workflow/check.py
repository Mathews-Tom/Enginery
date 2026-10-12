"""Typed, closed check contracts bound to executable workflow nodes.

A check names *what is verified* for a ``run_command`` or ``verify_evidence``
node: a stable id, the subject the result is bound to, exactly one of a
command contract or a named observer contract, and what the workflow does
for each non-pass result. The result space is the existing
``EvidenceResult`` (PASS / FAIL / INDETERMINATE); only PASS proceeds.

There is no expression language and no shell string: a command is an argv
tuple (or an explicit request-bound command whose argv the run request
supplies), and an observer is a member of a closed enumeration.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from enginery.domain.errors import InvalidInputError
from enginery.domain.node_attempt import EvidenceResult
from enginery.domain.workflow.shell_guard import reject_shell_invocation

_CHECK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_CHECK_KEYS = frozenset({"id", "subject", "command", "observer", "on_fail", "on_indeterminate"})
_COMMAND_KEYS = frozenset({"source", "argv"})


class CheckSubjectKind(enum.Enum):
    """The exact subject a check result is bound to."""

    SOURCE_REVISION = "source_revision"
    WORKSPACE_HEAD = "workspace_head"
    PULL_REQUEST_HEAD = "pull_request_head"


class CommandSource(enum.Enum):
    """Where a command contract's argv comes from."""

    DECLARED = "declared"
    REQUEST = "request"


class CheckObserver(enum.Enum):
    """The closed set of named observer contracts."""

    PULL_REQUEST_MERGE_READINESS = "pull_request_merge_readiness"
    G4_GATE_FAIL_CLOSED = "g4_gate_fail_closed"


class CheckDisposition(enum.Enum):
    """What a workflow does with one check result. Only PROCEED advances."""

    PROCEED = "proceed"
    REPAIR_OR_STOP = "repair_or_stop"
    STOP = "stop"
    HOLD = "hold"


_FAIL_ACTIONS = frozenset({CheckDisposition.REPAIR_OR_STOP, CheckDisposition.STOP})
_INDETERMINATE_ACTIONS = frozenset({CheckDisposition.STOP, CheckDisposition.HOLD})


@dataclass(frozen=True, slots=True)
class CommandContract:
    """An explicit argv tuple, or a command the run request supplies."""

    source: CommandSource
    argv: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.source is CommandSource.REQUEST:
            if self.argv:
                raise InvalidInputError("a request-bound command contract cannot declare argv")
            return
        if not self.argv:
            raise InvalidInputError("a declared command contract requires a non-empty argv")
        if any(not isinstance(part, str) or not part.strip() for part in self.argv):
            raise InvalidInputError("command argv entries must be non-blank strings")
        executable = self.argv[0]
        if any(character.isspace() for character in executable):
            raise InvalidInputError(
                "command argv[0] must be a single executable, not a shell string"
            )
        reject_shell_invocation(self.argv)

    @classmethod
    def from_mapping(cls, raw: object) -> CommandContract:
        if not isinstance(raw, Mapping):
            raise InvalidInputError("check 'command' must be a mapping")
        unknown_keys = set(raw) - _COMMAND_KEYS
        if unknown_keys:
            raise InvalidInputError(
                "check command declares unknown keys",
                details={"unknown_keys": sorted(str(key) for key in unknown_keys)},
            )
        source = _enum(CommandSource, raw.get("source"), "command source")
        argv_raw = raw.get("argv", ())
        if not isinstance(argv_raw, (list, tuple)) or not all(
            isinstance(part, str) for part in argv_raw
        ):
            raise InvalidInputError("check command 'argv' must be a list of strings")
        return cls(source=source, argv=tuple(argv_raw))

    def to_mapping(self) -> dict[str, object]:
        if self.source is CommandSource.REQUEST:
            return {"source": self.source.value}
        return {"source": self.source.value, "argv": list(self.argv)}


@dataclass(frozen=True, slots=True)
class CheckDeclaration:
    """One typed check: id, bound subject, command-or-observer, and stop behavior."""

    check_id: str
    subject: CheckSubjectKind
    command: CommandContract | None
    observer: CheckObserver | None
    on_fail: CheckDisposition
    on_indeterminate: CheckDisposition

    def __post_init__(self) -> None:
        if not _CHECK_ID_PATTERN.fullmatch(self.check_id):
            raise InvalidInputError(
                "check id must be a stable dotted lowercase identifier",
                details={"check_id": self.check_id},
            )
        if (self.command is None) == (self.observer is None):
            raise InvalidInputError(
                "a check must declare exactly one of a command or an observer contract",
                details={"check_id": self.check_id},
            )
        if self.on_fail not in _FAIL_ACTIONS:
            raise InvalidInputError(
                "check on_fail must be repair_or_stop or stop",
                details={"check_id": self.check_id},
            )
        if self.on_indeterminate not in _INDETERMINATE_ACTIONS:
            raise InvalidInputError(
                "check on_indeterminate must be stop or hold",
                details={"check_id": self.check_id},
            )

    def disposition(self, result: EvidenceResult) -> CheckDisposition:
        """Map a check result to a workflow disposition; only PASS proceeds."""
        if result is EvidenceResult.PASS:
            return CheckDisposition.PROCEED
        if result is EvidenceResult.FAIL:
            return self.on_fail
        return self.on_indeterminate

    @classmethod
    def from_mapping(cls, raw: object) -> CheckDeclaration:
        if not isinstance(raw, Mapping):
            raise InvalidInputError("node 'check' must be a mapping")
        unknown_keys = set(raw) - _CHECK_KEYS
        if unknown_keys:
            raise InvalidInputError(
                "check declares unknown keys; manifests cannot embed executable payloads",
                details={"unknown_keys": sorted(str(key) for key in unknown_keys)},
            )
        check_id = raw.get("id")
        if not isinstance(check_id, str):
            raise InvalidInputError("check requires a string 'id'")
        command_raw = raw.get("command")
        observer_raw = raw.get("observer")
        return cls(
            check_id=check_id,
            subject=_enum(CheckSubjectKind, raw.get("subject"), "check subject"),
            command=CommandContract.from_mapping(command_raw) if command_raw is not None else None,
            observer=(
                _enum(CheckObserver, observer_raw, "check observer")
                if observer_raw is not None
                else None
            ),
            on_fail=_enum(CheckDisposition, raw.get("on_fail"), "check on_fail"),
            on_indeterminate=_enum(
                CheckDisposition, raw.get("on_indeterminate"), "check on_indeterminate"
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        document: dict[str, object] = {
            "id": self.check_id,
            "subject": self.subject.value,
            "on_fail": self.on_fail.value,
            "on_indeterminate": self.on_indeterminate.value,
        }
        if self.command is not None:
            document["command"] = self.command.to_mapping()
        if self.observer is not None:
            document["observer"] = self.observer.value
        return document


def _enum[E: enum.Enum](enum_type: type[E], value: object, label: str) -> E:
    if not isinstance(value, str):
        raise InvalidInputError(f"{label} must be a string")
    try:
        return enum_type(value)
    except ValueError as error:
        raise InvalidInputError(f"{label} declares unknown value {value!r}") from error


__all__ = [
    "CheckDeclaration",
    "CheckDisposition",
    "CheckObserver",
    "CheckSubjectKind",
    "CommandContract",
    "CommandSource",
]
