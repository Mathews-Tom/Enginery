"""The evidence document of the factory gate and its exit-code contract.

Pure standard library. The builder is the only place a verdict is decided, and it decides
conservatively: a required assertion that did not run is a failure, a control that did not hold
makes every assertion that depends on it untrustworthy (so the verdict fails), and a scenario that
could not start because the engine, an image or a permission is missing is a hold, never a pass.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_HOLD = 3

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_HOLD = "hold"
VERDICTS = (VERDICT_PASS, VERDICT_FAIL, VERDICT_HOLD)

MODE_LOCAL = "local"
MODE_LIVE = "live"

IDENTITY_KEYS = ("subject", "build", "config", "host", "engine", "image", "profile", "evaluator")
ASSERTION_KEYS = (
    "id",
    "description",
    "ran",
    "status",
    "commands",
    "observed",
    "expected",
    "observer",
    "raw_artifacts",
)
CONTROL_KEYS = ("id", "description", "ran", "status", "commands", "observed", "expected")
EFFECT_KEYS_REQUIRED = ("hosted_inference_calls",)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class EvidenceError(Exception):
    """The builder was used inconsistently (an unknown or repeated assertion id)."""


class EvidenceBuilder:
    """Collects assertions and controls for one scenario and turns them into a verdict."""

    def __init__(
        self,
        *,
        scenario: str,
        mode: str,
        required: Mapping[str, str],
        scrub: Callable[[Any], Any] | None = None,
        started_at: str | None = None,
        raw_writer: Callable[[str, Any], str] | None = None,
    ) -> None:
        if mode not in (MODE_LOCAL, MODE_LIVE):
            raise EvidenceError(f"unknown mode {mode!r}")
        self.scenario = scenario
        self.mode = mode
        self.required = dict(required)
        self.started_at = started_at or utc_now()
        self._scrub: Callable[[Any], Any] = scrub or (lambda value: value)
        self.raw_writer = raw_writer
        self._assertions: dict[str, dict[str, Any]] = {}
        self._controls: dict[str, dict[str, Any]] = {}
        self._hold: str | None = None
        self._abort: str | None = None
        self.details: dict[str, Any] = {}
        self.not_requested: dict[str, str] = {}

    # --- recording -------------------------------------------------------------------------

    def assertion(
        self,
        assertion_id: str,
        *,
        passed: bool,
        commands: Sequence[str],
        observed: Any,
        expected: str,
        observer: Any = None,
        raw_artifacts: Sequence[str] = (),
        description: str | None = None,
    ) -> bool:
        if assertion_id not in self.required:
            raise EvidenceError(f"assertion {assertion_id!r} is not part of this scenario")
        if assertion_id in self._assertions:
            raise EvidenceError(f"assertion {assertion_id!r} was recorded twice")
        if observer is None:
            raise EvidenceError(
                f"assertion {assertion_id!r} needs an independent host-side observation"
            )
        references = list(raw_artifacts)
        if not references and self.raw_writer is not None:
            references.append(
                self.raw_writer(
                    f"assertions/{assertion_id}.json",
                    {
                        "id": assertion_id,
                        "commands": [str(command) for command in commands],
                        "observed": observed,
                        "expected": expected,
                        "observer": observer,
                    },
                )
            )
        self._assertions[assertion_id] = {
            "id": assertion_id,
            "description": description or self.required[assertion_id],
            "ran": True,
            "status": "pass" if passed else "fail",
            "commands": [str(command) for command in commands],
            "observed": observed,
            "expected": expected,
            "observer": observer,
            "raw_artifacts": references,
        }
        return passed

    def fail_assertion(
        self, assertion_id: str, reason: str, *, commands: Sequence[str] = ()
    ) -> None:
        """Record an assertion that could not be evaluated (a step error): always a failure."""
        if assertion_id in self._assertions:
            return
        self.assertion(
            assertion_id,
            passed=False,
            commands=commands,
            observed={"error": reason},
            expected="the assertion runs to completion",
            observer={"not_observed": reason},
        )
        self._assertions[assertion_id]["ran"] = False

    def control(
        self,
        control_id: str,
        *,
        description: str,
        passed: bool,
        observed: Any,
        expected: str,
        commands: Sequence[str] = (),
    ) -> bool:
        if control_id in self._controls:
            raise EvidenceError(f"control {control_id!r} was recorded twice")
        self._controls[control_id] = {
            "id": control_id,
            "description": description,
            "ran": True,
            "status": "pass" if passed else "fail",
            "commands": [str(command) for command in commands],
            "observed": observed,
            "expected": expected,
        }
        return passed

    def hold(self, reason: str) -> None:
        self._hold = reason

    def abort(self, reason: str) -> None:
        self._abort = self._abort or reason

    @property
    def recorded(self) -> set[str]:
        return set(self._assertions)

    # --- verdict ---------------------------------------------------------------------------

    def _complete_assertions(self) -> list[dict[str, Any]]:
        completed: list[dict[str, Any]] = []
        for assertion_id, description in self.required.items():
            entry = self._assertions.get(assertion_id)
            if entry is None:
                reason = (
                    f"not run: the scenario is held ({self._hold})"
                    if self._hold
                    else "not run: the scenario ended before this assertion ran"
                )
                entry = {
                    "id": assertion_id,
                    "description": description,
                    "ran": False,
                    "status": "fail",
                    "commands": [],
                    "observed": {"error": reason},
                    "expected": "the assertion runs and passes",
                    "observer": None,
                    "raw_artifacts": [],
                }
            completed.append(entry)
        return completed

    def build(
        self,
        *,
        identities: Mapping[str, Any],
        effects: Mapping[str, Any],
        raw_artifacts_dir: str | None,
        ended_at: str | None = None,
    ) -> dict[str, Any]:
        assertions = self._complete_assertions()
        controls = list(self._controls.values())
        failed = [a["id"] for a in assertions if a["status"] != "pass"]
        failed_controls = [c["id"] for c in controls if c["status"] != "pass"]
        if self._hold is not None:
            verdict, reason = VERDICT_HOLD, self._hold
        elif self._abort is not None:
            verdict, reason = VERDICT_FAIL, self._abort
        elif failed or failed_controls:
            verdict = VERDICT_FAIL
            parts = []
            if failed:
                parts.append(f"failed or not-run assertions: {', '.join(failed)}")
            if failed_controls:
                parts.append(f"failed controls: {', '.join(failed_controls)}")
            reason = "; ".join(parts)
        else:
            verdict = VERDICT_PASS
            reason = f"all {len(assertions)} required assertions ran and passed"
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "scenario": self.scenario,
            "mode": self.mode,
            "verdict": verdict,
            "reason": reason,
            "started_at": self.started_at,
            "ended_at": ended_at or utc_now(),
            "identities": dict(identities),
            "assertions": assertions,
            "controls": controls,
            "external_effect_counts": dict(effects),
            "raw_artifacts_dir": raw_artifacts_dir,
            "optional_not_requested": dict(self.not_requested),
            "details": self.details,
        }
        scrubbed = self._scrub(document)
        problems = validate_evidence(scrubbed)
        if problems:
            scrubbed["verdict"] = VERDICT_FAIL
            scrubbed["reason"] = "the evidence document is malformed: " + "; ".join(problems)
        return scrubbed  # type: ignore[no-any-return]


def exit_code_for(evidence: Mapping[str, Any]) -> int:
    """The process exit status for an evidence document: 0 only for a complete pass."""
    if validate_evidence(evidence):
        return EXIT_FAIL
    verdict = evidence.get("verdict")
    if verdict == VERDICT_HOLD:
        return EXIT_HOLD
    if verdict != VERDICT_PASS:
        return EXIT_FAIL
    assertions = evidence.get("assertions", [])
    if not assertions:
        return EXIT_FAIL
    for entry in list(assertions) + list(evidence.get("controls", [])):
        if entry.get("status") != "pass" or entry.get("ran") is not True:
            return EXIT_FAIL
    return EXIT_PASS


def validate_evidence(document: Mapping[str, Any]) -> list[str]:
    """Schema checks for an evidence document; the empty list means it is well formed."""
    problems: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version must be 1")
    for key in ("scenario", "reason", "started_at", "ended_at"):
        if not isinstance(document.get(key), str) or not document[key]:
            problems.append(f"{key} must be non-empty text")
    if document.get("mode") not in (MODE_LOCAL, MODE_LIVE):
        problems.append("mode must be local or live")
    if document.get("verdict") not in VERDICTS:
        problems.append("verdict must be pass, fail or hold")
    identities = document.get("identities")
    if not isinstance(identities, Mapping):
        problems.append("identities must be an object")
    else:
        for key in IDENTITY_KEYS:
            if key not in identities:
                problems.append(f"identities lacks {key}")
    assertions = document.get("assertions")
    if not isinstance(assertions, list):
        problems.append("assertions must be a list")
        assertions = []
    seen: set[str] = set()
    for entry in assertions:
        if not isinstance(entry, Mapping):
            problems.append("an assertion is not an object")
            continue
        for key in ASSERTION_KEYS:
            if key not in entry:
                problems.append(f"assertion {entry.get('id')} lacks {key}")
        identifier = entry.get("id")
        if identifier in seen:
            problems.append(f"assertion {identifier} is listed twice")
        seen.add(str(identifier))
        if entry.get("status") not in ("pass", "fail"):
            problems.append(f"assertion {identifier} status must be pass or fail")
        if entry.get("status") == "pass" and entry.get("ran") is not True:
            problems.append(f"assertion {identifier} passed without having run")
        if entry.get("status") == "pass" and not entry.get("commands"):
            problems.append(f"assertion {identifier} passed without recorded commands")
        if entry.get("status") == "pass" and entry.get("observer") is None:
            problems.append(f"assertion {identifier} passed without a host-side observation")
    controls = document.get("controls")
    if not isinstance(controls, list):
        problems.append("controls must be a list")
        controls = []
    for entry in controls:
        if not isinstance(entry, Mapping):
            problems.append("a control is not an object")
            continue
        for key in CONTROL_KEYS:
            if key not in entry:
                problems.append(f"control {entry.get('id')} lacks {key}")
        if entry.get("status") not in ("pass", "fail"):
            problems.append(f"control {entry.get('id')} status must be pass or fail")
    effects = document.get("external_effect_counts")
    if not isinstance(effects, Mapping):
        problems.append("external_effect_counts must be an object")
    else:
        for key in EFFECT_KEYS_REQUIRED:
            if key not in effects:
                problems.append(f"external_effect_counts lacks {key}")
    if document.get("verdict") == VERDICT_PASS:
        if any(entry.get("status") != "pass" for entry in assertions if isinstance(entry, Mapping)):
            problems.append("a pass verdict has a failed assertion")
        if any(entry.get("status") != "pass" for entry in controls if isinstance(entry, Mapping)):
            problems.append("a pass verdict has a failed control")
    return problems


def dumps(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
