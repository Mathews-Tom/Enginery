"""Immutable provenance records; recording a claim never grants acceptance authority."""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.node_attempt import EvidenceResult

MAX_EVIDENCE_BYTES = 1024 * 1024
RECEIPT_SCHEMA_VERSION = 1
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+\Z")


class ReceiptCategory(enum.Enum):
    WORKER_CLAIM = "worker_claim"
    INSTRUMENT_OBSERVATION = "instrument_observation"
    EVALUATED_VERDICT = "evaluated_verdict"


class ObserverKind(enum.Enum):
    WORKER = "worker"
    COMMAND = "command"
    SERVICE = "service"
    BROWSER = "browser"
    EVALUATOR = "evaluator"


class BuildKind(enum.Enum):
    CANDIDATE_ARTIFACT = "candidate_artifact"
    SOURCE_TREE = "source_tree"


class DecodeStatus(enum.Enum):
    DECODABLE = "decodable"
    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 4096:
        raise InvalidInputError(f"{name} must be non-blank bounded text without outer whitespace")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise InvalidInputError(f"{name} must not contain control characters")
    return value


def _instance(value: object, expected: type[object], name: str) -> None:
    if not isinstance(value, expected):
        raise InvalidInputError(f"{name} must be {expected.__name__}")


def _tuple(value: object, expected: type[object], name: str) -> None:
    if not isinstance(value, tuple) or any(not isinstance(item, expected) for item in value):
        raise InvalidInputError(f"{name} must be an immutable tuple of {expected.__name__}")


