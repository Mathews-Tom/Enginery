from __future__ import annotations

from dataclasses import replace

import pytest

from enginery.capabilities.approval import approved_capabilities, materialize_approval_schema
from enginery.capabilities.lock import (
    ApprovedCapabilities,
    CapabilityApproval,
    CapabilityLock,
    LockedCapability,
)
from enginery.domain.principal import AuthorityPrincipal, PrincipalType
from enginery.policy.approval import ApprovalRegistry
from enginery.policy.evaluator import PolicyEvaluator
from tests.support.capabilities import (
    approvals_for,
    lock_of,
    locked_entry,
    package_files,
    replace_file,
)

_OPERATOR = AuthorityPrincipal(
    id="operator-1", principal_type=PrincipalType.HUMAN, role="operator", authorization_source="cli"
)
_RUN = AuthorityPrincipal(
    id="run-1", principal_type=PrincipalType.AGENT, role="run", authorization_source="coordinator"
)


def _approved(lock: CapabilityLock, entry: LockedCapability) -> PolicyEvaluator:
    registry = ApprovalRegistry([_OPERATOR])
    evaluator = PolicyEvaluator("policy-v1", approval_registry=registry)
    schema = materialize_approval_schema(entry, lock, requesting_principal_id=_RUN.id)
    registry.record_approval(schema, [_OPERATOR])
    return evaluator


def test_materialize_approval_schema_rejects_a_trusted_entry() -> None:
    trusted = locked_entry("skill-a", introduced_by_run=False)
    lock = lock_of(trusted)

    with pytest.raises(ValueError, match="only a pending"):
        materialize_approval_schema(trusted, lock, requesting_principal_id=_RUN.id)


def test_materialize_approval_schema_binds_the_closure_and_whole_lock_digests() -> None:
    pending = locked_entry("skill-a", introduced_by_run=True)
    lock = lock_of(pending)

    schema = materialize_approval_schema(pending, lock, requesting_principal_id=_RUN.id)

    assert schema.capability_locks == str(lock.digest())
    assert schema.diff_or_artifact_digest == str(pending.digest)
    assert schema.capability_introduced_by_run is True
    assert schema.requesting_principal_id == _RUN.id


def test_a_reviewed_base_entry_is_covered_without_any_approval() -> None:
    entry = locked_entry("skill-a", introduced_by_run=False)
    lock = lock_of(entry)
    evaluator = PolicyEvaluator("policy-v1")

    approvals = approved_capabilities(lock, evaluator, requesting_principal_id=_RUN.id)

    assert approvals.approvals == frozenset()
    assert approvals.covers(entry, lock.digest())


def test_an_unapproved_run_introduced_entry_is_not_covered() -> None:
    entry = locked_entry("skill-a", introduced_by_run=True)
    lock = lock_of(entry)
    evaluator = PolicyEvaluator("policy-v1", approval_registry=ApprovalRegistry([_OPERATOR]))

    approvals = approved_capabilities(lock, evaluator, requesting_principal_id=_RUN.id)

    assert approvals.names() == frozenset()
    assert not approvals.covers(entry, lock.digest())


def test_an_approved_run_introduced_entry_is_covered_by_its_closure_and_lock_digests() -> None:
    pending = locked_entry("skill-a", introduced_by_run=True)
    lock = lock_of(pending)

    approvals = approved_capabilities(
        lock, _approved(lock, pending), requesting_principal_id=_RUN.id
    )

    assert approvals == ApprovedCapabilities(
        frozenset({CapabilityApproval("skill-a", pending.digest, lock.digest())})
    )
    assert approvals.covers(pending, lock.digest())


def test_an_approval_computed_for_one_lock_covers_no_other_lock() -> None:
    """A bare name never authorizes: the same entry under a drifted lock is uncovered."""

    pending = locked_entry("skill-a", introduced_by_run=True)
    original_lock = lock_of(pending)
    approvals = approved_capabilities(
        original_lock, _approved(original_lock, pending), requesting_principal_id=_RUN.id
    )
    drifted_lock = lock_of(pending, locked_entry("skill-b", introduced_by_run=True))

    assert approvals.covers(pending, original_lock.digest())
    assert not approvals.covers(pending, drifted_lock.digest())


