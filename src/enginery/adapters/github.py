"""GitHub CLI-backed adapters that emit only normalized application values."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TypeGuard, cast
from urllib.parse import quote

from enginery.adapters.subprocess_output import SubprocessOutputError, execute_result
from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.delivery_ports import PublicationReceipt, PublicationRequest
from enginery.application.work_classification import (
    IssueCandidate,
    declared_classification,
    label_names,
)
from enginery.application.work_ports import (
    EvidenceCompleteness,
    LifecycleProjection,
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestRequest,
    PullRequestReview,
    PullRequestSnapshot,
    PullRequestStatus,
    WorkLedgerSnapshot,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import (
    AmbiguousExternalSideEffectError,
    AuthenticationFailureError,
    ExternalConflictError,
    InvalidInputError,
    MissingPrerequisiteError,
    RateLimitError,
    StaleEvidenceError,
    TransientProviderFailureError,
)
from enginery.domain.g4_authority_evidence import G4AuthorityEvidence
from enginery.domain.g4_deficiency import G4DeficiencyFinding
from enginery.domain.ids import OperationId, WorkItemId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.ledger.artifact_store import ArtifactStore

_GITHUB_API_VERSION = "2026-03-10"
_PAGE_SIZE = 100
_MAX_PAGES = 100
_MAX_EVIDENCE_BYTES = 1024 * 1024
_LIFECYCLE_MARKER_PREFIX = "<!-- enginery:lifecycle:"
_PULL_REQUEST_MARKER_PREFIX = "<!-- enginery:pull-request:"
_MERGE_METHODS = frozenset({"merge", "squash", "rebase"})


class GitHubCliUnavailableError(MissingPrerequisiteError, OSError):
    """The configured ``gh`` executable could not be run at all.

    It is classified, so a command that needs GitHub exits with the missing-prerequisite
    code and a message naming the executable instead of a traceback. It is also an
    ``OSError``, which is how the adapters' ``probe`` methods already report an unavailable
    CLI, so they keep doing so.
    """


@dataclass(frozen=True, slots=True)
class GitHubAdapterConfig:
    """Opaque GitHub CLI configuration for one repository."""

    repository: str
    credential_reference: str
    executable: str = "gh"
    api_version: str = _GITHUB_API_VERSION
    smoke_repository_allowlist: frozenset[str] = frozenset({"Mathews-Tom/enginery-provider-smoke"})

    def __post_init__(self) -> None:
        if not _is_repository_name(self.repository):
            raise InvalidInputError("GitHub repository must use owner/name form")
        if not self.credential_reference.strip():
            raise InvalidInputError("GitHub credential reference must be non-blank")
        if not self.executable.strip():
            raise InvalidInputError("GitHub CLI executable must be non-blank")
        if not self.api_version.strip():
            raise InvalidInputError("GitHub API version must be non-blank")
        if any(
            not _is_repository_name(repository) for repository in self.smoke_repository_allowlist
        ):
            raise InvalidInputError("GitHub smoke repository allowlist contains an invalid name")

    def require_smoke_repository(self) -> None:
        """Refuse a live fixture mutation outside the reviewed repository allowlist."""
        if self.repository not in self.smoke_repository_allowlist:
            raise InvalidInputError("GitHub repository is not allowlisted for provider smoke tests")


@dataclass(slots=True)
class GitHubWorkLedger:
    """Project GitHub issues into the normalized work-ledger contract."""

    config: GitHubAdapterConfig
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] = field(
        default=lambda arguments: subprocess.run(
            arguments, check=False, capture_output=True, text=True
        )
    )
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)
    github_reads: int = field(default=0, init=False)

    def probe(self) -> AdapterStatus:
        try:
            version_lines = self._run((self.config.executable, "--version")).stdout.splitlines()
            if not version_lines or not version_lines[0].strip():
                raise TransientProviderFailureError("GitHub CLI did not report a version")
            version = version_lines[0]
            self._request("GET", "user")
        except OSError:
            return AdapterStatus(
                kind=ProviderKind.WORK_LEDGER,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="GitHub CLI is unavailable",
            )
        except AuthenticationFailureError:
            return AdapterStatus(
                kind=ProviderKind.WORK_LEDGER,
                availability=AdapterAvailability.MISCONFIGURED,
                fingerprint=None,
                detail="GitHub CLI authentication is unavailable",
            )
        except (RateLimitError, TransientProviderFailureError, ExternalConflictError):
            return AdapterStatus(
                kind=ProviderKind.WORK_LEDGER,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="GitHub API probe did not complete",
            )
        return AdapterStatus(
            kind=ProviderKind.WORK_LEDGER,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="github-work-ledger",
                provider_version=f"{version};api={self.config.api_version}",
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability("issue_snapshots", 1),
                    AdapterCapability("lifecycle_projection", 1),
                    AdapterCapability("pagination", 1),
                ),
            ),
            detail="GitHub issue work ledger is available",
        )

    def list_candidates(self, *, since: str | None) -> tuple[IssueCandidate, ...]:
        """List open issue metadata, retaining labels from the paginated listing."""
        candidates: list[IssueCandidate] = []
        page = 1
        while page <= _MAX_PAGES:
            endpoint = (
                f"repos/{self.config.repository}/issues"
                f"?state=open&per_page={_PAGE_SIZE}&page={page}"
            )
            if since is not None:
                endpoint += f"&since={quote(since, safe='')}"
            records = self._request_array("GET", endpoint)
            for record in records:
                if not isinstance(record, Mapping):
                    raise TransientProviderFailureError("GitHub issue record must be an object")
                if "pull_request" in record:
                    continue
                labels = record.get("labels")
                if not isinstance(labels, list):
                    raise TransientProviderFailureError("GitHub issue labels must be a JSON array")
                candidates.append(
                    IssueCandidate(
                        external_reference=(
                            f"{self.config.repository}#{_required_positive_int(record, 'number')}"
                        ),
                        title=_required_string(record, "title"),
                        labels=label_names(labels),
                        updated_at=_required_string(record, "updated_at"),
                    )
                )
            if len(records) < _PAGE_SIZE:
                return tuple(candidates)
            page += 1
        raise TransientProviderFailureError("GitHub candidate listing exceeded the 100-page limit")

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        issue_number = self._issue_number(external_reference)
        endpoint = f"repos/{self.config.repository}/issues/{issue_number}"
        payload = self._request_object("GET", endpoint)
        if "pull_request" in payload:
            raise InvalidInputError("GitHub pull requests cannot be ingested as issues")
        labels = self._labels(endpoint)
        declared_classification(label_names(labels), source_url=_required_string(payload, "url"))
        confirmed_payload = self._request_object("GET", endpoint)
        if payload != confirmed_payload:
            raise StaleEvidenceError(
                "GitHub issue changed while declared classification was collected"
            )
        confirmed_labels = self._labels(endpoint)
        if label_names(labels) != label_names(confirmed_labels):
            raise StaleEvidenceError(
                "GitHub declared classification changed while it was collected"
            )
        return self._snapshot(payload, labels, issue_number)

    def publish_lifecycle(
        self, projection: LifecycleProjection, *, operation_id: OperationId
    ) -> ReconciliationResult:
        existing = self._find_lifecycle_projection(operation_id)
        if existing is not ReconciliationResult.NOT_FOUND:
            return existing
        issue_number = self._issue_number(projection.external_reference)
        marker = _lifecycle_marker(operation_id)
        body = "\n".join(
            (
                marker,
                f"Enginery lifecycle: run {projection.run_id}; state {projection.state}.",
                f"Evidence digest: {projection.evidence_digest}"
                if projection.evidence_digest
                else "",
            )
        ).strip()
        self._request(
            "POST",
            f"repos/{self.config.repository}/issues/{issue_number}/comments",
            "--raw-field",
            f"body={body}",
        )
        self._outcomes[str(operation_id)] = ReconciliationResult.FOUND_MATCHING
        return ReconciliationResult.FOUND_MATCHING

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        discovered = self._find_lifecycle_projection(operation_id)
        if discovered is not ReconciliationResult.NOT_FOUND:
            self._outcomes[str(operation_id)] = discovered
            return discovered
        return self._outcomes.get(str(operation_id), ReconciliationResult.NOT_FOUND)

    def _snapshot(
        self, payload: Mapping[str, object], labels: Sequence[object], issue_number: int
    ) -> WorkLedgerSnapshot:
        title = _required_string(payload, "title")
        updated_at = _required_string(payload, "updated_at")
        body_value = payload.get("body")
        if body_value is None:
            body = title
        elif isinstance(body_value, str) and body_value.strip():
            body = body_value
        else:
            raise InvalidInputError("GitHub issue body must be a non-blank string or null")
        source_snapshot_reference = _required_string(payload, "url")
        work_kind, risk_class, classification = declared_classification(
            label_names(labels), source_url=source_snapshot_reference
        )
        work_item = WorkItem(
            id=WorkItemId(f"github:{self.config.repository}#{issue_number}"),
            work_kind=work_kind,
            source_provider="github-issues",
            external_reference=f"{self.config.repository}#{issue_number}",
            source_snapshot_reference=source_snapshot_reference,
            title=title,
            objective=body,
            acceptance_criteria=(body,),
            constraints=(),
            risk_class=risk_class,
            repository_targets=(self.config.repository,),
            dependencies=(),
            state=WorkItemState.NEW,
        )
        source_digest = Digest.of_json(
            {
                "work_item_digest": str(work_item.bound_field_digest),
                "classification_digest": str(classification.digest),
            }
        )
        return WorkLedgerSnapshot(
            work_item=work_item,
            source_revision=f"{updated_at}:{source_digest}",
            classification_provenance=classification,
        )

    def _find_lifecycle_projection(self, operation_id: OperationId) -> ReconciliationResult:
        marker = _lifecycle_marker(operation_id)
        matches = 0
        page = 1
        while True:
            records = self._request_array(
                "GET",
                f"repos/{self.config.repository}/issues/comments?per_page={_PAGE_SIZE}&page={page}",
            )
            for record in records:
                if isinstance(record, Mapping) and marker in _optional_string(record, "body"):
                    matches += 1
            if len(records) < _PAGE_SIZE:
                break
            page += 1
        if matches == 0:
            return ReconciliationResult.NOT_FOUND
        if matches == 1:
            return ReconciliationResult.FOUND_MATCHING
        return ReconciliationResult.FOUND_CONFLICTING

    def verify_g4_evidence(
        self,
        *,
        finding: G4DeficiencyFinding,
        principal_github_user_ids: Mapping[str, int],
        verified_at: datetime,
    ) -> G4AuthorityEvidence:
        """Read a merged evidence PR and all reviews before verifying authority."""
        pull_request = self._request_object(
            "GET",
            f"repos/{self.config.repository}/pulls/{finding.evidence_pull_request_number}",
        )
        reviews: list[object] = []
        page = 1
        while True:
            records = self._request_array(
                "GET",
                f"repos/{self.config.repository}/pulls/{finding.evidence_pull_request_number}"
                f"/reviews?per_page={_PAGE_SIZE}&page={page}",
            )
            reviews.extend(records)
            if len(records) < _PAGE_SIZE:
                break
            page += 1
        return verify_g4_evidence_pull_request(
            finding=finding,
            principal_github_user_ids=principal_github_user_ids,
            pull_request=pull_request,
            reviews=reviews,
            verified_at=verified_at,
        )

    def _issue_number(self, external_reference: str) -> int:
        prefix = f"{self.config.repository}#"
        if not external_reference.startswith(prefix):
            raise InvalidInputError(
                "GitHub issue reference does not belong to the configured repository"
            )
        number = external_reference.removeprefix(prefix)
        if not re.fullmatch(r"[1-9][0-9]*", number):
            raise InvalidInputError("GitHub issue reference must end with a positive issue number")
        return int(number)

    def _labels(self, issue_endpoint: str) -> list[object]:
        labels: list[object] = []
        page = 1
        while True:
            records = self._request_array(
                "GET", f"{issue_endpoint}/labels?per_page={_PAGE_SIZE}&page={page}"
            )
            labels.extend(records)
            if len(records) < _PAGE_SIZE:
                return labels
            page += 1

    def _request_object(self, method: str, endpoint: str, *fields: str) -> Mapping[str, object]:
        payload = self._request(method, endpoint, *fields)
        if not isinstance(payload, Mapping):
            raise TransientProviderFailureError("GitHub API response must be a JSON object")
        return payload

    def _request_array(self, method: str, endpoint: str, *fields: str) -> Sequence[object]:
        payload = self._request(method, endpoint, *fields)
        if not isinstance(payload, list):
            raise TransientProviderFailureError("GitHub API response must be a JSON array")
        return cast(Sequence[object], payload)

    def _request(self, method: str, endpoint: str, *fields: str) -> object:
        if method == "GET":
            self.github_reads += 1
        return _request(self.config, self.command_runner, method, endpoint, *fields)

    def _run(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return _run(self.command_runner, arguments)


def _bounded_github_command(arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """Use the shared bounded reader without exposing tool diagnostics."""
    try:
        result = execute_result(arguments, dict(os.environ), timeout=60)
    except SubprocessOutputError as error:
        if isinstance(error.__cause__, OSError):
            raise error.__cause__ from error
        raise TransientProviderFailureError(f"GitHub evidence command failed: {error}") from error
    return subprocess.CompletedProcess(arguments, result.returncode, result.stdout, result.stderr)


@dataclass(slots=True)
class GitHubPullRequests:
    """Create and reconcile GitHub pull requests through deterministic markers."""

    config: GitHubAdapterConfig
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] = (
        _bounded_github_command
    )
    artifact_store: ArtifactStore | None = None
    evidence_page_cap: int = _MAX_PAGES
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        work_ledger_status = GitHubWorkLedger(
            self.config, command_runner=self.command_runner
        ).probe()
        if work_ledger_status.availability is not AdapterAvailability.AVAILABLE:
            return AdapterStatus(
                kind=ProviderKind.SOURCE_CONTROL,
                availability=work_ledger_status.availability,
                fingerprint=None,
                detail=work_ledger_status.detail,
            )
        assert work_ledger_status.fingerprint is not None
        return AdapterStatus(
            kind=ProviderKind.SOURCE_CONTROL,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="github-pull-requests",
                provider_version=work_ledger_status.fingerprint.provider_version,
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability("pull_request_create_or_update", 1),
                    AdapterCapability("pull_request_reconciliation", 1),
                    AdapterCapability("pull_request_head_metadata", 1),
                    AdapterCapability("pull_request_merge", 1),
                ),
            ),
            detail="GitHub pull-request adapter is available",
        )

    def create_or_update(self, request: PullRequestRequest) -> PullRequestSnapshot:
        candidates = self._matching_pull_requests(
            request.operation_id, request.head_branch, request.base_branch
        )
        if len(candidates) > 1:
            raise AmbiguousExternalSideEffectError(
                "multiple GitHub pull requests match the operation marker"
            )
        marker = _pull_request_marker(request.operation_id)
        body = f"{request.body}\n\n{marker}"
        if candidates:
            number = _required_positive_int(candidates[0], "number")
            payload = self._request_object(
                "PATCH",
                f"repos/{self.config.repository}/pulls/{number}",
                "--raw-field",
                f"title={request.title}",
                "--raw-field",
                f"body={body}",
                "--raw-field",
                f"base={request.base_branch}",
            )
        else:
            payload = self._request_object(
                "POST",
                f"repos/{self.config.repository}/pulls",
                "--raw-field",
                f"title={request.title}",
                "--raw-field",
                f"head={request.head_branch}",
                "--raw-field",
                f"base={request.base_branch}",
                "--raw-field",
                f"body={body}",
            )
        snapshot = _pull_request_snapshot(payload)
        if (
            snapshot.head_branch != request.head_branch
            or snapshot.base_branch != request.base_branch
        ):
            raise ExternalConflictError("GitHub pull request head or base differs from the request")
        self._outcomes[str(request.operation_id)] = ReconciliationResult.FOUND_MATCHING
        return snapshot

    def get(self, number: int) -> PullRequestSnapshot:
        if number < 1:
            raise InvalidInputError("GitHub pull request number must be positive")
        return _pull_request_snapshot(
            self._request_object("GET", f"repos/{self.config.repository}/pulls/{number}")
        )

    def evidence(self, number: int) -> PullRequestEvidence:
        """Retain fully paginated raw API observations; never authorize a prefix."""
        if number < 1:
            raise InvalidInputError("GitHub pull request number must be positive")
        if self.artifact_store is None:
            raise MissingPrerequisiteError("GitHub evidence requires the project artifact store")
        if type(self.evidence_page_cap) is not int or not 2 <= self.evidence_page_cap <= _MAX_PAGES:
            raise InvalidInputError("GitHub evidence page cap must be between 2 and 100")
        try:
            return self._collect_evidence(number)
        except InvalidInputError as error:
            raise TransientProviderFailureError(f"GitHub evidence is malformed: {error}") from error

    def _collect_evidence(self, number: int) -> PullRequestEvidence:
        raw: list[Digest] = []
        proofs: list[EvidenceCompleteness] = []
        endpoint = f"repos/{self.config.repository}/pulls/{number}"
        payload = self._evidence_object(endpoint, raw)
        snapshot = _pull_request_snapshot(payload)
        if any(
            re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None
            for revision in (snapshot.head_revision, snapshot.base_revision)
        ):
            raise TransientProviderFailureError(
                "GitHub evidence revisions must be exact commit SHAs"
            )
        if snapshot.number != number:
            raise StaleEvidenceError("GitHub returned a different pull request")
        mergeable = payload.get("mergeable")
        if mergeable is not None and not isinstance(mergeable, bool):
            raise TransientProviderFailureError("GitHub mergeability must be boolean or null")
        reviews_raw = self._evidence_pages(f"{endpoint}/reviews", "reviews", raw, proofs)
        reviews = tuple(_evidence_review(review) for review in reviews_raw)
        commit = f"repos/{self.config.repository}/commits/{snapshot.head_revision}"
        runs = self._evidence_pages(
            f"{commit}/check-runs", "check_runs", raw, proofs, array_field="check_runs"
        )
        checks = tuple(_evidence_check(run, snapshot.head_revision) for run in runs)
        combined_states: list[str] = []
        combined = self._evidence_pages(
            f"{commit}/status",
            "combined_status",
            raw,
            proofs,
            array_field="statuses",
            expected_head=snapshot.head_revision,
            combined_states=combined_states,
        )
        history = self._evidence_pages(f"{commit}/statuses", "statuses", raw, proofs)
        current_statuses = _latest_statuses(history, snapshot.head_revision)
        combined_statuses = _latest_statuses(combined, snapshot.head_revision)
        # The combined-status endpoint omits each status's creator, which only the statuses
        # list carries; compare everything both endpoints report and keep the list's creator.
        if [replace(status, creator_id=None) for status in current_statuses] != [
            replace(status, creator_id=None) for status in combined_statuses
        ]:
            raise StaleEvidenceError("GitHub combined and per-context statuses disagree")
        latest_payload = self._evidence_object(endpoint, raw)
        latest = _pull_request_snapshot(latest_payload)
        latest_mergeable = latest_payload.get("mergeable")
        if latest_mergeable is not None and not isinstance(latest_mergeable, bool):
            raise TransientProviderFailureError("GitHub mergeability must be boolean or null")
        if latest != snapshot or (mergeable is not None and latest_mergeable != mergeable):
            raise StaleEvidenceError("GitHub pull request changed while evidence was collected")
        if mergeable is None:
            mergeable = latest_mergeable
        return PullRequestEvidence(
            snapshot,
            reviews,
            checks,
            mergeable,
            current_statuses,
            combined_states[0],
            tuple(proofs),
            tuple(raw),
        )

    def _evidence_object(self, endpoint: str, raw: list[Digest]) -> Mapping[str, object]:
        payload = self._evidence_request(endpoint, raw)
        if not isinstance(payload, Mapping):
            raise TransientProviderFailureError("GitHub evidence must be a JSON object")
        return payload

    def _evidence_request(self, endpoint: str, raw: list[Digest]) -> object:
        assert self.artifact_store is not None

        def retain(body: bytes) -> None:
            assert self.artifact_store is not None
            raw.append(self.artifact_store.publish_bytes(body, media_type="application/json"))

        return _request(self.config, self.command_runner, "GET", endpoint, raw_sink=retain)

    def _evidence_pages(
        self,
        endpoint: str,
        collection: str,
        raw: list[Digest],
        proofs: list[EvidenceCompleteness],
        *,
        array_field: str | None = None,
        expected_head: str | None = None,
        combined_states: list[str] | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        records: list[Mapping[str, object]] = []
        digests: list[Digest] = []
        total: int | None = None
        identities: set[int] = set()
        for page in range(1, self.evidence_page_cap + 1):
            payload = self._evidence_request(f"{endpoint}?per_page={_PAGE_SIZE}&page={page}", raw)
            digests.append(raw[-1])
            if array_field is not None:
                if not isinstance(payload, Mapping):
                    raise TransientProviderFailureError("GitHub collection must be an object")
                count = payload.get("total_count")
                if type(count) is not int or count < 0:
                    raise TransientProviderFailureError("GitHub collection lacks total_count")
                if total is not None and count != total:
                    raise StaleEvidenceError("GitHub collection total_count changed across pages")
                total = count
                if expected_head is not None and payload.get("sha") != expected_head:
                    raise StaleEvidenceError("GitHub combined status has a stale head")
                if combined_states is not None:
                    state = _required_string(payload, "state").lower()
                    if state not in {"success", "pending", "failure", "error"}:
                        raise TransientProviderFailureError("GitHub combined status is malformed")
                    if combined_states and combined_states[0] != state:
                        raise StaleEvidenceError("GitHub combined state changed across pages")
                    combined_states.append(state)
                items = payload.get(array_field)
            else:
                items = payload
            if not isinstance(items, list) or len(items) > _PAGE_SIZE:
                raise TransientProviderFailureError(
                    "GitHub collection page must be a bounded array"
                )
            for item in items:
                if not isinstance(item, Mapping):
                    raise TransientProviderFailureError(
                        "GitHub collection element must be an object"
                    )
                identity = _required_positive_int(item, "id")
                if identity in identities:
                    raise StaleEvidenceError("GitHub pagination repeated an element")
                identities.add(identity)
                records.append(item)
            if total is not None and (
                len(records) > total or (len(items) < _PAGE_SIZE and len(records) != total)
            ):
                raise TransientProviderFailureError("GitHub short page disagrees with total_count")
            # Even an apparently terminal page at the cap is not a completeness proof.
            if page == self.evidence_page_cap:
                raise TransientProviderFailureError("GitHub evidence pagination cap reached")
            if len(items) < _PAGE_SIZE:
                proofs.append(
                    EvidenceCompleteness(
                        collection,
                        page,
                        self.evidence_page_cap,
                        len(records),
                        total,
                        True,
                        tuple(digests),
                    )
                )
                return tuple(records)
        raise TransientProviderFailureError("GitHub evidence pagination did not terminate")

    def merge(
        self,
        number: int,
        *,
        expected_head_revision: str,
        operation_id: OperationId,
        merge_method: str = "merge",
    ) -> PullRequestSnapshot:
        """Merge one pull request, bound atomically to its exact expected head.

        Idempotent by construction rather than by the marker/``reconcile``
        pattern the other mutations use: a retry after a lost response
        first re-reads current state (read one), then relies on GitHub's
        own ``sha`` precondition on the merge call itself to bind the
        mutation atomically to ``expected_head_revision`` (read two,
        enforced provider-side -- a later external head change is
        rejected with a conflict rather than silently merging the wrong
        commit). If a retry observes the PR already merged at the exact
        expected head, it returns that snapshot unchanged rather than
        attempting a second mutation; merged at any other head is a
        conflict, never silently adopted.
        """
        if number < 1:
            raise InvalidInputError("GitHub pull request number must be positive")
        if not expected_head_revision.strip():
            raise InvalidInputError("expected_head_revision must be non-blank")
        if merge_method not in _MERGE_METHODS:
            raise InvalidInputError("merge_method must be one of merge, squash, rebase")
        current = self.get(number)
        if current.merged:
            if current.head_revision != expected_head_revision:
                raise ExternalConflictError(
                    "GitHub pull request was already merged at a different head revision"
                )
            self._outcomes[str(operation_id)] = ReconciliationResult.FOUND_MATCHING
            return current
        if current.head_revision != expected_head_revision:
            raise StaleEvidenceError(
                "GitHub pull request head changed since the caller's expected revision"
            )
        payload = self._request_object(
            "PUT",
            f"repos/{self.config.repository}/pulls/{number}/merge",
            "--raw-field",
            f"sha={expected_head_revision}",
            "--raw-field",
            f"merge_method={merge_method}",
        )
        merged_flag = payload.get("merged")
        if not isinstance(merged_flag, bool):
            raise TransientProviderFailureError(
                "GitHub merge response must report 'merged' as boolean"
            )
        if not merged_flag:
            raise ExternalConflictError("GitHub reported the merge did not succeed")
        merged_snapshot = self.get(number)
        if not merged_snapshot.merged or merged_snapshot.head_revision != expected_head_revision:
            raise ExternalConflictError(
                "GitHub pull request state after merge does not match the expected head revision"
            )
        self._outcomes[str(operation_id)] = ReconciliationResult.FOUND_MATCHING
        return merged_snapshot

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        candidates = self._matching_pull_requests(operation_id, None, None)
        if len(candidates) == 1:
            result = ReconciliationResult.FOUND_MATCHING
        elif len(candidates) > 1:
            result = ReconciliationResult.FOUND_CONFLICTING
        else:
            result = self._outcomes.get(str(operation_id), ReconciliationResult.NOT_FOUND)
        self._outcomes[str(operation_id)] = result
        return result

    def _matching_pull_requests(
        self, operation_id: OperationId, head_branch: str | None, base_branch: str | None
    ) -> tuple[Mapping[str, object], ...]:
        marker = _pull_request_marker(operation_id)
        query = ["state=all", f"per_page={_PAGE_SIZE}"]
        if head_branch is not None:
            owner = self.config.repository.partition("/")[0]
            query.append(f"head={quote(f'{owner}:{head_branch}', safe='')}")
        if base_branch is not None:
            query.append(f"base={quote(base_branch, safe='')}")
        page = 1
        matches: list[Mapping[str, object]] = []
        while True:
            payload = self._request_array(
                "GET",
                f"repos/{self.config.repository}/pulls?{'&'.join(query)}&page={page}",
            )
            for record in payload:
                if isinstance(record, Mapping) and marker in _optional_string(record, "body"):
                    matches.append(record)
            if len(payload) < _PAGE_SIZE:
                break
            page += 1
        return tuple(matches)

    def _request_object(self, method: str, endpoint: str, *fields: str) -> Mapping[str, object]:
        payload = _request(self.config, self.command_runner, method, endpoint, *fields)
        if not isinstance(payload, Mapping):
            raise TransientProviderFailureError("GitHub API response must be a JSON object")
        return payload

    def _request_array(self, method: str, endpoint: str, *fields: str) -> Sequence[object]:
        payload = _request(self.config, self.command_runner, method, endpoint, *fields)
        if not isinstance(payload, list):
            raise TransientProviderFailureError("GitHub API response must be a JSON array")
        return cast(Sequence[object], payload)


@dataclass(frozen=True, slots=True)
class GitHubReleaseRequest:
    """Release-specific fields the generic ``ReleasePort`` contract does not carry.

    Staged against an artifact digest before ``publish()`` runs, since
    ``PublicationRequest`` (shared across every ``ReleasePort``
    implementation) has no notion of a tag, target commit, or release
    notes body.
    """

    tag_name: str
    target_commitish: str
    name: str
    body: str
    prerelease: bool = False

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (self.tag_name, self.target_commitish, self.name)):
            raise InvalidInputError(
                "GitHub release tag_name, target_commitish, and name must be non-blank"
            )
        if not self.body.strip():
            raise InvalidInputError("GitHub release body must be non-blank")


@dataclass(slots=True)
class GitHubReleaseAdapter:
    """Create and verify one tagged GitHub Release, bound atomically to its exact commit.

    Does not upload the published artifact as a binary release asset:
    PyPI is the installable-artifact destination for a Python package,
    and this adapter's release body embeds the artifact's digest as
    verifiable evidence text instead, keeping exactly one canonical
    artifact host per publication. Idempotent by construction, mirroring
    ``GitHubPullRequests.merge()``: a retry after a lost response first
    checks for an existing release at the expected tag before attempting
    to create one, and only ever adopts it when the target commit
    matches; a tag reused against a different commit is a conflict,
    never silently adopted.
    """

    config: GitHubAdapterConfig
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] = field(
        default=lambda arguments: subprocess.run(
            arguments, check=False, capture_output=True, text=True
        )
    )
    _staged: dict[str, GitHubReleaseRequest] = field(default_factory=dict, init=False)
    _operation_tags: dict[str, str] = field(default_factory=dict, init=False)
    _outcomes: dict[str, ReconciliationResult] = field(default_factory=dict, init=False)

    def probe(self) -> AdapterStatus:
        try:
            result = _run(self.command_runner, (self.config.executable, "--version"))
        except OSError:
            return AdapterStatus(
                kind=ProviderKind.RELEASE,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="GitHub CLI is not available",
            )
        if result.returncode != 0:
            return AdapterStatus(
                kind=ProviderKind.RELEASE,
                availability=AdapterAvailability.UNAVAILABLE,
                fingerprint=None,
                detail="GitHub CLI is not available",
            )
        return AdapterStatus(
            kind=ProviderKind.RELEASE,
            availability=AdapterAvailability.AVAILABLE,
            fingerprint=AdapterFingerprint(
                provider_id="github-release",
                provider_version=result.stdout.strip() or "unknown",
                api_version=ADAPTER_API_VERSION,
                capabilities=(AdapterCapability("release_create_and_verify", 1),),
            ),
            detail="GitHub release adapter is available",
        )

    def stage(self, digest: Digest, request: GitHubReleaseRequest) -> None:
        """Record the release fields to publish for a given artifact digest."""
        self._staged[str(digest)] = request

    def publish(self, request: PublicationRequest) -> PublicationReceipt:
        staged = self._staged.get(str(request.artifact.digest))
        if staged is None:
            raise InvalidInputError(
                "no staged release request matches this artifact's digest; call stage() first"
            )
        self._operation_tags[str(request.operation_id)] = staged.tag_name
        existing = self._find_by_tag(staged.tag_name)
        if existing is not None:
            existing_commitish = _required_string(existing, "target_commitish")
            if existing_commitish != staged.target_commitish:
                raise ExternalConflictError(
                    "a GitHub release already exists for this tag at a different commit",
                    details={"tag_name": staged.tag_name, "existing": existing_commitish},
                )
            self._outcomes[str(request.operation_id)] = ReconciliationResult.FOUND_MATCHING
            return PublicationReceipt(
                destination=request.destination,
                version=request.artifact.version,
                artifact_digest=request.artifact.digest,
            )
        digest_evidence = f"\n\n<!-- enginery:artifact-digest:{request.artifact.digest} -->"
        payload = self._request_object(
            "POST",
            f"repos/{self.config.repository}/releases",
            "--raw-field",
            f"tag_name={staged.tag_name}",
            "--raw-field",
            f"target_commitish={staged.target_commitish}",
            "--raw-field",
            f"name={staged.name}",
            "--raw-field",
            f"body={staged.body}{digest_evidence}",
            "--field",
            f"prerelease={'true' if staged.prerelease else 'false'}",
        )
        created_commitish = _required_string(payload, "target_commitish")
        if created_commitish != staged.target_commitish:
            raise ExternalConflictError(
                "GitHub created the release against an unexpected commit",
                details={"expected": staged.target_commitish, "observed": created_commitish},
            )
        self._outcomes[str(request.operation_id)] = ReconciliationResult.FOUND_MATCHING
        return PublicationReceipt(
            destination=request.destination,
            version=request.artifact.version,
            artifact_digest=request.artifact.digest,
        )

    def verify(self, receipt: PublicationReceipt) -> PublicationReceipt:
        for digest_str, candidate in self._staged.items():
            if digest_str != str(receipt.artifact_digest):
                continue
            release = self._find_by_tag(candidate.tag_name)
            if release is None:
                raise ExternalConflictError(
                    "GitHub does not report a release for the published tag",
                    details={"tag_name": candidate.tag_name},
                )
            observed_commitish = _required_string(release, "target_commitish")
            if observed_commitish != candidate.target_commitish:
                raise ExternalConflictError(
                    "GitHub release target commit does not match the expected commit",
                    details={
                        "expected": candidate.target_commitish,
                        "observed": observed_commitish,
                    },
                )
            digest_evidence = f"<!-- enginery:artifact-digest:{receipt.artifact_digest} -->"
            if digest_evidence not in _optional_string(release, "body"):
                raise ExternalConflictError(
                    "GitHub release body does not carry the expected artifact-digest evidence"
                )
            return receipt
        raise InvalidInputError(
            "no staged release request matches this receipt's artifact digest; call stage() first"
        )

    def reconcile(self, *, operation_id: OperationId) -> ReconciliationResult:
        tag_name = self._operation_tags.get(str(operation_id))
        if tag_name is None:
            result = self._outcomes.get(str(operation_id), ReconciliationResult.NOT_FOUND)
            self._outcomes[str(operation_id)] = result
            return result
        existing = self._find_by_tag(tag_name)
        result = (
            ReconciliationResult.FOUND_MATCHING
            if existing is not None
            else ReconciliationResult.NOT_FOUND
        )
        self._outcomes[str(operation_id)] = result
        return result

    def _find_by_tag(self, tag_name: str) -> Mapping[str, object] | None:
        result = _run(
            self.command_runner,
            (
                self.config.executable,
                "api",
                "--method",
                "GET",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                f"X-GitHub-Api-Version: {self.config.api_version}",
                f"repos/{self.config.repository}/releases/tags/{quote(tag_name, safe='')}",
            ),
        )
        if result.returncode != 0:
            if "http 404" in result.stderr.lower():
                return None
            _raise_github_failure(result.stderr)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise TransientProviderFailureError("GitHub API returned invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise TransientProviderFailureError("GitHub API response must be a JSON object")
        return payload

    def _request_object(self, method: str, endpoint: str, *fields: str) -> Mapping[str, object]:
        payload = _request(self.config, self.command_runner, method, endpoint, *fields)
        if not isinstance(payload, Mapping):
            raise TransientProviderFailureError("GitHub API response must be a JSON object")
        return payload


def _request(
    config: GitHubAdapterConfig,
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]],
    method: str,
    endpoint: str,
    *fields: str,
    raw_sink: Callable[[bytes], None] | None = None,
) -> object:
    result = _run(
        command_runner,
        (
            config.executable,
            "api",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {config.api_version}",
            endpoint,
            *fields,
        ),
    )
    if result.returncode != 0:
        _raise_github_failure(result.stderr)
    if raw_sink is not None:
        body = result.stdout.encode("utf-8")
        if len(body) + len(result.stderr.encode("utf-8")) > _MAX_EVIDENCE_BYTES:
            raise TransientProviderFailureError("GitHub evidence response exceeds 1 MiB")
        raw_sink(body)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise TransientProviderFailureError("GitHub API returned invalid JSON") from error


def _run(
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]],
    arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    try:
        result = command_runner(arguments)
    except OSError as error:
        raise GitHubCliUnavailableError(
            f"GitHub CLI {arguments[0]!r} could not be run: {error.strerror or error}",
            details={"executable": arguments[0]},
        ) from error
    if result.returncode != 0 and arguments[1:] == ("--version",):
        _raise_github_failure(result.stderr)
    return result


def _is_repository_name(value: str) -> bool:
    owner, separator, repository = value.partition("/")
    return bool(separator and owner and repository and "/" not in repository)


def _required_object(payload: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, Mapping):
        raise TransientProviderFailureError(
            f"GitHub response field {field_name!r} must be a JSON object"
        )
    return value


def _required_string(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(f"GitHub response field {field_name!r} must be a non-blank string")
    return value


def _optional_string(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    return value if isinstance(value, str) else ""


def _required_positive_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if type(value) is not int or value < 1:
        raise TransientProviderFailureError(
            f"GitHub response field {field_name!r} must be a positive integer"
        )
    return value


def _pull_request_snapshot(payload: Mapping[str, object]) -> PullRequestSnapshot:
    head = payload.get("head")
    base = payload.get("base")
    if not isinstance(head, Mapping) or not isinstance(base, Mapping):
        raise TransientProviderFailureError("GitHub pull request lacks head or base metadata")
    merged_raw = payload.get("merged")
    if merged_raw is not None and not isinstance(merged_raw, bool):
        raise TransientProviderFailureError("GitHub pull request 'merged' must be boolean")
    merged_at = payload.get("merged_at")
    if merged_at is not None and not isinstance(merged_at, str):
        raise TransientProviderFailureError("GitHub pull request 'merged_at' must be a string")
    # The list endpoint's "Pull Request Simple" schema omits `merged` and only
    # carries `merged_at`; the single-PR and merge-response schemas carry
    # `merged` directly. Treating either signal as authoritative keeps
    # `PullRequestSnapshot.merged` correct regardless of which endpoint
    # produced this payload.
    merged = bool(merged_raw) or merged_at is not None
    return PullRequestSnapshot(
        number=_required_positive_int(payload, "number"),
        url=_required_string(payload, "html_url"),
        state=_required_string(payload, "state"),
        head_branch=_required_string(head, "ref"),
        head_revision=_required_string(head, "sha"),
        base_branch=_required_string(base, "ref"),
        base_revision=_required_string(base, "sha"),
        merged=merged,
    )


def _evidence_timestamp(payload: Mapping[str, object], field_name: str) -> str:
    value = _required_string(payload, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TransientProviderFailureError("GitHub evidence timestamp is malformed") from error
    if parsed.tzinfo is None:
        raise TransientProviderFailureError("GitHub evidence timestamp lacks a timezone")
    return value


def _evidence_review(payload: Mapping[str, object]) -> PullRequestReview:
    state = _required_string(payload, "state").upper()
    if state not in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"}:
        raise TransientProviderFailureError("GitHub review state is malformed")
    # Pending reviews are not submitted and cannot authorize or supersede a decision.
    submitted = payload.get("submitted_at")
    if state == "PENDING" and submitted is None:
        timestamp = None
    else:
        timestamp = _evidence_timestamp(payload, "submitted_at")
    revision = _required_string(payload, "commit_id")
    if re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None:
        raise TransientProviderFailureError("GitHub review commit_id must be an exact commit SHA")
    return PullRequestReview(
        _required_string(_required_object(payload, "user"), "login"),
        state,
        revision,
        _required_positive_int(payload, "id"),
        timestamp,
    )


def _evidence_check(payload: Mapping[str, object], head: str) -> PullRequestCheck:
    if _required_string(payload, "head_sha") != head:
        raise StaleEvidenceError("GitHub check run is bound to a stale head revision")
    status = _required_string(payload, "status")
    if status not in {"queued", "in_progress", "completed", "waiting", "requested", "pending"}:
        raise TransientProviderFailureError("GitHub check-run status is malformed")
    conclusion = payload.get("conclusion")
    if not isinstance(conclusion, str) and conclusion is not None:
        raise TransientProviderFailureError("GitHub check-run conclusion must be a string or null")
    if conclusion is not None and conclusion not in {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }:
        raise TransientProviderFailureError("GitHub check-run conclusion is malformed")
    return PullRequestCheck(
        _required_string(payload, "name"),
        status,
        conclusion,
        head,
        _evidence_actor_id(payload, "app"),
    )


def _latest_statuses(
    payloads: tuple[Mapping[str, object], ...],
    head: str,
) -> tuple[PullRequestStatus, ...]:
    latest: dict[str, PullRequestStatus] = {}
    for payload in payloads:
        state = _required_string(payload, "state").lower()
        if state not in {"success", "pending", "failure", "error"}:
            raise TransientProviderFailureError("GitHub commit status state is malformed")
        # These endpoints are addressed by immutable commit SHA; status objects do not
        # carry a SHA in GitHub's schema. Reject it if a provider supplies a conflicting one.
        if "sha" in payload and payload["sha"] != head:
            raise StaleEvidenceError("GitHub commit status is bound to a stale head revision")
        status = PullRequestStatus(
            _required_string(payload, "context"),
            state,
            head,
            _required_positive_int(payload, "id"),
            _evidence_timestamp(payload, "created_at"),
            _evidence_actor_id(payload, "creator"),
        )
        previous = latest.get(status.context)
        if previous is None or status.status_id > previous.status_id:
            latest[status.context] = status
    return tuple(latest[context] for context in sorted(latest))


def _evidence_actor_id(payload: Mapping[str, object], field: str) -> int | None:
    actor = payload.get(field)
    if actor is None:
        return None
    if not isinstance(actor, Mapping):
        raise TransientProviderFailureError(f"GitHub {field} must be an object")
    return _required_positive_int(actor, "id")


def _pull_request_marker(operation_id: OperationId) -> str:
    return f"{_PULL_REQUEST_MARKER_PREFIX}{operation_id} -->"


def _lifecycle_marker(operation_id: OperationId) -> str:
    return f"{_LIFECYCLE_MARKER_PREFIX}{operation_id} -->"


def verify_g4_evidence_pull_request(
    *,
    finding: G4DeficiencyFinding,
    principal_github_user_ids: Mapping[str, int],
    pull_request: Mapping[str, object],
    reviews: Sequence[object],
    verified_at: datetime,
) -> G4AuthorityEvidence:
    """Fail closed unless GitHub proves the finding's exact review evidence."""
    if pull_request.get("merged") is not True:
        raise StaleEvidenceError("G4 evidence pull request is not merged")
    if pull_request.get("number") != finding.evidence_pull_request_number:
        raise StaleEvidenceError("G4 evidence pull request number does not match the finding")
    head = pull_request.get("head")
    author = pull_request.get("user")
    body = pull_request.get("body")
    if (
        not isinstance(head, Mapping)
        or not isinstance(author, Mapping)
        or not isinstance(body, str)
    ):
        raise TransientProviderFailureError("GitHub evidence pull request payload is malformed")
    head_revision = _required_string(head, "sha")
    author_login = _required_string(author, "login").casefold()
    author_user_id = _required_github_user_id(author, "id")
    if author_login != finding.evidence_pull_request_author_login.casefold():
        raise StaleEvidenceError("G4 evidence pull request author does not match the finding")
    if Digest.of_bytes(body.encode("utf-8")) != finding.evidence_document_digest:
        raise StaleEvidenceError("G4 evidence pull request document digest does not match")
    producer_user_id = principal_github_user_ids.get(finding.producer_principal_id)
    if not _is_github_user_id(producer_user_id):
        raise InvalidInputError(
            "G4 finding producer is not a configured GitHub authority principal"
        )
    if producer_user_id == author_user_id:
        raise InvalidInputError("G4 evidence pull request author cannot be the finding producer")
    current_reviews: dict[int, tuple[str, str]] = {}
    for record in reviews:
        if not isinstance(record, Mapping):
            raise TransientProviderFailureError("GitHub evidence review payload is malformed")
        reviewer = record.get("user")
        if not isinstance(reviewer, Mapping):
            raise TransientProviderFailureError("GitHub evidence review is missing its reviewer")
        current_reviews[_required_github_user_id(reviewer, "id")] = (
            _required_string(record, "state").casefold(),
            _required_string(record, "commit_id"),
        )
    approvers = tuple(
        (principal_id, github_user_id)
        for principal_id, github_user_id in principal_github_user_ids.items()
        if (
            _is_github_user_id(github_user_id)
            and github_user_id != author_user_id
            and github_user_id != producer_user_id
            and current_reviews.get(github_user_id) == ("approved", head_revision)
        )
    )
    if len(approvers) < 2:
        raise StaleEvidenceError(
            "G4 evidence pull request requires two distinct current configured approvals"
        )
    return G4AuthorityEvidence(
        finding_id=finding.finding_id,
        pull_request_number=finding.evidence_pull_request_number,
        merged_head_revision=head_revision,
        document_digest=finding.evidence_document_digest,
        producer_github_user_id=producer_user_id,
        evidence_pull_request_author_github_user_id=author_user_id,
        approver_principal_ids=(approvers[0][0], approvers[1][0]),
        approver_github_user_ids=(approvers[0][1], approvers[1][1]),
        verified_at=verified_at,
    )


def _is_github_user_id(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _required_github_user_id(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if not _is_github_user_id(value):
        raise TransientProviderFailureError(
            f"GitHub payload field {field!r} must be a positive integer"
        )
    return value


def _raise_github_failure(stderr: str) -> None:
    detail = stderr.lower()
    if "http 401" in detail or "bad credentials" in detail or "authentication" in detail:
        raise AuthenticationFailureError("GitHub authentication failed")
    if "rate limit" in detail or "http 429" in detail:
        raise RateLimitError("GitHub rate limit exceeded")
    if "http 405" in detail or "http 409" in detail or "http 422" in detail:
        raise ExternalConflictError("GitHub rejected the requested mutation")
    raise TransientProviderFailureError("GitHub API request failed")


__all__ = [
    "GitHubAdapterConfig",
    "GitHubPullRequests",
    "GitHubReleaseAdapter",
    "GitHubReleaseRequest",
    "GitHubWorkLedger",
    "verify_g4_evidence_pull_request",
]