def _state(value: object, fields: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(fields.split()):
        raise InvalidInputError("evidence document has missing or unknown fields")
    return value


def _digest(value: object) -> Digest:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise InvalidInputError("evidence digest must be a canonical sha256 digest")
    return Digest("sha256", value[7:])


def _enum[E: enum.Enum](expected: type[E], value: object) -> E:
    try:
        return expected(value)
    except (TypeError, ValueError) as error:
        raise InvalidInputError(f"invalid {expected.__name__}") from error


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise InvalidInputError("evidence sequence must be a JSON array")
    return value


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidInputError("evidence timestamp must be ISO-8601 text")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidInputError("evidence timestamp is invalid") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise InvalidInputError("evidence timestamp must be timezone-aware")
    return result


@dataclass(frozen=True, slots=True)
class BuildIdentity:
    kind: BuildKind
    digest: Digest

    def __post_init__(self) -> None:
        _instance(self.kind, BuildKind, "build kind")
        _instance(self.digest, Digest, "build digest")

    def to_state(self) -> dict[str, object]:
        return {"kind": self.kind.value, "digest": str(self.digest)}

    @classmethod
    def from_state(cls, state: object) -> BuildIdentity:
        data = _state(state, "kind digest")
        return cls(_enum(BuildKind, data["kind"]), _digest(data["digest"]))


@dataclass(frozen=True, slots=True)
class EvaluatorIdentity:
    assertion_set_digest: Digest
    version: str

    def __post_init__(self) -> None:
        _instance(self.assertion_set_digest, Digest, "assertion-set digest")
        _text(self.version, "evaluator version")

    def to_state(self) -> dict[str, object]:
        return {"assertion_set_digest": str(self.assertion_set_digest), "version": self.version}

    @classmethod
    def from_state(cls, state: object) -> EvaluatorIdentity:
        data = _state(state, "assertion_set_digest version")
        return cls(_digest(data["assertion_set_digest"]), _text(data["version"], "version"))


@dataclass(frozen=True, slots=True)
class ToolVersion:
    name: str
    version: str

    def __post_init__(self) -> None:
        _text(self.name, "tool name")
        _text(self.version, "tool version")

    def to_state(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_state(cls, state: object) -> ToolVersion:
        data = _state(state, "name version")
        return cls(_text(data["name"], "name"), _text(data["version"], "version"))


@dataclass(frozen=True, slots=True)
class EnvironmentFingerprint:
    host_receipt_digest: Digest
    tool_versions: tuple[ToolVersion, ...]

    def __post_init__(self) -> None:
        _instance(self.host_receipt_digest, Digest, "host/runner receipt digest")
        _tuple(self.tool_versions, ToolVersion, "tool versions")
        names = [tool.name for tool in self.tool_versions]
        if not names or len(set(names)) != len(names):
            raise InvalidInputError("environment requires nonempty, uniquely named tool versions")
        object.__setattr__(
            self,
            "tool_versions",
            tuple(sorted(self.tool_versions, key=lambda x: x.name)),
        )

    def to_state(self) -> dict[str, object]:
        return {
            "host_receipt_digest": str(self.host_receipt_digest),
            "tool_versions": [tool.to_state() for tool in self.tool_versions],
        }

    @classmethod
    def from_state(cls, state: object) -> EnvironmentFingerprint:
        data = _state(state, "host_receipt_digest tool_versions")
        return cls(
            _digest(data["host_receipt_digest"]),
            tuple(ToolVersion.from_state(item) for item in _items(data["tool_versions"])),
        )


@dataclass(frozen=True, slots=True)
class ObserverIdentity:
    identity: str
    kind: ObserverKind
    version: str
    mock: bool = False

    def __post_init__(self) -> None:
        _text(self.identity, "observer identity")
        _instance(self.kind, ObserverKind, "observer kind")
        _text(self.version, "observer version")
        if type(self.mock) is not bool:
            raise InvalidInputError("observer mock marker must be boolean")

    def to_state(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "kind": self.kind.value,
            "version": self.version,
            "mock": self.mock,
        }

    @classmethod
    def from_state(cls, state: object) -> ObserverIdentity:
        data = _state(state, "identity kind version mock")
        mock = data["mock"]
        if type(mock) is not bool:
            raise InvalidInputError("observer mock marker must be boolean")
        return cls(
            _text(data["identity"], "identity"),
            _enum(ObserverKind, data["kind"]),
            _text(data["version"], "version"),
            mock,
        )


@dataclass(frozen=True, slots=True)
class MediaDecodeResult:
    status: DecodeStatus
    decoder: str
    width: int | None = None
    height: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _instance(self.status, DecodeStatus, "decode status")
        _text(self.decoder, "decoder identity")
        if (self.width is None) != (self.height is None):
            raise InvalidInputError("decoded dimensions must be present together")
        for dimension in (self.width, self.height):
            if dimension is not None and (type(dimension) is not int or dimension <= 0):
                raise InvalidInputError("decoded dimensions must be positive integers")
        if self.reason is not None:
            _text(self.reason, "decode reason")
        if self.status is DecodeStatus.INVALID and self.reason is None:
            raise InvalidInputError("invalid media requires a decode reason")
        if self.status is not DecodeStatus.INVALID and self.reason is not None:
            raise InvalidInputError("only invalid media carries a decode failure reason")
        if self.status is DecodeStatus.NOT_APPLICABLE and self.width is not None:
            raise InvalidInputError("non-media captures cannot carry decoded dimensions")

    def to_state(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "decoder": self.decoder,
            "width": self.width,
            "height": self.height,
            "reason": self.reason,
        }

    @classmethod
    def from_state(cls, state: object) -> MediaDecodeResult:
        data = _state(state, "status decoder width height reason")
        width, height, reason = data["width"], data["height"], data["reason"]
        if width is not None and type(width) is not int:
            raise InvalidInputError("decode width must be an integer")
        if height is not None and type(height) is not int:
            raise InvalidInputError("decode height must be an integer")
        return cls(
            _enum(DecodeStatus, data["status"]),
            _text(data["decoder"], "decoder"),
            width,
            height,
            None if reason is None else _text(reason, "reason"),
        )


@dataclass(frozen=True, slots=True)
class CaptureArtifact:
    digest: Digest
    media_type: str
    size_bytes: int
    decode: MediaDecodeResult

    def __post_init__(self) -> None:
        _instance(self.digest, Digest, "raw artifact digest")
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE.fullmatch(self.media_type):
            raise InvalidInputError("capture media type must be a canonical type/subtype")
        if type(self.size_bytes) is not int or not 0 <= self.size_bytes <= MAX_EVIDENCE_BYTES:
            raise InvalidInputError("raw capture exceeds the 1 MiB limit or has an invalid size")
        _instance(self.decode, MediaDecodeResult, "capture decode result")
        if self.media_type == "image/png" and self.decode.status is DecodeStatus.NOT_APPLICABLE:
            raise InvalidInputError("PNG captures require a decode result")
        if (
            self.media_type == "image/png"
            and self.decode.status is DecodeStatus.DECODABLE
            and self.decode.width is None
        ):
            raise InvalidInputError("decoded PNG captures require dimensions")

    def to_state(self) -> dict[str, object]:
        return {
            "digest": str(self.digest),
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "decode": self.decode.to_state(),
        }

    @classmethod
    def from_state(cls, state: object) -> CaptureArtifact:
        data = _state(state, "digest media_type size_bytes decode")
        size = data["size_bytes"]
        if type(size) is not int:
            raise InvalidInputError("capture size must be an integer")
        return cls(
            _digest(data["digest"]),
            _text(data["media_type"], "media type"),
            size,
            MediaDecodeResult.from_state(data["decode"]),
        )


@dataclass(frozen=True, slots=True)
class CaptureManifest:
    artifacts: tuple[CaptureArtifact, ...]

    def __post_init__(self) -> None:
        _tuple(self.artifacts, CaptureArtifact, "capture artifacts")
        if not self.artifacts or len(self.artifacts) > 256:
            raise InvalidInputError("capture manifest requires between 1 and 256 artifacts")

    def to_state(self) -> dict[str, object]:
        return {"artifacts": [artifact.to_state() for artifact in self.artifacts]}

    @classmethod
    def from_state(cls, state: object) -> CaptureManifest:
        data = _state(state, "artifacts")
        return cls(tuple(CaptureArtifact.from_state(item) for item in _items(data["artifacts"])))


@dataclass(frozen=True, slots=True)
class AssertionOutcome:
    assertion_id: str
    result: EvidenceResult
    ran: bool
    skipped: bool = False

    def __post_init__(self) -> None:
        _text(self.assertion_id, "assertion identity")
        _instance(self.result, EvidenceResult, "assertion result")
        if type(self.ran) is not bool or type(self.skipped) is not bool:
            raise InvalidInputError("assertion execution markers must be boolean")
        if self.result is EvidenceResult.PASS and (not self.ran or self.skipped):
            raise InvalidInputError("an unexecuted or skipped assertion cannot pass")

    def to_state(self) -> dict[str, object]:
        return {
            "assertion_id": self.assertion_id,
            "result": self.result.value,
            "ran": self.ran,
            "skipped": self.skipped,
        }

    @classmethod
    def from_state(cls, state: object) -> AssertionOutcome:
        data = _state(state, "assertion_id result ran skipped")
        ran, skipped = data["ran"], data["skipped"]
        if type(ran) is not bool or type(skipped) is not bool:
            raise InvalidInputError("assertion execution markers must be boolean")
        return cls(
            _text(data["assertion_id"], "assertion identity"),
            _enum(EvidenceResult, data["result"]),
            ran,
            skipped,
        )


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    """A bound record, not proof of its producer's trustworthiness or authorization.

    Only the trusted evaluator may create acceptance verdicts. Category separation
    prevents a worker's claimed PASS or successful media decode becoming a verdict.
    """

    source_revision: str
    base_revision: str
    head_revision: str
    build: BuildIdentity
    configuration_snapshot_digest: Digest
    evaluator: EvaluatorIdentity
    environment: EnvironmentFingerprint
    observer: ObserverIdentity
    capture_manifest: CaptureManifest
    started_at: datetime
    ended_at: datetime
    category: ReceiptCategory
    result: EvidenceResult | None = None
    input_receipt_digests: tuple[Digest, ...] = ()
    assertion_outcomes: tuple[AssertionOutcome, ...] = ()

    def __post_init__(self) -> None:
        for name in ("source_revision", "base_revision", "head_revision"):
            _text(getattr(self, name), name)
        for value, expected, name in (
            (self.build, BuildIdentity, "build identity"),
            (self.configuration_snapshot_digest, Digest, "configuration snapshot digest"),
            (self.evaluator, EvaluatorIdentity, "evaluator identity"),
            (self.environment, EnvironmentFingerprint, "environment fingerprint"),
            (self.observer, ObserverIdentity, "observer identity"),
            (self.capture_manifest, CaptureManifest, "capture manifest"),
            (self.category, ReceiptCategory, "receipt category"),
        ):
            _instance(value, expected, name)
        for timestamp in (self.started_at, self.ended_at):
            if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
                raise InvalidInputError("receipt timestamps must be timezone-aware")
        if self.ended_at < self.started_at:
            raise InvalidInputError("receipt end precedes start")
        _tuple(self.input_receipt_digests, Digest, "input receipt digests")
        _tuple(self.assertion_outcomes, AssertionOutcome, "assertion outcomes")
        ids = [outcome.assertion_id for outcome in self.assertion_outcomes]
        if len(ids) != len(set(ids)):
            raise InvalidInputError("assertion outcomes must have unique identities")
        if self.category is ReceiptCategory.EVALUATED_VERDICT:
            _instance(self.result, EvidenceResult, "evaluated verdict result")
            if self.observer.kind is not ObserverKind.EVALUATOR or not self.input_receipt_digests:
                raise InvalidInputError("evaluated verdict requires evaluator and input receipts")
            if self.result is EvidenceResult.PASS and (
                self.observer.mock
                or not self.assertion_outcomes
                or any(item.result is not EvidenceResult.PASS for item in self.assertion_outcomes)
            ):
                raise InvalidInputError(
                    "passing verdict requires real, executed passing assertions"
                )
        elif self.result is not None:
            raise InvalidInputError(
                "claims and observations cannot carry an evaluated verdict result"
            )
        if self.category is ReceiptCategory.WORKER_CLAIM:
            if self.observer.kind is not ObserverKind.WORKER:
                raise InvalidInputError("worker claim requires a worker observer identity")
        elif self.category is ReceiptCategory.INSTRUMENT_OBSERVATION and self.observer.kind in (
            ObserverKind.WORKER,
            ObserverKind.EVALUATOR,
        ):
            raise InvalidInputError("instrument observation requires an instrument observer")

    def to_state(self) -> dict[str, object]:
        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "source_revision": self.source_revision,
            "base_revision": self.base_revision,
            "head_revision": self.head_revision,
            "build": self.build.to_state(),
            "configuration_snapshot_digest": str(self.configuration_snapshot_digest),
            "evaluator": self.evaluator.to_state(),
            "environment": self.environment.to_state(),
            "observer": self.observer.to_state(),
            "capture_manifest": self.capture_manifest.to_state(),
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "ended_at": self.ended_at.astimezone(UTC).isoformat(),
            "category": self.category.value,
            "result": None if self.result is None else self.result.value,
            "input_receipt_digests": [str(item) for item in self.input_receipt_digests],
            "assertion_outcomes": [item.to_state() for item in self.assertion_outcomes],
        }

    @property
    def digest(self) -> Digest:
        return Digest.of_json(self.to_state())

    @classmethod
    def from_state(cls, state: object) -> EvidenceReceipt:
        data = _state(
            state,
            "schema_version source_revision base_revision head_revision build "
            "configuration_snapshot_digest evaluator environment observer capture_manifest "
            "started_at ended_at category result input_receipt_digests assertion_outcomes",
        )
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != RECEIPT_SCHEMA_VERSION
        ):
            raise InvalidInputError("unsupported evidence receipt schema version")
        return cls(
            source_revision=_text(data["source_revision"], "source revision"),
            base_revision=_text(data["base_revision"], "base revision"),
            head_revision=_text(data["head_revision"], "head revision"),
            build=BuildIdentity.from_state(data["build"]),
            configuration_snapshot_digest=_digest(data["configuration_snapshot_digest"]),
            evaluator=EvaluatorIdentity.from_state(data["evaluator"]),
            environment=EnvironmentFingerprint.from_state(data["environment"]),
            observer=ObserverIdentity.from_state(data["observer"]),
            capture_manifest=CaptureManifest.from_state(data["capture_manifest"]),
            started_at=_time(data["started_at"]),
            ended_at=_time(data["ended_at"]),
            category=_enum(ReceiptCategory, data["category"]),
            result=None if data["result"] is None else _enum(EvidenceResult, data["result"]),
            input_receipt_digests=tuple(
                _digest(item) for item in _items(data["input_receipt_digests"])
            ),
            assertion_outcomes=tuple(
                AssertionOutcome.from_state(item) for item in _items(data["assertion_outcomes"])
            ),
        )


__all__ = [
    "MAX_EVIDENCE_BYTES",
    "RECEIPT_SCHEMA_VERSION",
    "AssertionOutcome",
    "BuildIdentity",
    "BuildKind",
    "CaptureArtifact",
    "CaptureManifest",
    "DecodeStatus",
    "EnvironmentFingerprint",
    "EvaluatorIdentity",
    "EvidenceReceipt",
    "MediaDecodeResult",
    "ObserverIdentity",
    "ObserverKind",
    "ReceiptCategory",
    "ToolVersion",
]
