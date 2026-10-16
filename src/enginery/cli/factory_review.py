"""Private review packets and repair-only feedback over the existing Stage 1 ledger."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from enginery.adapters.git_reader import DIFF_FLAGS, git_bytes
from enginery.adapters.review_presenters import GhAxiView, LavishPresenter, PresenterError
from enginery.application.branch_publication import ProtectedPathPolicy
from enginery.application.git_environment import repository_isolated_environment
from enginery.application.review_packet import MAX_PACKET_BYTES, ReviewPacket
from enginery.cli.config_locations import private_state_directory, read_user_layer
from enginery.cli.factory_collect import (
    database_path,
    ensure_private_directory,
    read_ledger,
    require_state_path,
)
from enginery.cli.setup_files import read_regular, refuse_links, write_private_atomically
from enginery.cli.stage1_composition import HEARTBEAT_WINDOW, read_only_service, runtime_for_run
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError
from enginery.domain.ids import NodeId, RunId
from enginery.engine.runtime import RUNTIME_NODE_AGGREGATE_TYPE, CoordinatorRuntime
from enginery.engine.task_repository import intake_path_for
from enginery.evidence.review_rendering import render_html, render_terminal
from enginery.ledger.redaction import assert_no_raw_credentials, redact_credential_shaped_text
from enginery.ledger.service import LedgerService
from enginery.workflows.review import ReviewFinding, ReviewReport
from enginery.workflows.stage1 import Stage1RunRequest, Stage1RunService, first_attempt_generation


def _json(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(raw.encode()) > MAX_PACKET_BYTES:
        raise InvalidInputError("review input exceeds the 1 MiB limit")
    return raw


def _projection(ledger: LedgerService, aggregate_type: str, aggregate_id: str) -> dict[str, object]:
    record = ledger.read_projection(aggregate_type=aggregate_type, aggregate_id=aggregate_id)
    state = {} if record is None else dict(record.state)
    _json(state)
    return state


def _git(path: Path, *arguments: str) -> str:
    return git_bytes(path, *arguments).decode("utf-8", errors="replace")


def _revision(path: Path, revision: str) -> str:
    resolved = _git(
        path, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"
    ).strip()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", resolved) is None:
        raise InvalidInputError("review revision is not a full Git commit identity")
    return resolved


def _receipt_document(root: Path, digest_text: str) -> dict[str, object]:
    algorithm, separator, hexadecimal = digest_text.partition(":")
    if not separator:
        raise InvalidInputError("review receipt reference is not a digest")
    digest = Digest(algorithm, hexadecimal)
    path = root / "objects" / digest.algorithm / digest.hex_value[:2] / digest.hex_value
    refuse_links(root.parent, path.relative_to(root.parent), what="review receipt artifact")
    if path.exists() and path.stat().st_nlink != 1:
        raise InvalidInputError("review receipt artifact must not be hard-linked")
    raw = read_regular(path, what="review receipt artifact")
    if raw is None:
        return {"digest": digest_text, "availability": "missing"}
    if Digest.of_bytes(raw) != digest:
        raise InvalidInputError("review receipt artifact digest mismatch")
    try:
        document: object = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise InvalidInputError("review receipt artifact is not JSON") from error
    return {"digest": digest_text, "availability": "digest_verified", "document": document}


def preimplementation_waits(ledger: LedgerService, request: Stage1RunRequest) -> tuple[str, ...]:
    """Find waits whose dependency closure does not include implementation."""

    def requires_implementation(node_id: str, visited: frozenset[str]) -> bool:
        if node_id == "implement":
            return True
        if node_id in visited:
            return False
        node = request.manifest.nodes[NodeId(node_id)]
        return any(
            requires_implementation(str(dependency), visited | {node_id})
            for dependency in node.dependencies
        )

    return tuple(
        str(node_id)
        for node_id in request.manifest.nodes
        if _projection(ledger, RUNTIME_NODE_AGGREGATE_TYPE, f"{request.run.id}:{node_id}").get(
            "status"
        )
        == "awaiting_human"
        and not requires_implementation(str(node_id), frozenset())
    )


def build_packet(project: Path, ledger: LedgerService, request: Stage1RunRequest) -> ReviewPacket:
    """Assemble one immutable subject from the run, not worker presentation documents."""
    run_id = str(request.run.id)
    runtime = CoordinatorRuntime(ledger, owner="factory")
    states = {
        name: _projection(ledger, RUNTIME_NODE_AGGREGATE_TYPE, f"{run_id}:{name}")
        for name in ("implement", "validate", "review", "open_pr", "wait_for_ci", "verify")
    }
    implementation = states["implement"]
    attempt_id = implementation.get("attempt_id")
    attempt = _projection(ledger, "node_attempt", attempt_id) if isinstance(attempt_id, str) else {}
    result = attempt.get("result")
    result = dict(result) if isinstance(result, Mapping) else {}
    head = result.get("head_revision")
    early_waits = preimplementation_waits(ledger, request)
    preimplementation = not isinstance(head, str) and bool(early_waits)
    if preimplementation:
        repository = request.repository_path
        base = _revision(repository, request.run.base_revision)
        head = ""
        diff = ""
        changed_paths: tuple[str, ...] = ()
    else:
        if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None:
            raise MissingPrerequisiteError("review requires the host-verified implementation head")
        # Only host-owned committed objects, never the isolated worker clone.
        repository = (
            intake_path_for(request.workspace_path)
            if request.runner.isolated_mode
            else request.workspace_path
        )
        if not request.runner.isolated_mode and _revision(repository, "HEAD") != head:
            raise InvalidInputError(
                "review head changed; collect and validate the current implementation"
            )
        bound_base = runtime.read_node_request(run_id=run_id, node_id="implement").base_revision
        base = _revision(repository, bound_base)
        diff = _git(repository, "diff", *DIFF_FLAGS, base, head, "--")
        changed_paths = tuple(
            value
            for value in _git(
                repository, "diff", "--name-only", "-z", *DIFF_FLAGS, base, head, "--"
            ).split("\0")
            if value
        )
    policy = ProtectedPathPolicy(request.protected_paths)
    protected_digest = str(
        Digest.of_json(
            {
                "patterns": list(policy.patterns),
                "base_revision": base,
                "head_revision": head,
                "matched_paths": list(policy.matched(changed_paths)),
            }
        )
    )
    observed = _projection(ledger, "behavioral_acceptance", run_id)
    configuration_digest = str(request.run.configuration_snapshot_digest)
    if (
        observed.get("head_revision") != head
        or observed.get("configuration_snapshot_digest") != configuration_digest
    ):
        observed = {}
    receipts: list[str] = []
    receipt = observed.get("receipt_digest")
    if isinstance(receipt, str):
        receipts.append(receipt)
    observations = observed.get("observation_receipt_digests")
    if isinstance(observations, list):
        receipts.extend(value for value in observations if isinstance(value, str))
    calibration = observed.get("calibration_receipt_digest")
    if isinstance(calibration, str):
        receipts.append(calibration)
    raw = request.initial_state()
    findings = states["review"].get("finding_ids", [])
    ci_state = (
        states["verify"]
        if states["verify"].get("github_evidence")
        or states["verify"].get("reason") == "github_evidence_incomplete"
        else states["wait_for_ci"]
    )
    ci = {
        key: value
        for key, value in ci_state.items()
        if key
        in {
            "head_revision",
            "configuration_snapshot_digest",
            "base_revision",
            "evidence_bundle_digest",
            "evidence_digest",
            "behavioral_receipt_digests",
            "github_evidence",
            "github_evidence_digest",
            "github_evidence_observations",
            "evidence_artifact_digest",
            "reason",
            "repair",
            "message",
        }
    }
    ci_head = ci.get("head_revision")
    ci_view: dict[str, object] = {
        "availability": "current_head"
        if ci
        and ci_head == head
        and ci.get("configuration_snapshot_digest") == configuration_digest
        else "not_persisted_for_current_head",
        "persisted": ci
        if ci_head == head and ci.get("configuration_snapshot_digest") == configuration_digest
        else {},
    }
    sections = (
        (
            "Intent",
            _json(
                {
                    "work_item": raw["work_item"],
                    "source_revision": raw["source_revision"],
                    "request_reference": str(
                        private_state_directory(project) / "runs" / run_id / "request.json"
                    ),
                }
            ),
        ),
        (
            "Reproduction evidence",
            _json(
                {
                    "artifact_root": str(request.execution_configuration.artifact_root),
                    "implementation_result": result,
                    "preimplementation_waits": list(early_waits) if preimplementation else [],
                    "validation": states["validate"],
                }
            ),
        ),
        (
            "Observed result",
            _json(
                {
                    "availability": "current_head"
                    if observed
                    else "not_persisted_for_current_head",
                    "display_only": True,
                    "behavioral_acceptance": observed,
                    "receipts": [
                        _receipt_document(request.execution_configuration.artifact_root, value)
                        for value in receipts
                    ],
                }
            ),
        ),
        (
            "Risk",
            _json(
                {
                    "classification_provenance": raw.get("classification_provenance"),
                    "protected_paths": list(request.protected_paths),
                    "changed_paths": list(changed_paths),
                    "matched_paths": list(
                        ProtectedPathPolicy(request.protected_paths).matched(changed_paths)
                    ),
                    "publication": states["open_pr"],
                }
            ),
        ),
        (
            "Routing and cost basis",
            _json(
                {
                    "execution_selection": request.execution_selection.to_state(),
                    "implementation": raw["implementation"],
                    "runner": request.runner.to_state(),
                }
            ),
        ),
    )
    packet = ReviewPacket(
        run_id=run_id,
        request_digest=str(request.digest),
        configuration_snapshot_digest=str(request.run.configuration_snapshot_digest),
        base_revision=base,
        head_revision=head,
        diff=diff,
        evidence_receipt_digests=tuple(receipts),
        ci_evidence_json=_json(ci_view),
        findings_json=_json(findings),
        sections=sections,
        protected_publication_digest=protected_digest,
        preimplementation_waits=early_waits if preimplementation else (),
    )
    # No packet, even a display-only one, may leak credential-shaped worker text.
    assert_no_raw_credentials(_json(packet.to_state()))
    return packet


def _open(
    project: Path,
    run_id: str,
    *,
    writable: bool = False,
    database: Path | None = None,
    owner: str = "factory",
) -> tuple[LedgerService, Stage1RunRequest]:
    RunId(run_id)
    if re.fullmatch(r"[A-Za-z0-9_.-]+", run_id) is None or run_id in {".", ".."}:
        raise InvalidInputError("review run id must be a plain identifier")
    if database is None:
        connection = read_ledger(project)
        if connection is None:
            raise MissingPrerequisiteError("factory review requires an existing project run")
        if writable:
            connection.close()
            path = database_path(project)
            require_state_path(path, what="project ledger")
            ledger = LedgerService.open(path)
        else:
            ledger = LedgerService(connection)
    else:
        path = database.expanduser().absolute()
        refuse_links(path.parent, Path(path.name), what="Stage 1 ledger")
        if not path.is_file():
            raise MissingPrerequisiteError("factory review requires an existing Stage 1 ledger")
        if writable:
            ledger = LedgerService.open(path)
        else:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=1")
            ledger = LedgerService(connection)
    try:
        runtime = runtime_for_run(ledger, owner=owner, run_id=run_id)
        request = Stage1RunService(runtime=runtime, ledger=ledger).read(RunId(run_id)).request
        return ledger, request
    except BaseException:
        ledger.close()
        raise


def read_packet(
    project: Path, run_id: str, *, database: Path | None = None, owner: str = "factory"
) -> ReviewPacket:
    """Rebuild the CURRENT subject; never trust a previously rendered packet."""
    ledger, request = _open(project, run_id, database=database, owner=owner)
    try:
        return build_packet(project, ledger, request)
    finally:
        ledger.close()


def _packet_path(project: Path, packet: ReviewPacket) -> Path:
    return (
        private_state_directory(project)
        / "review"
        / packet.run_id
        / packet.digest.hex_value
        / "packet.html"
    )


def review(
    project: Path, run_id: str, *, database: Path | None = None, owner: str = "factory"
) -> dict[str, object]:
    packet = read_packet(project, run_id, database=database, owner=owner)
    lines = render_terminal(packet)
    document = render_html(packet).encode()
    path = _packet_path(project, packet)
    ensure_private_directory(path.parent, what="review packet directory")
    require_state_path(path, what="review packet")
    existing = read_regular(path, what="review packet")
    if existing is not None and existing != document:
        raise InvalidInputError("immutable review packet path contains different bytes")
    if existing is None:
        write_private_atomically(path, document, what="review packet")
    return {
        "run_id": run_id,
        "packet_path": str(path),
        "subject_digest": packet.subject_digest,
        "lines": list(lines),
    }


def feedback(project: Path, run_id: str, *, subject_digest: str, text: str) -> dict[str, object]:
    """Queue an actionable finding only; an empty/non-actionable report is impossible."""
    if not text.strip() or len(text.encode()) > MAX_PACKET_BYTES:
        raise InvalidInputError("feedback must be non-blank and at most 1 MiB")
    ledger, request = _open(project, run_id, writable=True)
    try:
        packet = build_packet(project, ledger, request)
        payload = {
            "kind": "packet_feedback",
            "subject_digest": subject_digest,
            "text": redact_credential_shaped_text(text),
        }
        finding_id = _json(payload)
        previous: object = json.loads(packet.findings_json)
        prior_ids = (
            [value for value in previous if isinstance(value, str)]
            if isinstance(previous, list)
            else []
        )
        if finding_id in prior_ids:
            return {
                "run_id": run_id,
                "subject_digest": subject_digest,
                "feedback_queued": True,
                "already_queued": True,
                "authority_granted": False,
                "finding_id": str(Digest.of_json(payload)),
            }
        if subject_digest != packet.subject_digest:
            raise InvalidInputError("feedback subject is stale; run factory review again")
        finding_ids = tuple(dict.fromkeys((*prior_ids, finding_id)))
        _json(list(finding_ids))
        runtime = runtime_for_run(ledger, owner="factory", run_id=run_id)
        implementation = _projection(ledger, RUNTIME_NODE_AGGREGATE_TYPE, f"{run_id}:implement")
        attempt = implementation.get("attempt_id")
        if not isinstance(attempt, str):
            raise MissingPrerequisiteError("feedback requires a collected implementation")
        repair_attempt = first_attempt_generation(attempt) - first_attempt_generation(
            request.implementation.attempt_id
        )
        service = read_only_service(runtime=runtime, ledger=ledger, request=request)
        result = service.review_implementation(
            request,
            ReviewReport(
                producer="implementation_worker",
                reviewer="packet_feedback",
                findings=tuple(
                    ReviewFinding(value, actionable=True, blocking=False) for value in finding_ids
                ),
            ),
            repair_attempt=repair_attempt,
            packet_feedback_digest=Digest.of_json(payload).hex_value,
            now=datetime.now(UTC),
            heartbeat_window=HEARTBEAT_WINDOW,
        )
        return {
            "run_id": run_id,
            "subject_digest": subject_digest,
            "review_outcome": result.outcome.value,
            "feedback_queued": True,
            "authority_granted": False,
            "finding_id": str(Digest.of_json(payload)),
        }
    finally:
        ledger.close()


def _presenter(project: Path, *, run_id: str, port: int, no_open: bool) -> LavishPresenter:
    user = read_user_layer()
    programs = None if user is None else user.operator_executables
    if programs is None or programs.lavish is None:
        raise InvalidInputError(
            "Lavish is not configured; set [executables].lavish in user preferences "
            "or use the terminal packet"
        )
    root = private_state_directory(project)
    state = root / "review" / run_id / "lavish"
    ensure_private_directory(state, what="private review presenter state")
    try:
        return LavishPresenter(programs.lavish, state, port, no_open=no_open, packet_root=root)
    except PresenterError as error:
        raise InvalidInputError(str(error)) from error


def present_review(
    project: Path, report: Mapping[str, object], *, port: int, no_open: bool
) -> None:
    try:
        _presenter(project, run_id=str(report["run_id"]), port=port, no_open=no_open).open(
            Path(str(report["packet_path"]))
        )
    except PresenterError as error:
        raise InvalidInputError(str(error)) from error


def lavish_feedback(
    project: Path, run_id: str, *, subject_digest: str, port: int
) -> dict[str, object]:
    packet = read_packet(project, run_id)
    presenter = _presenter(project, run_id=run_id, port=port, no_open=True)
    if packet.subject_digest != subject_digest:
        raise InvalidInputError("feedback subject is stale; run factory review again")
    path = _packet_path(project, packet)
    try:
        findings = presenter.poll(path)
        if not findings:
            return {
                "run_id": run_id,
                "subject_digest": subject_digest,
                "feedback_queued": False,
                "authority_granted": False,
            }
        return feedback(project, run_id, subject_digest=subject_digest, text="\n".join(findings))
    except PresenterError as error:
        raise InvalidInputError(str(error)) from error


def github_view(project: Path, run_id: str, report: Mapping[str, object]) -> dict[str, object]:
    """Optional read-only presentation; none of this output can become evidence."""
    user = read_user_layer()
    programs = None if user is None else user.operator_executables
    result: dict[str, object] = {"display_only": True}
    if programs is None or programs.gh_axi is None:
        return {
            **result,
            "note": "gh-axi is not configured in user preferences [executables].gh_axi",
        }
    pull_request = report.get("pull_request")
    number = pull_request.get("number") if isinstance(pull_request, Mapping) else None
    if type(number) is not int:
        return {**result, "note": "this run has no durable pull request to display"}
    ledger, request = _open(project, run_id)
    try:
        view = GhAxiView(programs.gh_axi)
        repository = request.execution_configuration.github_repository
        result["view"] = view.pr_view(repository, number)
        result["checks"] = view.checks(repository, number)
        return result
    except PresenterError as error:
        return {**result, "note": str(error)}
    finally:
        ledger.close()
