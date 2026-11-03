"""Model usage a harness reported for one attempt: a sourced claim, never billing.

A report aggregates the finalized assistant messages a harness printed in one retained
output artifact. Its token counts and cost components are the harness's own figures: the
cost carries no currency and is not an invoice or a verified provider charge. A message the
harness finalized without a report leaves the attempt partially reported, and a present
report that does not have the protocol's shape is retained as malformed. Neither is ever
coerced to zero or dropped.

Worker output is untrusted and can be large, so a report is bounded: message counts are
complete, while the line numbers and malformed reasons it keeps are the first
``MAX_RECORDED_LINES`` of each kind. The full output stays in the cited artifact.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_receipt import WORKER_REPORTED

REPORTED_USAGE_SCHEMA_VERSION = 1

HARNESS_REPORTED = "harness"
"""``reported_by`` for figures a trusted-local harness printed for its own model calls."""

USAGE_COMPONENTS = ("input", "output", "cache_read", "cache_write", "total")
"""Token and cost components, in the harness's own buckets; ``total`` is not recomputed."""

MAX_REPORTED_VALUE = 2**53 - 1
"""The largest per-message figure a JSON harness can report exactly; larger is malformed.

Bounding each message also bounds every sum: no total can overflow a float or JSON.
"""

MAX_RECORDED_LINES = 256
"""Line numbers (and malformed reasons) kept per kind; counts beyond this stay complete."""

_REPORTERS = frozenset({HARNESS_REPORTED, WORKER_REPORTED})
_MESSAGE_KINDS = ("reported", "unreported", "malformed")
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "reported_by",
        "source",
        "messages",
        "reported_lines",
        "unreported_lines",
        "malformed",
        "unparsed_lines",
        "tokens",
        "cost",
    }
)
_SOURCE_RECORD = "message_end"
_SOURCE_ROLE = "assistant"


class UsageReportState(enum.Enum):
    """How completely the harness reported usage for its finalized assistant messages."""

    REPORTED = "reported"
    """Every finalized assistant message carried a well-formed report."""
    PARTIAL = "partial"
    """Some finalized assistant messages carried no report; totals cover the others."""
    MALFORMED = "malformed"
    """A present report did not match the protocol; no totals are derived."""


