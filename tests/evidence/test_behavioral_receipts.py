from __future__ import annotations

import json
import struct
import zlib
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.domain.behavioral_evidence import (
    MAX_EVIDENCE_BYTES,
    AssertionOutcome,
    BuildIdentity,
    BuildKind,
    CaptureArtifact,
    CaptureManifest,
    DecodeStatus,
    EnvironmentFingerprint,
    EvaluatorIdentity,
    EvidenceReceipt,
    ObserverIdentity,
    ObserverKind,
    ReceiptCategory,
    ToolVersion,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.node_attempt import EvidenceResult
from enginery.evidence.media import decode_png, validate_media
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.errors import ArtifactDigestMismatchError, ArtifactMissingError
from enginery.ledger.evidence_receipts import EvidenceReceiptRepository


def _digest(label: str) -> Digest:
    return Digest.of_bytes(label.encode())


def _chunk(kind: bytes, data: bytes, *, bad_crc: bool = False) -> bytes:
    checksum = zlib.crc32(data, zlib.crc32(kind)) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum ^ int(bad_crc))


def _png(
    *, width: int = 1, height: int = 1, pixels: bytes = b"\x00\x12\x34\x56\xff",
    compressed: bytes | None = None, bad_crc: bool = False, interlace: int = 0,
) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, interlace)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(pixels) if compressed is None else compressed, bad_crc=bad_crc)
        + _chunk(b"IEND", b"")
    )


def _receipt(capture: CaptureArtifact | None = None) -> EvidenceReceipt:
    if capture is None:
        data = _png()
        capture = CaptureArtifact(_digest_bytes(data), "image/png", len(data), decode_png(data))
    return EvidenceReceipt(
        source_revision="issue-revision-1", base_revision="a" * 40, head_revision="b" * 40,
        build=BuildIdentity(BuildKind.SOURCE_TREE, _digest("verified head tree")),
        configuration_snapshot_digest=_digest("compiled configuration"),
        evaluator=EvaluatorIdentity(_digest("protected assertions"), "1.0"),
        environment=EnvironmentFingerprint(_digest("host receipt"), (ToolVersion("python", "3.12"),)),
        observer=ObserverIdentity("host-command-observer", ObserverKind.COMMAND, "1.0"),
        capture_manifest=CaptureManifest((capture,)),
        started_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
        ended_at=datetime(2026, 1, 2, 12, 0, 1, tzinfo=UTC),
        category=ReceiptCategory.INSTRUMENT_OBSERVATION,
    )


def _digest_bytes(data: bytes) -> Digest:
    return Digest.of_bytes(data)


def _verdict() -> EvidenceReceipt:
    observation = _receipt()
    return replace(
        observation, observer=ObserverIdentity("trusted-evaluator", ObserverKind.EVALUATOR, "1.0"),
        category=ReceiptCategory.EVALUATED_VERDICT, result=EvidenceResult.PASS,
        input_receipt_digests=(observation.digest,),
        assertion_outcomes=(AssertionOutcome("changed-path", EvidenceResult.PASS, ran=True),),
    )


