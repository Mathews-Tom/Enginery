from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterEventKind,
    AdapterFingerprint,
    AdapterStatus,
    NormalizedAdapterEvent,
    NormalizedAdapterFailure,
    ProviderKind,
    require_matching_fingerprints,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import FailureClass, InvalidInputError, MissingPrerequisiteError
from enginery.domain.ids import OperationId


def _fingerprint(*, provider_version: str = "1.0.0") -> AdapterFingerprint:
    return AdapterFingerprint(
        provider_id="local-git",
        provider_version=provider_version,
        api_version=ADAPTER_API_VERSION,
        capabilities=(
            AdapterCapability(name="reconcile", version=1),
            AdapterCapability(name="branches", version=1),
        ),
    )


def _status(fingerprint: AdapterFingerprint | None = None) -> AdapterStatus:
    return AdapterStatus(
        kind=ProviderKind.SOURCE_CONTROL,
        availability=AdapterAvailability.AVAILABLE,
        fingerprint=fingerprint or _fingerprint(),
        detail="local git is available",
    )


def test_fingerprint_digest_is_canonical_across_capability_order() -> None:
    first = _fingerprint()
    second = AdapterFingerprint(
        provider_id="local-git",
        provider_version="1.0.0",
        api_version=ADAPTER_API_VERSION,
        capabilities=(
            AdapterCapability(name="branches", version=1),
            AdapterCapability(name="reconcile", version=1),
        ),
    )

    assert first.capabilities == second.capabilities
    assert first.digest == second.digest


@pytest.mark.parametrize(
    ("name", "version"),
    [
        (" ", 1),
        ("reconcile", 0),
    ],
)
def test_capability_rejects_invalid_identity(name: str, version: int) -> None:
    with pytest.raises(InvalidInputError):
        AdapterCapability(name=name, version=version)


def test_fingerprint_rejects_incompatible_api_version() -> None:
    with pytest.raises(InvalidInputError, match="API version"):
        AdapterFingerprint(
            provider_id="local-git",
            provider_version="1.0.0",
            api_version=ADAPTER_API_VERSION + 1,
        )


def test_fingerprint_rejects_duplicate_capability_names() -> None:
    with pytest.raises(InvalidInputError, match="unique"):
        AdapterFingerprint(
            provider_id="local-git",
            provider_version="1.0.0",
            api_version=ADAPTER_API_VERSION,
            capabilities=(
                AdapterCapability(name="reconcile", version=1),
                AdapterCapability(name="reconcile", version=2),
            ),
        )


def test_available_status_requires_fingerprint() -> None:
    with pytest.raises(InvalidInputError, match="require a fingerprint"):
        AdapterStatus(
            kind=ProviderKind.HARNESS,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=None,
            detail="scripted harness is available",
        )


def test_status_without_availability_cannot_smuggle_fingerprint() -> None:
    with pytest.raises(InvalidInputError, match="cannot report a fingerprint"):
        AdapterStatus(
            kind=ProviderKind.HARNESS,
            availability=AdapterAvailability.UNAVAILABLE,
            fingerprint=_fingerprint(),
            detail="scripted harness is missing",
        )


def test_fingerprint_drift_blocks_before_provider_use() -> None:
    bound = {"local-git": _fingerprint().digest}

    with pytest.raises(MissingPrerequisiteError, match="fingerprint changed"):
        require_matching_fingerprints(
            bound,
            {"local-git": _status(_fingerprint(provider_version="2.0.0"))},
        )


def test_missing_bound_provider_blocks_before_provider_use() -> None:
    with pytest.raises(MissingPrerequisiteError, match="not configured"):
        require_matching_fingerprints({"local-git": Digest.of_bytes(b"bound")}, {})

    with pytest.raises(MissingPrerequisiteError, match="no bound"):
        require_matching_fingerprints({}, {})


def test_matching_fingerprints_allow_provider_use() -> None:
    fingerprint = _fingerprint()

    require_matching_fingerprints(
        {"local-git": fingerprint.digest},
        {"local-git": _status(fingerprint)},
    )


@pytest.mark.parametrize("retry_after_seconds", [-1.0, float("nan"), float("inf")])
def test_normalized_failure_rejects_invalid_retry_delay(retry_after_seconds: float) -> None:
    with pytest.raises(InvalidInputError, match="finite and non-negative"):
        NormalizedAdapterFailure(
            failure_class=FailureClass.TRANSIENT_PROVIDER_FAILURE,
            summary="provider timeout",
            retry_after_seconds=retry_after_seconds,
        )


def test_normalized_failure_preserves_classification() -> None:
    failure = NormalizedAdapterFailure(
        failure_class=FailureClass.RATE_LIMIT,
        summary="provider rate limit exceeded",
        retry_after_seconds=60.0,
    )

    assert failure.failure_class is FailureClass.RATE_LIMIT


def test_event_only_carries_redacted_metadata_and_artifact_digest() -> None:
    attributes = {"phase": "validated"}
    event = NormalizedAdapterEvent(
        kind=AdapterEventKind.TERMINAL,
        occurred_at=datetime(2026, 7, 19, tzinfo=UTC),
        operation_id=OperationId("op-1"),
        summary="validation completed",
        attributes=attributes,
        output_digest=Digest.of_bytes(b"redacted output"),
    )
    attributes["phase"] = "tampered"

    assert event.attributes == {"phase": "validated"}
    assert event.output_digest == Digest.of_bytes(b"redacted output")
