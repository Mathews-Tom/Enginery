"""Versioned, digest-frozen acceptance baseline of current supported behavior.

The baseline is package data owned by the evaluator, not by workers. Each
version file is verified against a SHA-256 registered here before it is
parsed, so an in-place edit of a frozen version fails loudly; a change
requires a new version file and a new registration. The public file carries
identities only: journeys, assertion identities mapped to check ids, the
existing script or test that exercises each journey, the calibration
categories, and the declared custody of private calibration scenarios.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from enginery.domain.errors import InvalidInputError
from enginery.domain.workflow.manifest import WorkflowManifest

_CONFIG_DIRECTORY = Path(__file__).parent.parent / "config"

REGISTERED_BASELINE_DIGESTS: Mapping[str, str] = {
    "1": "537a50b96730347b0eb1ad9a2d8e6474a3d1868696b3fbb4f2f2950383667d9c",
}
"""SHA-256 of each frozen baseline file's bytes, keyed by ``baseline_version``."""

CALIBRATION_CATEGORIES = frozenset({"caught", "escaped", "not_injected", "inconclusive"})
SCOPE = "current_supported_behavior"
_TOP_LEVEL_KEYS = frozenset(
    {"baseline_version", "scope", "owner", "calibration", "scenario_custody", "journeys"}
)
_JOURNEY_KEYS = frozenset({"id", "summary", "exercised_by", "assertions"})
_ASSERTION_KEYS = frozenset({"id", "check_id"})
_CUSTODY_KEYS = frozenset({"owner", "storage", "public_content"})


class AcceptanceBaselineError(InvalidInputError):
    """Raised when a baseline file is malformed, tampered with, or unregistered."""


@dataclass(frozen=True, slots=True)
class BaselineAssertion:
    """One assertion identity mapped to the check that decides it."""

    assertion_id: str
    check_id: str


@dataclass(frozen=True, slots=True)
class BaselineJourney:
    """One supported journey, its assertions, and what exercises it."""

    journey_id: str
    summary: str
    exercised_by: tuple[str, ...]
    assertions: tuple[BaselineAssertion, ...]


@dataclass(frozen=True, slots=True)
class ScenarioCustody:
    """Where private calibration scenarios live; the public file never holds them."""

    owner: str
    storage: str
    public_content: str


@dataclass(frozen=True, slots=True)
class AcceptanceBaseline:
    """A validated acceptance baseline version."""

    baseline_version: str
    journeys: tuple[BaselineJourney, ...]
    calibration_categories: frozenset[str]
    scenario_custody: ScenarioCustody

    @property
    def check_ids(self) -> frozenset[str]:
        return frozenset(
            assertion.check_id for journey in self.journeys for assertion in journey.assertions
        )

    def require_manifest_checks(self, manifest: WorkflowManifest) -> None:
        """Reject a manifest binding a check the baseline does not reference."""
        bound = {node.check.check_id for node in manifest.nodes.values() if node.check is not None}
        missing = sorted(bound - self.check_ids)
        if missing:
            raise AcceptanceBaselineError(
                "manifest binds checks the acceptance baseline does not reference",
                details={"missing_check_ids": missing},
            )


def load_acceptance_baseline(version: str = "1") -> AcceptanceBaseline:
    """Load a packaged baseline version after verifying its registered digest."""
    path = _CONFIG_DIRECTORY / f"acceptance-baseline-v{version}.toml"
    try:
        content = path.read_bytes()
    except OSError as error:
        raise AcceptanceBaselineError(
            "acceptance baseline could not be read", details={"path": str(path)}
        ) from error
    return parse_acceptance_baseline(content, version=version)


def parse_acceptance_baseline(content: bytes, *, version: str) -> AcceptanceBaseline:
    """Verify ``content`` against the digest registered for ``version``, then parse it."""
    registered = REGISTERED_BASELINE_DIGESTS.get(version)
    if registered is None:
        raise AcceptanceBaselineError(
            "acceptance baseline version is not registered", details={"version": version}
        )
    if hashlib.sha256(content).hexdigest() != registered:
        raise AcceptanceBaselineError(
            "acceptance baseline bytes do not match the registered digest; "
            "a frozen version cannot be edited in place",
            details={"version": version},
        )
    baseline = parse_baseline_document(content.decode("utf-8"))
    if baseline.baseline_version != version:
        raise AcceptanceBaselineError(
            "acceptance baseline declares a different baseline_version than its file",
            details={"declared": baseline.baseline_version, "expected": version},
        )
    return baseline


