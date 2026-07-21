"""Tests for enginery.incidents.authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from enginery.domain.errors import InvalidInputError
from enginery.domain.policy_decision import PolicyAction
from enginery.incidents.authority import (
    DEFAULT_GRANT_TTL,
    DeploymentGrant,
    DeploymentGrantExpiredError,
    issue_grant,
)

_NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _make_grant(**overrides: object) -> DeploymentGrant:
    defaults: dict[str, object] = {
        "grant_id": "grant-1",
        "action": PolicyAction.DEPLOYMENT_EXECUTE,
        "target": "127.0.0.1:8765",
        "principal_id": "human-1",
        "issued_at": _NOW,
        "expires_at": _NOW + timedelta(minutes=5),
    }
    defaults.update(overrides)
    return DeploymentGrant(**defaults)  # type: ignore[arg-type]


class TestDeploymentGrant:
    def test_valid_grant_constructs(self) -> None:
        grant = _make_grant()
        assert grant.action is PolicyAction.DEPLOYMENT_EXECUTE

    def test_rejects_a_non_deployment_action(self) -> None:
        with pytest.raises(InvalidInputError, match="deployment"):
            _make_grant(action=PolicyAction.RELEASE_PUBLISH)

    def test_rejects_blank_target(self) -> None:
        with pytest.raises(InvalidInputError, match="target"):
            _make_grant(target=" ")

    def test_rejects_blank_principal_id(self) -> None:
        with pytest.raises(InvalidInputError, match="principal_id"):
            _make_grant(principal_id=" ")

    def test_rejects_expires_at_not_after_issued_at(self) -> None:
        with pytest.raises(InvalidInputError, match="expires_at"):
            _make_grant(expires_at=_NOW)

    def test_accepts_rollback_action(self) -> None:
        grant = _make_grant(action=PolicyAction.DEPLOYMENT_ROLLBACK)
        assert grant.action is PolicyAction.DEPLOYMENT_ROLLBACK


class TestRequireNotExpired:
    def test_does_not_raise_before_expiry(self) -> None:
        grant = _make_grant()
        grant.require_not_expired(reference_time=_NOW + timedelta(seconds=1))

    def test_raises_at_exact_expiry(self) -> None:
        grant = _make_grant()
        with pytest.raises(DeploymentGrantExpiredError):
            grant.require_not_expired(reference_time=grant.expires_at)

    def test_raises_after_expiry(self) -> None:
        grant = _make_grant()
        with pytest.raises(DeploymentGrantExpiredError):
            grant.require_not_expired(reference_time=grant.expires_at + timedelta(minutes=1))


class TestIssueGrant:
    def test_uses_the_default_ttl(self) -> None:
        grant = issue_grant(
            action=PolicyAction.DEPLOYMENT_EXECUTE,
            target="127.0.0.1:8765",
            principal_id="human-1",
            issued_at=_NOW,
        )
        assert grant.expires_at == _NOW + DEFAULT_GRANT_TTL

    def test_accepts_a_custom_ttl(self) -> None:
        grant = issue_grant(
            action=PolicyAction.DEPLOYMENT_ROLLBACK,
            target="127.0.0.1:8765",
            principal_id="human-1",
            issued_at=_NOW,
            ttl=timedelta(seconds=30),
        )
        assert grant.expires_at == _NOW + timedelta(seconds=30)

    def test_each_grant_has_a_unique_id(self) -> None:
        first = issue_grant(
            action=PolicyAction.DEPLOYMENT_EXECUTE,
            target="t",
            principal_id="human-1",
            issued_at=_NOW,
        )
        second = issue_grant(
            action=PolicyAction.DEPLOYMENT_EXECUTE,
            target="t",
            principal_id="human-1",
            issued_at=_NOW,
        )
        assert first.grant_id != second.grant_id
