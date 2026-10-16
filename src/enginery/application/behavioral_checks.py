"""Operator-authored behavioral assertions and their evaluator-custody identity.

These are declarations, not observations or verdicts. Parsing a declaration never executes
its argv, contacts its URL, or treats the existence of an assertion as a passing result.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, cast
from urllib.parse import urlsplit

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.workflow.check import CommandContract, CommandSource

type ObserverKind = Literal["command", "service", "browser"]
type AssertionKind = Literal["exit_status", "stdout", "stderr", "produced_file", "body", "dom"]
type FaultClass = Literal["constant_result", "skipped_persistence", "wrong_build"]

OBSERVER_KINDS = ("command", "service", "browser")
ASSERTION_KINDS = ("exit_status", "stdout", "stderr", "produced_file", "body", "dom")
FAULT_CLASSES = ("constant_result", "skipped_persistence", "wrong_build")
EVALUATOR_VERSION = "behavioral-evaluator-v1"
OPERATOR_EXECUTABLE_KEYS = ("browser", "browser_digest", "lavish", "gh_axi")
_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise InvalidInputError(f"{what} must be a table")
    return value


def _keys(value: dict[str, object], *, required: set[str], optional: set[str], what: str) -> None:
    unknown = set(value) - required - optional
    missing = required - set(value)
    if unknown or missing:
        raise InvalidInputError(
            f"{what} has unknown or missing keys",
            details={"unknown": sorted(unknown), "missing": sorted(missing)},
        )


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InvalidInputError(f"{what} must be non-blank text without NUL bytes")
    return value


def _digest(value: object, what: str) -> str:
    text = _text(value, what)
    algorithm, _, hex_value = text.partition(":")
    try:
        return str(Digest(algorithm, hex_value))
    except InvalidInputError:
        raise InvalidInputError(f"{what} must be a SHA-256 digest") from None


def require_base_revision(value: str) -> None:
    """A mutable ref or abbreviated object id cannot identify the assertion source."""
    if _REVISION.fullmatch(value) is None:
        raise InvalidInputError("behavioral checks require a full immutable base commit revision")


def _loopback_url(value: object) -> str:
    text = _text(value, "behavioral check URL")
    if any(character.isspace() or ord(character) < 32 for character in text):
        raise InvalidInputError("behavioral check URL must not contain whitespace or controls")
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        port = parsed.port
        loopback = host == "localhost" or (
            host is not None and ipaddress.ip_address(host).is_loopback
        )
    except ValueError as error:
        raise InvalidInputError(
            "behavioral check URL must be HTTP(S) loopback without credentials"
        ) from error
    if (
        not loopback
        or parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port == 0
    ):
        raise InvalidInputError("behavioral check URL must be HTTP(S) loopback without credentials")
    return text


@dataclass(frozen=True, slots=True)
class BehavioralAssertion:
    """One typed comparison over a trusted observer's actual output."""

    kind: AssertionKind
    operator: Literal["equals", "contains", "exists"]
    expected: str | int | bool
    path: str | None = None
    selector: str | None = None

    def to_state(self) -> dict[str, object]:
        state: dict[str, object] = {"kind": self.kind, self.operator: self.expected}
        if self.path is not None:
            state["path"] = self.path
        if self.selector is not None:
            state["selector"] = self.selector
        return state

    @classmethod
    def from_state(cls, state: object) -> BehavioralAssertion:
        raw = _mapping(state, "behavioral assertion")
        kind = raw.get("kind")
        if kind not in ASSERTION_KINDS:
            raise InvalidInputError("behavioral assertion kind is unsupported")
        required = {"kind"}
        if kind == "produced_file":
            required.add("path")
        if kind == "dom":
            required.add("selector")
        _keys(raw, required=required, optional={"equals", "contains", "exists"}, what="assertion")
        operators = set(raw) & {"equals", "contains", "exists"}
        if len(operators) != 1:
            raise InvalidInputError("behavioral assertion requires exactly one comparison")
        operator = operators.pop()
        expected = raw[operator]
        if kind == "exit_status":
            if operator != "equals" or not isinstance(expected, int) or isinstance(expected, bool):
                raise InvalidInputError("exit_status requires an integer equals assertion")
        elif operator == "exists":
            if kind not in {"produced_file", "dom"} or not isinstance(expected, bool):
                raise InvalidInputError("exists requires a produced_file or DOM boolean assertion")
        elif not isinstance(expected, str) or "\x00" in expected:
            raise InvalidInputError("text comparisons require a string without NUL bytes")
        elif operator == "contains" and not expected:
            raise InvalidInputError("contains must not be empty")
        path = None
        if kind == "produced_file":
            path = _text(raw["path"], "produced file path")
            relative = PurePosixPath(path)
            if relative.is_absolute() or ".." in relative.parts or path == "." or "\\" in path:
                raise InvalidInputError("produced file path must stay relative to the candidate")
        selector = _text(raw["selector"], "DOM selector") if kind == "dom" else None
        return cls(
            kind=cast(AssertionKind, kind),
            operator=cast(Literal["equals", "contains", "exists"], operator),
            expected=cast(str | int | bool, expected),
            path=path,
            selector=selector,
        )