def _change(state: dict[str, object], path: tuple[str | int, ...], value: object) -> dict[str, object]:
    changed = deepcopy(state)
    cursor: object = changed
    for key in path[:-1]:
        if isinstance(key, int):
            assert isinstance(cursor, list)
            cursor = cursor[key]
        else:
            assert isinstance(cursor, dict)
            cursor = cursor[key]
    last = path[-1]
    if isinstance(last, int):
        assert isinstance(cursor, list)
        cursor[last] = value
    else:
        assert isinstance(cursor, dict)
        cursor[last] = value
    return changed


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("source_revision",), "issue-revision-2"),
        (("base_revision",), "c" * 40),
        (("head_revision",), "c" * 40),
        (("build", "kind"), "candidate_artifact"),
        (("build", "digest"), str(_digest("other build"))),
        (("configuration_snapshot_digest",), str(_digest("other configuration"))),
        (("evaluator", "assertion_set_digest"), str(_digest("other assertions"))),
        (("evaluator", "version"), "2.0"),
        (("environment", "host_receipt_digest"), str(_digest("other host"))),
        (("environment", "tool_versions", 0, "name"), "other-tool"),
        (("environment", "tool_versions", 0, "version"), "3.13"),
        (("observer", "identity"), "other-observer"),
        (("observer", "kind"), "browser"),
        (("observer", "version"), "2.0"),
        (("observer", "mock"), True),
        (("capture_manifest", "artifacts", 0, "digest"), str(_digest("other raw bytes"))),
        (("capture_manifest", "artifacts", 0, "media_type"), "application/octet-stream"),
        (("capture_manifest", "artifacts", 0, "size_bytes"), 1),
        (("capture_manifest", "artifacts", 0, "decode", "decoder"), "other-decoder/1"),
        (("capture_manifest", "artifacts", 0, "decode", "width"), 2),
        (("capture_manifest", "artifacts", 0, "decode", "height"), 2),
        (("started_at",), "2026-01-02T11:59:59+00:00"),
        (("ended_at",), "2026-01-02T12:00:02+00:00"),
        (("input_receipt_digests",), [str(_digest("input observation"))]),
        (("assertion_outcomes",), [AssertionOutcome("x", EvidenceResult.FAIL, True).to_state()]),
    ],
)
def test_each_bound_provenance_field_changes_receipt_digest(
    path: tuple[str | int, ...], value: object,
) -> None:
    original = _receipt()
    changed = EvidenceReceipt.from_state(_change(original.to_state(), path, value))
    assert changed.digest != original.digest


def test_decode_status_reason_and_category_are_bound() -> None:
    original = _receipt()
    capture = original.capture_manifest.artifacts[0]
    invalid = replace(capture.decode, status=DecodeStatus.INVALID, reason="corrupted")
    failed = replace(original, capture_manifest=CaptureManifest((replace(capture, decode=invalid),)))
    other_reason = replace(invalid, reason="truncated")
    assert failed.digest != original.digest
    assert replace(
        failed, capture_manifest=CaptureManifest((replace(capture, decode=other_reason),)),
    ).digest != failed.digest
    worker_observer = ObserverIdentity("worker", ObserverKind.WORKER, "1.0")
    worker = replace(original, category=ReceiptCategory.WORKER_CLAIM, observer=worker_observer)
    assert worker.digest != original.digest
    assert worker.result is None
    with pytest.raises(InvalidInputError, match="cannot carry"):
        replace(worker, result=EvidenceResult.PASS)
    with pytest.raises(InvalidInputError):
        replace(original, category=ReceiptCategory.EVALUATED_VERDICT, result=EvidenceResult.PASS)


def test_evaluated_verdict_binds_result_input_receipts_and_assertion_outcomes() -> None:
    verdict = _verdict()
    assert replace(verdict, result=EvidenceResult.INDETERMINATE).digest != verdict.digest
    assert replace(verdict, input_receipt_digests=(_digest("other input"),)).digest != verdict.digest
    assert replace(
        verdict, assertion_outcomes=(AssertionOutcome("other-assertion", EvidenceResult.PASS, True),),
    ).digest != verdict.digest
    failed = replace(
        verdict, result=EvidenceResult.FAIL,
        assertion_outcomes=(AssertionOutcome("changed-path", EvidenceResult.FAIL, True),),
    )
    assert failed.digest != verdict.digest
    assert replace(
        failed, assertion_outcomes=(AssertionOutcome("changed-path", EvidenceResult.FAIL, False),),
    ).digest != failed.digest
    assert replace(
        failed, assertion_outcomes=(AssertionOutcome("changed-path", EvidenceResult.FAIL, True, True),),
    ).digest != failed.digest


