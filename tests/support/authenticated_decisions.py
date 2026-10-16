"""Real policy/ledger decisions with an already-verified local terminal test identity."""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from datetime import timedelta

import pytest

from enginery.application.human_decisions import (
    AuthenticatedDecisionContext,
    HumanDecision,
    ReviewAuthority,
)
from enginery.application.review_packet import ReviewPacket
from enginery.cli import factory_decisions
from enginery.ledger.human_decisions import issue_challenge, list_decisions
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import Stage1RunRequest


@dataclass(frozen=True)
class AuthenticatedDecisions:
    """Simulate ingress identity, never policy evaluation or publication authorization."""

    ledger: LedgerService

    @property
    def registration(self) -> ReviewAuthority:
        return ReviewAuthority("test-operator", getpass.getuser(), os.getuid())

    def packet(self, request: Stage1RunRequest) -> ReviewPacket:
        return factory_decisions.build_packet(request.repository_path, self.ledger, request)

    def approve(
        self,
        request: Stage1RunRequest,
        *,
        action: str = "pull_request.open",
        reason: str = "Approve the exact reviewed test evidence",
        owner: str = "coordinator",
    ) -> dict[str, object]:
        registration = self.registration
        context = AuthenticatedDecisionContext(
            registration.principal, registration.os_uid, "/dev/ttys-test"
        )
        packet = self.packet(request)
        issued = issue_challenge(
            self.ledger.connection,
            run_id=str(request.run.id),
            subject_fields=packet.subject_fields(),
            actions=factory_decisions._actions(self.ledger, request),
            context=context,
            registered_humans=registration.registered_humans,
            lifetime=timedelta(seconds=registration.ttl_seconds),
        )
        # Authentication ingress itself is covered by the CLI's real-PTY tests. Everything
        # following that boundary is the same verifier, transaction and durable write.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(factory_decisions, "_authenticate", lambda _: (registration, context))
            patch.setattr(factory_decisions, "_registered_identity", lambda *_: registration)
            return factory_decisions.decide_in_ledger(
                self.ledger,
                request,
                action=action,
                subject_digest=issued.subject_digest,
                challenge=issued.challenge,
                reason=reason,
                owner=owner,
            )

    def decide(
        self,
        request: Stage1RunRequest,
        *,
        action: str = "pull_request.open",
        reason: str = "Approve the exact reviewed test evidence",
        owner: str = "coordinator",
    ) -> HumanDecision:
        result = self.approve(request, action=action, reason=reason, owner=owner)
        return next(
            decision
            for decision in list_decisions(self.ledger.connection, str(request.run.id))
            if decision.id == result["decision_id"]
        )

    def authorize_publication(self, request: Stage1RunRequest) -> str:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(factory_decisions, "_registered_identity", lambda *_: self.registration)
            return factory_decisions.require_for_effect(
                self.ledger, request, "pull_request.open"
            ).id
