"""GitHub CLI-backed adapters that emit only normalized application values."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import quote

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.work_ports import (
    LifecycleProjection,
    PullRequestCheck,
    PullRequestEvidence,
    PullRequestRequest,
    PullRequestReview,
    PullRequestSnapshot,
    WorkLedgerSnapshot,
)
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import (
    AmbiguousExternalSideEffectError,
    AuthenticationFailureError,
    ExternalConflictError,
    InvalidInputError,
    RateLimitError,
    StaleEvidenceError,
    TransientProviderFailureError,
)
from enginery.domain.ids import OperationId, WorkItemId
from enginery.domain.node_attempt import ReconciliationResult
from enginery.domain.work_item import WorkItem, WorkItemState

_GITHUB_API_VERSION = "2026-03-10"
_PAGE_SIZE = 100
_LIFECYCLE_MARKER_PREFIX = "<!-- enginery:lifecycle:"
_PULL_REQUEST_MARKER_PREFIX = "<!-- enginery:pull-request:"


@dataclass(frozen=True, slots=True)
class GitHubAdapterConfig:
    """Opaque GitHub CLI configuration for one repository."""

    repository: str
    credential_reference: str
    executable: str = "gh"
    api_version: str = _GITHUB_API_VERSION

    def __post_init__(self) -> None:
        if not _is_repository_name(self.repository):
            raise InvalidInputError("GitHub repository must use owner/name form")
        if not self.credential_reference.strip():
            raise InvalidInputError("GitHub credential reference must be non-blank")
        if not self.executable.strip():
            raise InvalidInputError("GitHub CLI executable must be non-blank")
        if not self.api_version.strip():
            raise InvalidInputError("GitHub API version must be non-blank")


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

    def fetch(self, external_reference: str) -> WorkLedgerSnapshot:
        issue_number = self._issue_number(external_reference)
        payload = self._request_object(
            "GET", f"repos/{self.config.repository}/issues/{issue_number}"
        )
        if "pull_request" in payload:
            raise InvalidInputError("GitHub pull requests cannot be ingested as issues")
        return self._snapshot(payload, issue_number)

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

    def _snapshot(self, payload: Mapping[str, object], issue_number: int) -> WorkLedgerSnapshot:
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
        work_item = WorkItem(
            id=WorkItemId(f"github:{self.config.repository}#{issue_number}"),
            work_kind=WorkKind.ISSUE,
            source_provider="github-issues",
            external_reference=f"{self.config.repository}#{issue_number}",
            source_snapshot_reference=source_snapshot_reference,
            title=title,
            objective=body,
            acceptance_criteria=(body,),
            constraints=(),
            risk_class=RiskClass.LOW,
            repository_targets=(self.config.repository,),
            dependencies=(),
            state=WorkItemState.NEW,
        )
        return WorkLedgerSnapshot(
            work_item=work_item,
            source_revision=f"{updated_at}:{work_item.bound_field_digest}",
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

    def _issue_number(self, external_reference: str) -> int:
        prefix = f"{self.config.repository}#"
        if not external_reference.startswith(prefix):
            raise InvalidInputError(
                "GitHub issue reference does not belong to the configured repository"
            )
        number = external_reference.removeprefix(prefix)
        if not number.isdecimal() or int(number) < 1:
            raise InvalidInputError("GitHub issue reference must end with a positive issue number")
        return int(number)

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
        return _request(self.config, self.command_runner, method, endpoint, *fields)

    def _run(self, arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return _run(self.command_runner, arguments)


@dataclass(slots=True)
class GitHubPullRequests:
    """Create and reconcile GitHub pull requests through deterministic markers."""

    config: GitHubAdapterConfig
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]] = field(
        default=lambda arguments: subprocess.run(
            arguments, check=False, capture_output=True, text=True
        )
    )
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
        payload = self._request_object("GET", f"repos/{self.config.repository}/pulls/{number}")
        snapshot = _pull_request_snapshot(payload)
        mergeable_raw = payload.get("mergeable")
        if mergeable_raw is not None and not isinstance(mergeable_raw, bool):
            raise TransientProviderFailureError(
                "GitHub pull request mergeability must be boolean or null"
            )
        reviews = tuple(
            PullRequestReview(
                reviewer=_required_string(_required_object(review, "user"), "login"),
                state=_required_string(review, "state"),
            )
            for review in self._paginate_array(
                f"repos/{self.config.repository}/pulls/{snapshot.number}/reviews"
            )
            if isinstance(review, Mapping)
        )
        checks = self._checks(snapshot)
        latest = self.get(snapshot.number)
        if (
            latest.head_revision != snapshot.head_revision
            or latest.base_revision != snapshot.base_revision
        ):
            raise StaleEvidenceError("GitHub pull request changed while evidence was collected")
        return PullRequestEvidence(
            pull_request=snapshot,
            reviews=reviews,
            checks=checks,
            mergeable=mergeable_raw,
        )

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

    def _checks(self, snapshot: PullRequestSnapshot) -> tuple[PullRequestCheck, ...]:
        page = 1
        checks: list[PullRequestCheck] = []
        while True:
            payload = self._request_object(
                "GET",
                (
                    f"repos/{self.config.repository}/commits/{snapshot.head_revision}/check-runs"
                    f"?per_page={_PAGE_SIZE}&page={page}"
                ),
            )
            runs = payload.get("check_runs")
            if not isinstance(runs, list):
                raise TransientProviderFailureError(
                    "GitHub check-runs response must contain an array"
                )
            for run in runs:
                if not isinstance(run, Mapping):
                    raise TransientProviderFailureError("GitHub check-run record must be an object")
                head_revision = _required_string(run, "head_sha")
                if head_revision != snapshot.head_revision:
                    raise StaleEvidenceError("GitHub check run is bound to a stale head revision")
                conclusion = run.get("conclusion")
                if conclusion is not None and not isinstance(conclusion, str):
                    raise TransientProviderFailureError(
                        "GitHub check-run conclusion must be a string or null"
                    )
                checks.append(
                    PullRequestCheck(
                        name=_required_string(run, "name"),
                        status=_required_string(run, "status"),
                        conclusion=conclusion,
                        head_revision=head_revision,
                    )
                )
            if len(runs) < _PAGE_SIZE:
                break
            page += 1
        return tuple(checks)

    def _paginate_array(self, endpoint: str) -> tuple[object, ...]:
        page = 1
        records: list[object] = []
        separator = "&" if "?" in endpoint else "?"
        while True:
            payload = self._request_array(
                "GET", f"{endpoint}{separator}per_page={_PAGE_SIZE}&page={page}"
            )
            records.extend(payload)
            if len(payload) < _PAGE_SIZE:
                break
            page += 1
        return tuple(records)

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


def _request(
    config: GitHubAdapterConfig,
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]],
    method: str,
    endpoint: str,
    *fields: str,
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
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise TransientProviderFailureError("GitHub API returned invalid JSON") from error


def _run(
    command_runner: Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]],
    arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    result = command_runner(arguments)
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
    if not isinstance(value, int) or value < 1:
        raise TransientProviderFailureError(
            f"GitHub response field {field_name!r} must be a positive integer"
        )
    return value


def _pull_request_snapshot(payload: Mapping[str, object]) -> PullRequestSnapshot:
    head = payload.get("head")
    base = payload.get("base")
    if not isinstance(head, Mapping) or not isinstance(base, Mapping):
        raise TransientProviderFailureError("GitHub pull request lacks head or base metadata")
    return PullRequestSnapshot(
        number=_required_positive_int(payload, "number"),
        url=_required_string(payload, "html_url"),
        state=_required_string(payload, "state"),
        head_branch=_required_string(head, "ref"),
        head_revision=_required_string(head, "sha"),
        base_branch=_required_string(base, "ref"),
        base_revision=_required_string(base, "sha"),
    )


def _pull_request_marker(operation_id: OperationId) -> str:
    return f"{_PULL_REQUEST_MARKER_PREFIX}{operation_id} -->"


def _lifecycle_marker(operation_id: OperationId) -> str:
    return f"{_LIFECYCLE_MARKER_PREFIX}{operation_id} -->"


def _raise_github_failure(stderr: str) -> None:
    detail = stderr.lower()
    if "http 401" in detail or "bad credentials" in detail or "authentication" in detail:
        raise AuthenticationFailureError("GitHub authentication failed")
    if "rate limit" in detail or "http 429" in detail:
        raise RateLimitError("GitHub rate limit exceeded")
    if "http 409" in detail or "http 422" in detail:
        raise ExternalConflictError("GitHub rejected the requested mutation")
    raise TransientProviderFailureError("GitHub API request failed")


__all__ = ["GitHubAdapterConfig", "GitHubPullRequests", "GitHubWorkLedger"]