def test_receipt_serialization_is_canonical_and_immutable() -> None:
    receipt = _verdict()
    state = receipt.to_state()
    assert EvidenceReceipt.from_state(json.loads(json.dumps(state))) == receipt
    assert receipt.digest == Digest.of_json(state)
    assert receipt.digest == Digest.of_json(dict(reversed(list(state.items()))))
    with pytest.raises(FrozenInstanceError):
        setattr(receipt, "head_revision", "changed")
    state["head_revision"] = "changed"
    assert receipt.head_revision == "b" * 40
    with pytest.raises(InvalidInputError, match="immutable tuple"):
        EnvironmentFingerprint(_digest("host"), [ToolVersion("x", "1")])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("unexpected",), "value"),
        (("schema_version",), True),
        (("schema_version",), 2),
        (("head_revision",), ""),
        (("started_at",), "2026-01-02T12:00:00"),
        (("ended_at",), "2025-01-01T00:00:00+00:00"),
        (("observer", "mock"), "false"),
        (("observer", "kind"), "invented"),
        (("build", "digest"), "not-a-digest"),
        (("environment", "tool_versions"), []),
        (("capture_manifest", "artifacts", 0, "size_bytes"), True),
        (("capture_manifest", "artifacts", 0, "decode", "width"), -1),
        (("capture_manifest", "artifacts", 0, "decode", "width"), None),
        (("capture_manifest", "artifacts", 0, "media_type"), "image/png\n"),
    ],
)
def test_malformed_receipt_is_refused(path: tuple[str | int, ...], value: object) -> None:
    with pytest.raises(InvalidInputError):
        EvidenceReceipt.from_state(_change(_receipt().to_state(), path, value))


def test_zero_skipped_and_mock_verdicts_cannot_pass() -> None:
    verdict = _verdict()
    with pytest.raises(InvalidInputError):
        replace(verdict, assertion_outcomes=())
    with pytest.raises(InvalidInputError):
        AssertionOutcome("skipped", EvidenceResult.PASS, ran=False, skipped=True)
    with pytest.raises(InvalidInputError):
        replace(verdict, observer=replace(verdict.observer, mock=True))


@pytest.mark.parametrize("interlace", [0, 1])
def test_valid_png_records_dimensions_without_semantic_success(interlace: int) -> None:
    decoded = decode_png(_png(interlace=interlace))
    assert decoded.status is DecodeStatus.DECODABLE
    assert (decoded.width, decoded.height) == (1, 1)
    observation = _receipt()
    assert observation.result is None
    with pytest.raises(InvalidInputError):
        replace(
            observation, category=ReceiptCategory.EVALUATED_VERDICT,
            observer=ObserverIdentity("evaluator", ObserverKind.EVALUATOR, "1"),
            input_receipt_digests=(observation.digest,), result=EvidenceResult.PASS,
        )


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"not png", "png_signature"),
        (_png(bad_crc=True), "png_chunk_crc"),
        (_png(compressed=b"not zlib"), "png_zlib"),
        (_png(width=0), "png_dimensions"),
        (_png(width=16385), "png_dimensions"),
        (_png(height=0), "png_dimensions"),
        (_png(pixels=b"\x00\x00"), "png_scanline_bytes"),
        (_png(pixels=b"\x00" + b"\x00" * 5), "png_scanline_bytes"),
        (_png(pixels=b"\x05\x12\x34\x56\xff"), "png_scanline_filter"),
        (_png(compressed=zlib.compress(b"\x00\x12\x34\x56\xff")[:-1]), "png_scanline_bytes"),
        (_png(compressed=zlib.compress(b"\x00\x12\x34\x56\xff") + b"extra"), "png_scanline_bytes"),
        (_png() + b"extra", "png_iend"),
        (_png(width=16384, height=16384), "png_decoded_limit"),
    ],
)
def test_png_rejects_bad_structure_compression_dimensions_and_scanlines(
    data: bytes, reason: str,
) -> None:
    decoded = decode_png(data)
    assert decoded.status is DecodeStatus.INVALID
    assert decoded.reason == reason


def test_png_validates_every_scanline_and_adam7_pass() -> None:
    rows = b"\x00\x12\x34\x56\xff" + b"\x04\x11\x22\x33\xff"
    assert decode_png(_png(height=2, pixels=rows)).status is DecodeStatus.DECODABLE
    assert decode_png(_png(height=2, pixels=rows[:-5] + b"\x05" + rows[-4:])).reason == (
        "png_scanline_filter"
    )
    # A 2x2 Adam7 image has three populated passes, one of which has two pixels.
    adam7 = b"\x00" + bytes(4) + b"\x00" + bytes(4) + b"\x00" + bytes(8)
    assert decode_png(_png(width=2, height=2, interlace=1, pixels=adam7)).status is (
        DecodeStatus.DECODABLE
    )
    assert decode_png(_png(width=2, height=2, interlace=1, pixels=adam7[:-1])).reason == (
        "png_scanline_bytes"
    )