def parse_baseline_document(text: str) -> AcceptanceBaseline:
    """Validate baseline structure. Digest verification is the caller's separate step."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise AcceptanceBaselineError(f"acceptance baseline is not valid TOML: {error}") from error
    _reject_unknown(raw, _TOP_LEVEL_KEYS, "baseline")
    version = _string(raw, "baseline_version")
    if raw.get("scope") != SCOPE:
        raise AcceptanceBaselineError(f"baseline scope must be {SCOPE!r}")
    if raw.get("owner") != "evaluator":
        raise AcceptanceBaselineError("baseline must be owned by the evaluator")
    calibration = _mapping(raw, "calibration")
    _reject_unknown(calibration, frozenset({"categories"}), "calibration")
    categories_raw = calibration.get("categories")
    if not isinstance(categories_raw, list) or not all(
        isinstance(item, str) for item in categories_raw
    ):
        raise AcceptanceBaselineError("calibration categories must be a list of strings")
    if len(set(categories_raw)) != len(categories_raw) or (
        set(categories_raw) != CALIBRATION_CATEGORIES
    ):
        raise AcceptanceBaselineError(
            "calibration categories must be exactly caught, escaped, not_injected, inconclusive",
            details={"categories": list(categories_raw)},
        )
    custody_raw = _mapping(raw, "scenario_custody")
    _reject_unknown(custody_raw, _CUSTODY_KEYS, "scenario_custody")
    custody = ScenarioCustody(
        owner=_string(custody_raw, "owner"),
        storage=_string(custody_raw, "storage"),
        public_content=_string(custody_raw, "public_content"),
    )
    if (
        custody.storage != "outside_worker_readable_mounts"
        or custody.public_content != "identities_only"
    ):
        raise AcceptanceBaselineError(
            "scenario custody must keep private scenarios outside worker-readable mounts "
            "and publish identities only"
        )
    journeys_raw = raw.get("journeys")
    if not isinstance(journeys_raw, list) or not journeys_raw:
        raise AcceptanceBaselineError("baseline must declare at least one journey")
    journeys = tuple(_journey(item) for item in journeys_raw)
    _require_unique([journey.journey_id for journey in journeys], "journey id")
    assertions = [a for journey in journeys for a in journey.assertions]
    _require_unique([assertion.assertion_id for assertion in assertions], "assertion id")
    _require_unique([assertion.check_id for assertion in assertions], "check id")
    return AcceptanceBaseline(
        baseline_version=version,
        journeys=journeys,
        calibration_categories=frozenset(categories_raw),
        scenario_custody=custody,
    )


def _journey(raw: object) -> BaselineJourney:
    if not isinstance(raw, dict):
        raise AcceptanceBaselineError("each journey must be a table")
    _reject_unknown(raw, _JOURNEY_KEYS, "journey")
    exercised_by = raw.get("exercised_by")
    if (
        not isinstance(exercised_by, list)
        or not exercised_by
        or not all(isinstance(item, str) and item.strip() for item in exercised_by)
    ):
        raise AcceptanceBaselineError("journey exercised_by must list at least one script or test")
    assertions_raw = raw.get("assertions")
    if not isinstance(assertions_raw, list) or not assertions_raw:
        raise AcceptanceBaselineError("each journey must declare at least one assertion")
    assertions: list[BaselineAssertion] = []
    for item in assertions_raw:
        if not isinstance(item, dict):
            raise AcceptanceBaselineError("each assertion must be a table")
        _reject_unknown(item, _ASSERTION_KEYS, "assertion")
        assertions.append(
            BaselineAssertion(assertion_id=_string(item, "id"), check_id=_string(item, "check_id"))
        )
    return BaselineJourney(
        journey_id=_string(raw, "id"),
        summary=_string(raw, "summary"),
        exercised_by=tuple(exercised_by),
        assertions=tuple(assertions),
    )


def _reject_unknown(raw: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise AcceptanceBaselineError(
            f"{label} declares unknown keys", details={"unknown_keys": sorted(unknown)}
        )


def _string(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceBaselineError(f"baseline requires non-blank string {key!r}")
    return value


def _mapping(raw: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise AcceptanceBaselineError(f"baseline requires table {key!r}")
    return value


def _require_unique(values: list[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise AcceptanceBaselineError(
            f"baseline {label}s must be unique", details={"duplicates": duplicates}
        )


__all__ = [
    "CALIBRATION_CATEGORIES",
    "REGISTERED_BASELINE_DIGESTS",
    "AcceptanceBaseline",
    "AcceptanceBaselineError",
    "BaselineAssertion",
    "BaselineJourney",
    "ScenarioCustody",
    "load_acceptance_baseline",
    "parse_acceptance_baseline",
    "parse_baseline_document",
]
