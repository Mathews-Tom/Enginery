from __future__ import annotations

from datetime import UTC, datetime

import pytest

from enginery.capabilities.approval import approved_capability_names, materialize_approval_schema
from enginery.capabilities.lock import (
    CapabilityLock,
    LockedCapability,
    ProvenanceRecord,
    ProvenanceStatus,
)
from enginery.domain.digests import Digest
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_OPERATOR = AuthorityPrincipal(
    id="operator-1", principal_type=PrincipalType.HUMAN, role="operator", authorization_source="cli"
)
_RUN = AuthorityPrincipal(
    id="run-1", principal_type=PrincipalType.AGENT, role="run", authorization_source="coordinator"
)


def _entry(
    name: str,
    payload: bytes,
    *,
    introduced_by_run: bool,
    status: ProvenanceStatus,
) -> LockedCapability:
    return LockedCapability(
        name=name,
        version="1",
        digest=Digest.of_bytes(payload),
        provenance=ProvenanceRecord(
            status=status, source_label="test-source", signer_key_id=None, verified_at=_NOW
        ),
        license="MIT",
        introduced_by_run=introduced_by_run,
    )


def test_materialize_approval_schema_rejects_a_trusted_entry() -> None:
    trusted = _entry(
        "skill-a", b"payload", introduced_by_run=False, status=ProvenanceStatus.LOCAL_TRUSTED
    )
    lock = CapabilityLock(entries=(trusted,))

    with pytest.raises(ValueError, match="only a pending"):
        materialize_approval_schema(trusted, lock, requesting_principal_id=_RUN.id)


def test_materialize_approval_schema_binds_the_lock_and_entry_digests() -> None:
    pending = _entry(
        "skill-a", b"payload", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED
    )
    lock = CapabilityLock(entries=(pending,))

    schema = materialize_approval_schema(pending, lock, requesting_principal_id=_RUN.id)

    assert schema.capability_locks == str(lock.digest())
    assert schema.diff_or_artifact_digest == str(pending.digest)
    assert schema.capability_introduced_by_run is True
    assert schema.requesting_principal_id == _RUN.id


def test_approved_capability_names_includes_reviewed_base_without_any_approval() -> None:
    trusted = _entry(
        "skill-a", b"payload", introduced_by_run=False, status=ProvenanceStatus.LOCAL_TRUSTED
    )
    lock = CapabilityLock(entries=(trusted,))
    evaluator = PolicyEvaluator("policy-v1")

    names = approved_capability_names(lock, evaluator, requesting_principal_id=_RUN.id)

    assert names == frozenset({"skill-a"})


def test_approved_capability_names_excludes_an_unapproved_run_introduced_entry() -> None:
    pending = _entry(
        "skill-a", b"payload", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED
    )
    lock = CapabilityLock(entries=(pending,))
    registry = ApprovalRegistry([_OPERATOR])
    evaluator = PolicyEvaluator("policy-v1", approval_registry=registry)

    names = approved_capability_names(lock, evaluator, requesting_principal_id=_RUN.id)

    assert names == frozenset()


def test_approved_capability_names_includes_an_approved_run_introduced_entry() -> None:
    pending = _entry(
        "skill-a", b"payload", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED
    )
    lock = CapabilityLock(entries=(pending,))
    registry = ApprovalRegistry([_OPERATOR])
    evaluator = PolicyEvaluator("policy-v1", approval_registry=registry)
    schema = materialize_approval_schema(pending, lock, requesting_principal_id=_RUN.id)
    registry.record_approval(schema, [_OPERATOR])

    names = approved_capability_names(lock, evaluator, requesting_principal_id=_RUN.id)

    assert names == frozenset({"skill-a"})


def test_approved_capability_names_drops_a_prior_approval_after_the_lock_changes() -> None:
    """Changing the rest of the lock supersedes an entry's own prior approval."""

    pending = _entry(
        "skill-a", b"payload", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED
    )
    original_lock = CapabilityLock(entries=(pending,))
    registry = ApprovalRegistry([_OPERATOR])
    evaluator = PolicyEvaluator("policy-v1", approval_registry=registry)
    schema = materialize_approval_schema(pending, original_lock, requesting_principal_id=_RUN.id)
    registry.record_approval(schema, [_OPERATOR])

    other_pending = _entry(
        "skill-b", b"other-payload", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED
    )
    changed_lock = CapabilityLock(entries=(pending, other_pending))

    names = approved_capability_names(changed_lock, evaluator, requesting_principal_id=_RUN.id)

    assert "skill-a" not in names


def test_run_cannot_approve_its_own_introduced_capability() -> None:
    """Producer separation: the requesting run principal cannot also be the approver."""

    pending = _entry(
        "skill-a", b"payload", introduced_by_run=True, status=ProvenanceStatus.UNAUTHENTICATED
    )
    lock = CapabilityLock(entries=(pending,))
    registry = ApprovalRegistry([_OPERATOR])
    schema = materialize_approval_schema(pending, lock, requesting_principal_id=_RUN.id)

    with pytest.raises(Exception, match="producer separation"):
        registry.record_approval(schema, [_RUN])