def test_receipt_and_raw_media_survive_repository_restart(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    repository = EvidenceReceiptRepository(store, media_validator=validate_media)
    raw = _png()
    capture = repository.publish_capture(raw, media_type="image/png")
    receipt = _receipt(capture)
    digest = repository.publish(receipt)
    restarted = EvidenceReceiptRepository(
        ArtifactStore(store.root), media_validator=validate_media,
    )
    assert restarted.load(digest) == receipt
    assert store.read_bytes(capture.digest) == raw
    assert json.loads(store.read_bytes(digest)) == receipt.to_state()
    assert digest == receipt.digest
    for path in (store.root, store.root / "objects", store.path_for(digest).parent):
        assert path.stat().st_mode & 0o777 == 0o700


def test_invalid_media_is_retained_as_failure_not_silently_dropped(tmp_path: Path) -> None:
    repository = EvidenceReceiptRepository(
        ArtifactStore(tmp_path / "artifacts"), media_validator=validate_media,
    )
    capture = repository.publish_capture(_png(bad_crc=True), media_type="image/png")
    receipt = _receipt(capture)
    assert capture.decode.status is DecodeStatus.INVALID
    loaded = repository.load(repository.publish(receipt))
    assert loaded.capture_manifest.artifacts[0].decode.reason == "png_chunk_crc"
    assert loaded.result is None


def test_receipt_requires_retained_raw_bytes_and_true_decode_metadata(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    repository = EvidenceReceiptRepository(store, media_validator=validate_media)
    with pytest.raises(ArtifactMissingError):
        repository.publish(_receipt())
    capture = repository.publish_capture(_png(), media_type="image/png")
    for changed in (
        replace(capture, size_bytes=capture.size_bytes + 1),
        replace(capture, decode=replace(capture.decode, width=2)),
    ):
        with pytest.raises(InvalidInputError):
            repository.publish(_receipt(changed))
    receipt = _receipt(capture)
    digest = repository.publish(receipt)
    store.path_for(capture.digest).write_bytes(b"corrupted")
    with pytest.raises(ArtifactDigestMismatchError):
        repository.load(digest)


def test_receipt_load_rechecks_decode_metadata_even_with_valid_artifact_digest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    repository = EvidenceReceiptRepository(store, media_validator=validate_media)
    capture = repository.publish_capture(_png(), media_type="image/png")
    forged = _receipt(replace(capture, decode=replace(capture.decode, width=99)))
    raw = json.dumps(forged.to_state(), sort_keys=True, separators=(",", ":")).encode()
    digest = store.publish_bytes(raw, media_type="application/json")
    with pytest.raises(InvalidInputError, match="decode result"):
        repository.load(digest)


def test_repository_refuses_oversized_bytes_before_read_or_publication(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    repository = EvidenceReceiptRepository(store, media_validator=validate_media)
    huge = b"x" * (MAX_EVIDENCE_BYTES + 1)
    with pytest.raises(InvalidInputError, match="1 MiB"):
        repository.publish_capture(huge, media_type="text/plain")
    digest = store.publish_bytes(huge)
    with pytest.raises(InvalidInputError, match="1 MiB"):
        repository.load(digest)
    assert decode_png(huge).reason == "png_input_limit"


def test_repository_refuses_raw_artifact_symlink(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    repository = EvidenceReceiptRepository(store, media_validator=validate_media)
    capture = repository.publish_capture(_png(), media_type="image/png")
    receipt = _receipt(capture)
    digest = repository.publish(receipt)
    artifact_path = store.path_for(capture.digest)
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png())
    artifact_path.unlink()
    artifact_path.symlink_to(outside)
    with pytest.raises(OSError):
        repository.load(digest)
    with pytest.raises(InvalidInputError, match="non-link"):
        repository.publish_capture(_png(), media_type="image/png")
