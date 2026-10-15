"""A recording stand-in for the branch-publication port, for tests that bypass real nodes."""

from __future__ import annotations

from dataclasses import dataclass, field

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.branch_publication import (
    BranchPublicationRequest,
    PublishedBranch,
)
from enginery.domain.errors import ExternalConflictError
from enginery.domain.node_attempt import ReconciliationResult

HEAD_REVISION = "1" * 40
"""The verified head recorded on an implementation attempt a test completes by hand."""


@dataclass
class RecordingBranchPublisher:
    """Records every publication request and reports the requested revision as published.

    ``remote`` models the remote branch revision: a publication whose expectation differs from it
    is an external conflict, exactly as the real publisher treats a branch it did not expect.
    """

    requests: list[BranchPublicationRequest] = field(default_factory=list)
    remote: str | None = None
    failures_remaining: int = 0
    changed: tuple[str, ...] = ()
    """The paths every ``changed_paths`` call reports, whatever the revisions asked for."""
    changed_path_requests: list[tuple[str, str]] = field(default_factory=list)

    def probe(self) -> AdapterStatus:
        return AdapterStatus(
            kind=ProviderKind.SOURCE_CONTROL,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="recording-branch-publisher",
                provider_version="1",
                api_version=ADAPTER_API_VERSION,
            ),
            detail="recording branch publisher",
        )

    def changed_paths(self, *, base_revision: str, head_revision: str) -> tuple[str, ...]:
        self.changed_path_requests.append((base_revision, head_revision))
        return self.changed

    def publish(self, request: BranchPublicationRequest) -> PublishedBranch:
        self.requests.append(request)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise ExternalConflictError("simulated publication interruption")
        if self.remote != request.head_revision and self.remote != request.expected_remote_revision:
            raise ExternalConflictError("the remote branch is not where this publication expects")
        self.remote = request.head_revision
        return PublishedBranch(branch=request.branch, revision=request.head_revision)

    def reconcile(self, request: BranchPublicationRequest) -> ReconciliationResult:
        if self.remote == request.head_revision:
            return ReconciliationResult.FOUND_MATCHING
        return ReconciliationResult.NOT_FOUND