@dataclass(frozen=True, slots=True)
class BehavioralCheck:
    """One named observer, its trusted input, assertions and declared calibration faults."""

    name: str
    observer: ObserverKind
    assertions: tuple[BehavioralAssertion, ...]
    fault_classes: tuple[FaultClass, ...]
    argv: tuple[str, ...] = ()
    url: str | None = None

    def to_state(self) -> dict[str, object]:
        state: dict[str, object] = {
            "name": self.name,
            "observer": self.observer,
            "assertions": [assertion.to_state() for assertion in self.assertions],
            "fault_classes": list(self.fault_classes),
        }
        if self.argv:
            state["argv"] = list(self.argv)
        if self.url is not None:
            state["url"] = self.url
        return state

    @classmethod
    def from_state(cls, state: object) -> BehavioralCheck:
        raw = _mapping(state, "behavioral check")
        _keys(
            raw,
            required={"name", "observer", "assertions", "fault_classes"},
            optional={"argv", "url"},
            what="behavioral check",
        )
        name = _text(raw["name"], "behavioral check name")
        if _NAME.fullmatch(name) is None:
            raise InvalidInputError("behavioral check name must be a stable lowercase identifier")
        observer = raw["observer"]
        if observer not in OBSERVER_KINDS:
            raise InvalidInputError("behavioral observer must be command, service or browser")
        argv: tuple[str, ...] = ()
        if "argv" in raw:
            command = CommandContract.from_mapping(
                {"source": CommandSource.DECLARED.value, "argv": raw["argv"]}
            )
            argv = command.argv
            if any("\x00" in argument for argument in argv):
                raise InvalidInputError("behavioral argv must not contain NUL bytes")
        url = _loopback_url(raw["url"]) if "url" in raw else None
        if observer == "command" and (not argv or url is not None):
            raise InvalidInputError("a command observer requires argv and must not declare a URL")
        if observer != "command" and url is None:
            raise InvalidInputError("service and browser observers require a loopback URL")
        assertions_raw = raw["assertions"]
        if not isinstance(assertions_raw, list):
            raise InvalidInputError("behavioral assertions must be a list")
        assertions = tuple(BehavioralAssertion.from_state(item) for item in assertions_raw)
        allowed_kinds = {
            "command": {"exit_status", "stdout", "stderr", "produced_file"},
            "service": {"body"},
            "browser": {"dom"},
        }
        if any(assertion.kind not in allowed_kinds[str(observer)] for assertion in assertions):
            raise InvalidInputError("behavioral assertion is incompatible with its observer")
        faults = raw["fault_classes"]
        if not isinstance(faults, list) or not faults:
            raise InvalidInputError("behavioral fault_classes must be a non-empty list")
        if any(not isinstance(item, str) or item not in FAULT_CLASSES for item in faults):
            raise InvalidInputError("behavioral fault class is unsupported")
        if len(faults) != len(set(faults)):
            raise InvalidInputError("behavioral fault_classes must not contain duplicates")
        return cls(
            name=name,
            observer=cast(ObserverKind, observer),
            assertions=assertions,
            fault_classes=tuple(cast(FaultClass, item) for item in faults),
            argv=argv,
            url=url,
        )


def checks_from_state(state: object) -> tuple[BehavioralCheck, ...]:
    if not isinstance(state, list):
        raise InvalidInputError("acceptance checks must be a list")
    checks = tuple(BehavioralCheck.from_state(item) for item in state)
    if len({check.name for check in checks}) != len(checks):
        raise InvalidInputError("acceptance check names must be unique")
    return checks


@dataclass(frozen=True, slots=True)
class OperatorExecutables:
    """Host programs selected only by operator-owned user preferences."""

    browser: str | None = None
    browser_digest: str | None = None
    lavish: str | None = None
    gh_axi: str | None = None

    def to_state(self) -> dict[str, object]:
        return {
            name: value
            for name in OPERATOR_EXECUTABLE_KEYS
            if (value := getattr(self, name)) is not None
        }

    @classmethod
    def from_state(cls, state: object) -> OperatorExecutables:
        raw = _mapping(state, "operator executables")
        _keys(raw, required=set(), optional=set(OPERATOR_EXECUTABLE_KEYS), what="executables")
        values = {name: _text(value, f"executables.{name}") for name, value in raw.items()}
        if ("browser" in values) != ("browser_digest" in values):
            raise InvalidInputError(
                "executables.browser and browser_digest must be configured together"
            )
        if "browser_digest" in values:
            values["browser_digest"] = _digest(values["browser_digest"], "browser digest")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class BehavioralChecksBinding:
    """Private immutable assertion copy; the digest also binds base, config and evaluator."""

    base_revision: str
    configuration_snapshot_digest: str
    checks: tuple[BehavioralCheck, ...]
    evaluator_version: str = EVALUATOR_VERSION

    @property
    def assertion_set_digest(self) -> Digest:
        return Digest.of_json([check.to_state() for check in self.checks])

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "base_revision": self.base_revision,
            "configuration_snapshot_digest": self.configuration_snapshot_digest,
            "checks": [check.to_state() for check in self.checks],
            "assertion_set_digest": str(self.assertion_set_digest),
            "evaluator_version": self.evaluator_version,
        }

    @property
    def digest(self) -> Digest:
        return Digest.of_json(self.to_state())

    @classmethod
    def from_state(cls, state: object) -> BehavioralChecksBinding:
        raw = _mapping(state, "behavioral checks custody document")
        _keys(
            raw,
            required={
                "schema_version",
                "base_revision",
                "configuration_snapshot_digest",
                "checks",
                "assertion_set_digest",
                "evaluator_version",
            },
            optional=set(),
            what="behavioral checks custody document",
        )
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise InvalidInputError("unsupported behavioral checks custody schema")
        revision = _text(raw["base_revision"], "acceptance base revision")
        require_base_revision(revision)
        binding = cls(
            base_revision=revision,
            configuration_snapshot_digest=_digest(
                raw["configuration_snapshot_digest"], "config digest"
            ),
            checks=checks_from_state(raw["checks"]),
            evaluator_version=_text(raw["evaluator_version"], "evaluator version"),
        )
        if raw["assertion_set_digest"] != str(binding.assertion_set_digest):
            raise InvalidInputError("behavioral checks custody assertion digest does not match")
        return binding