def _count(value: object, *, name: str, limit: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidInputError(f"reported usage {name} must be a non-negative integer")
    if limit is not None and value > limit:
        raise InvalidInputError(f"reported usage {name} exceeds {limit}")
    return value


def _amount(value: object, *, name: str, limit: int | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidInputError(f"reported usage {name} must be a finite non-negative number")
    if limit is not None and value > limit:
        raise InvalidInputError(f"reported usage {name} exceeds {limit}")
    try:
        amount = float(value)
    except OverflowError as error:
        raise InvalidInputError(f"reported usage {name} is not a finite number") from error
    if not math.isfinite(amount) or amount < 0:
        raise InvalidInputError(f"reported usage {name} must be a finite non-negative number")
    return amount


def _components(state: object, *, kind: str) -> dict[str, object]:
    if not isinstance(state, dict) or set(state) != set(USAGE_COMPONENTS):
        raise InvalidInputError(f"reported usage {kind} must name exactly its components")
    return {component: state[component] for component in USAGE_COMPONENTS}


@dataclass(frozen=True, slots=True)
class UsageTokens:
    """Token counts in the harness's buckets: new input, output, cache read and cache write."""

    input: int
    output: int
    cache_read: int
    cache_write: int
    total: int

    def __post_init__(self) -> None:
        for component in USAGE_COMPONENTS:
            _count(getattr(self, component), name=f"tokens.{component}")

    def to_state(self) -> dict[str, object]:
        return {component: getattr(self, component) for component in USAGE_COMPONENTS}

    @classmethod
    def from_state(cls, state: object, *, limit: int | None = None) -> UsageTokens:
        values = _components(state, kind="tokens")
        return cls(
            *(_count(values[name], name=f"tokens.{name}", limit=limit) for name in USAGE_COMPONENTS)
        )


@dataclass(frozen=True, slots=True)
class UsageCost:
    """Cost components the harness computed itself: no currency, no invoice, no verification."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    total: float

    def __post_init__(self) -> None:
        for component in USAGE_COMPONENTS:
            _amount(getattr(self, component), name=f"cost.{component}")

    def to_state(self) -> dict[str, object]:
        return {component: getattr(self, component) for component in USAGE_COMPONENTS}

    @classmethod
    def from_state(cls, state: object, *, limit: int | None = None) -> UsageCost:
        values = _components(state, kind="cost")
        return cls(
            *(_amount(values[name], name=f"cost.{name}", limit=limit) for name in USAGE_COMPONENTS)
        )


@dataclass(frozen=True, slots=True)
class MalformedUsageReport:
    """A present report the harness printed that did not have the protocol's shape."""

    line: int
    reason: str

    def __post_init__(self) -> None:
        if isinstance(self.line, bool) or not isinstance(self.line, int) or self.line < 1:
            raise InvalidInputError("a malformed usage report must name its 1-based line")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise InvalidInputError("a malformed usage report must state its reason")


def _ascending(lines: tuple[int, ...], *, name: str) -> None:
    for line in lines:
        _count(line, name=name)
    if any(line < 1 for line in lines) or any(a >= b for a, b in pairwise(lines)):
        raise InvalidInputError(f"reported usage {name} must be ascending 1-based lines")


@dataclass(frozen=True, slots=True)
class ReportedUsage:
    """One attempt's harness-reported usage, bound to the output artifact it was read from.

    Lines are 1-based line numbers in that artifact, so a counted figure can be traced to
    its finalized record. ``unparsed_lines`` counts output lines that were not JSON (for
    example a failed process's diagnostics): they carry no report that could be read.
    """

    source_artifact: Digest
    reported_by: str
    reported_messages: int
    unreported_messages: int
    malformed_messages: int
    reported_lines: tuple[int, ...]
    unreported_lines: tuple[int, ...]
    malformed: tuple[MalformedUsageReport, ...]
    unparsed_lines: int
    tokens: UsageTokens | None
    cost: UsageCost | None

    def __post_init__(self) -> None:
        if self.reported_by not in _REPORTERS:
            raise InvalidInputError("reported usage must name the harness or worker reporter")
        for kind, count, kept in (
            ("reported", self.reported_messages, len(self.reported_lines)),
            ("unreported", self.unreported_messages, len(self.unreported_lines)),
            ("malformed", self.malformed_messages, len(self.malformed)),
        ):
            if kept != min(_count(count, name=f"{kind} messages"), MAX_RECORDED_LINES):
                raise InvalidInputError(f"reported usage keeps the wrong {kind} line count")
        _ascending(self.reported_lines, name="reported_lines")
        _ascending(self.unreported_lines, name="unreported_lines")
        _ascending(tuple(report.line for report in self.malformed), name="malformed lines")
        kept_lines = [
            *self.reported_lines,
            *self.unreported_lines,
            *(report.line for report in self.malformed),
        ]
        if len(set(kept_lines)) != len(kept_lines):
            raise InvalidInputError("a reported usage line can be counted only once")
        _count(self.unparsed_lines, name="unparsed_lines")
        if not self.reported_messages and not self.malformed_messages:
            raise InvalidInputError("reported usage requires at least one present report")
        derived = not self.malformed_messages
        if (self.tokens is not None) != derived or (self.cost is not None) != derived:
            raise InvalidInputError("only well-formed reported usage carries totals")

    @property
    def state(self) -> UsageReportState:
        if self.malformed_messages:
            return UsageReportState.MALFORMED
        if self.unreported_messages:
            return UsageReportState.PARTIAL
        return UsageReportState.REPORTED

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": REPORTED_USAGE_SCHEMA_VERSION,
            "state": self.state.value,
            "reported_by": self.reported_by,
            "source": {
                "artifact_digest": str(self.source_artifact),
                "record": _SOURCE_RECORD,
                "role": _SOURCE_ROLE,
            },
            "messages": {
                "reported": self.reported_messages,
                "unreported": self.unreported_messages,
                "malformed": self.malformed_messages,
            },
            "reported_lines": list(self.reported_lines),
            "unreported_lines": list(self.unreported_lines),
            "malformed": [
                {"line": report.line, "reason": report.reason} for report in self.malformed
            ],
            "unparsed_lines": self.unparsed_lines,
            "tokens": None if self.tokens is None else self.tokens.to_state(),
            "cost": None if self.cost is None else self.cost.to_state(),
        }

    @classmethod
    def from_state(cls, state: object) -> ReportedUsage:
        """Decode a persisted report exactly; any deviation is malformed evidence."""
        if not isinstance(state, dict) or set(state) != _STATE_KEYS:
            raise InvalidInputError("reported usage must name exactly the reported-usage fields")
        version = state["schema_version"]
        if isinstance(version, bool) or version != REPORTED_USAGE_SCHEMA_VERSION:
            raise InvalidInputError("reported usage schema version is unsupported")
        source = state["source"]
        if (
            not isinstance(source, dict)
            or set(source) != {"artifact_digest", "record", "role"}
            or source["record"] != _SOURCE_RECORD
            or source["role"] != _SOURCE_ROLE
            or not isinstance(source["artifact_digest"], str)
        ):
            raise InvalidInputError("reported usage source must name its artifact and record")
        algorithm, _, hex_value = source["artifact_digest"].partition(":")
        messages = state["messages"]
        if not isinstance(messages, dict) or set(messages) != set(_MESSAGE_KINDS):
            raise InvalidInputError("reported usage must count each kind of message")
        lines: dict[str, tuple[int, ...]] = {}
        for key in ("reported_lines", "unreported_lines"):
            raw = state[key]
            if not isinstance(raw, list):
                raise InvalidInputError(f"reported usage {key} must be a list")
            lines[key] = tuple(_count(line, name=key) for line in raw)
        raw_malformed = state["malformed"]
        if not isinstance(raw_malformed, list) or any(
            not isinstance(entry, dict) or set(entry) != {"line", "reason"}
            for entry in raw_malformed
        ):
            raise InvalidInputError("reported usage malformed entries must name line and reason")
        reported_by = state["reported_by"]
        if not isinstance(reported_by, str):
            raise InvalidInputError("reported usage must name its reporter")
        usage = cls(
            source_artifact=Digest(algorithm=algorithm, hex_value=hex_value),
            reported_by=reported_by,
            reported_messages=_count(messages["reported"], name="reported messages"),
            unreported_messages=_count(messages["unreported"], name="unreported messages"),
            malformed_messages=_count(messages["malformed"], name="malformed messages"),
            reported_lines=lines["reported_lines"],
            unreported_lines=lines["unreported_lines"],
            malformed=tuple(
                MalformedUsageReport(line=entry["line"], reason=entry["reason"])
                for entry in raw_malformed
            ),
            unparsed_lines=_count(state["unparsed_lines"], name="unparsed_lines"),
            tokens=None if state["tokens"] is None else UsageTokens.from_state(state["tokens"]),
            cost=None if state["cost"] is None else UsageCost.from_state(state["cost"]),
        )
        if state["state"] != usage.state.value:
            raise InvalidInputError("reported usage state does not match its reports")
        return usage


@dataclass(slots=True)
class UsageReportBuilder:
    """Accumulate one artifact's finalized message reports in a single bounded pass.

    Lines must be added in artifact order. Totals are kept exactly (costs decimally, so a
    sum carries no binary rounding residue); only the first ``MAX_RECORDED_LINES`` line
    numbers of each kind are kept.
    """

    _tokens: dict[str, int] = field(default_factory=lambda: dict.fromkeys(USAGE_COMPONENTS, 0))
    _cost: dict[str, Decimal] = field(
        default_factory=lambda: dict.fromkeys(USAGE_COMPONENTS, Decimal(0))
    )
    _counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_MESSAGE_KINDS, 0))
    _reported_lines: list[int] = field(default_factory=list)
    _unreported_lines: list[int] = field(default_factory=list)
    _malformed: list[MalformedUsageReport] = field(default_factory=list)
    _unparsed: int = 0

    def add_report(self, line: int, tokens: UsageTokens, cost: UsageCost) -> None:
        self._keep("reported", self._reported_lines, line)
        for component in USAGE_COMPONENTS:
            self._tokens[component] += getattr(tokens, component)
            self._cost[component] += Decimal(repr(getattr(cost, component)))

    def add_unreported(self, line: int) -> None:
        self._keep("unreported", self._unreported_lines, line)

    def add_malformed(self, line: int, reason: str) -> None:
        self._counts["malformed"] += 1
        if len(self._malformed) < MAX_RECORDED_LINES:
            self._malformed.append(MalformedUsageReport(line=line, reason=reason))

    def add_unparsed(self) -> None:
        self._unparsed += 1

    def _keep(self, kind: str, lines: list[int], line: int) -> None:
        self._counts[kind] += 1
        if len(lines) < MAX_RECORDED_LINES:
            lines.append(line)

    def build(self, *, source_artifact: Digest, reported_by: str) -> ReportedUsage | None:
        """The attempt's report; ``None`` when no report is present (unmeasured, not zero)."""
        if not self._counts["reported"] and not self._counts["malformed"]:
            return None
        well_formed = not self._counts["malformed"]
        return ReportedUsage(
            source_artifact=source_artifact,
            reported_by=reported_by,
            reported_messages=self._counts["reported"],
            unreported_messages=self._counts["unreported"],
            malformed_messages=self._counts["malformed"],
            reported_lines=tuple(self._reported_lines),
            unreported_lines=tuple(self._unreported_lines),
            malformed=tuple(self._malformed),
            unparsed_lines=self._unparsed,
            tokens=UsageTokens(**self._tokens) if well_formed else None,
            cost=(
                UsageCost(**{name: float(total) for name, total in self._cost.items()})
                if well_formed
                else None
            ),
        )


__all__ = [
    "HARNESS_REPORTED",
    "MAX_RECORDED_LINES",
    "MAX_REPORTED_VALUE",
    "REPORTED_USAGE_SCHEMA_VERSION",
    "USAGE_COMPONENTS",
    "MalformedUsageReport",
    "ReportedUsage",
    "UsageCost",
    "UsageReportBuilder",
    "UsageReportState",
    "UsageTokens",
]