def test_approvals_do_not_cover_a_different_closure_under_the_same_name() -> None:
    approved_entry = locked_entry(files=package_files("skill-a"), introduced_by_run=True)
    lock = lock_of(approved_entry)
    approvals = approvals_for(lock, ["skill-a"])
    changed = package_files("skill-a", body=b"# changed\n")
    drifted = locked_entry(files=changed, introduced_by_run=True)

    assert approvals.covers(approved_entry, lock.digest())
    assert not approvals.covers(drifted, lock.digest())


def test_the_approval_set_drops_a_prior_approval_after_the_lock_changes() -> None:
    """Changing the rest of the lock supersedes an entry's own prior approval."""

    pending = locked_entry("skill-a", introduced_by_run=True)
    original_lock = lock_of(pending)
    evaluator = _approved(original_lock, pending)
    changed_lock = lock_of(pending, locked_entry("skill-b", introduced_by_run=True))

    approvals = approved_capabilities(changed_lock, evaluator, requesting_principal_id=_RUN.id)

    assert "skill-a" not in approvals.names()


def test_a_sibling_script_change_supersedes_a_prior_approval() -> None:
    """The acceptance case: approve a closure, change only a sibling script, and the
    old approval no longer applies to the new closure or the new lock."""

    base = package_files("skill-a", extra={"scripts/run.py": b"print('reviewed')\n"})
    approved_entry = locked_entry(files=base, introduced_by_run=True)
    approved_lock = lock_of(approved_entry)
    evaluator = _approved(approved_lock, approved_entry)
    assert approved_capabilities(
        approved_lock, evaluator, requesting_principal_id=_RUN.id
    ).names() == frozenset({"skill-a"})

    changed = replace_file(base, "scripts/run.py", b"print('changed after approval')\n")
    changed_entry = locked_entry(files=changed, introduced_by_run=True)

    approvals = approved_capabilities(
        lock_of(changed_entry), evaluator, requesting_principal_id=_RUN.id
    )

    assert changed_entry.digest != approved_entry.digest
    assert approvals.names() == frozenset()


def test_a_reference_change_supersedes_a_prior_approval() -> None:
    base = package_files("skill-a")
    approved_entry = locked_entry(files=base, introduced_by_run=True)
    approved_lock = lock_of(approved_entry)
    evaluator = _approved(approved_lock, approved_entry)
    changed = replace_file(base, "references/guide.md", b"edited after approval")

    approvals = approved_capabilities(
        lock_of(locked_entry(files=changed, introduced_by_run=True)),
        evaluator,
        requesting_principal_id=_RUN.id,
    )

    assert approvals.names() == frozenset()


def test_a_license_or_origin_change_supersedes_a_prior_approval() -> None:
    pending = locked_entry("skill-a", introduced_by_run=True)
    evaluator = _approved(lock_of(pending), pending)
    relicensed = replace(pending, license="GPL-3.0")
    moved = replace(pending, revision="rev-2")
    assert relicensed.digest == moved.digest == pending.digest

    for variant in (relicensed, moved):
        approvals = approved_capabilities(
            lock_of(variant), evaluator, requesting_principal_id=_RUN.id
        )
        assert approvals.names() == frozenset()


def test_run_cannot_approve_its_own_introduced_capability() -> None:
    """Producer separation: the requesting run principal cannot also be the approver."""

    pending = locked_entry("skill-a", introduced_by_run=True)
    lock = lock_of(pending)
    registry = ApprovalRegistry([_OPERATOR])
    schema = materialize_approval_schema(pending, lock, requesting_principal_id=_RUN.id)

    with pytest.raises(Exception, match="producer separation"):
        registry.record_approval(schema, [_RUN])
