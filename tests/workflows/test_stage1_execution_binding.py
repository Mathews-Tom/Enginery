from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.support.execution import selection_for
from tests.workflows.test_stage1_runtime import (
    _check_disposition_fixture,
    _check_node_dispatch,
    _complete_check_node,
    _drive_check_to_non_pass,
)

from enginery.application.execution_catalog import load_execution_catalog
from enginery.application.execution_controls import build_receipt
from enginery.domain.digests import Digest
from enginery.domain.errors import HumanActionRequiredError, InvalidInputError, StaleEvidenceError
from enginery.domain.execution_catalog import (
    ExactHostVersion,
    ExecutionCatalog,
    InstalledHost,
    SupportTier,
)
from enginery.domain.execution_receipt import evaluate_effective_requirement
from enginery.domain.execution_selection import (
    ExecutionSelection,
    HoldReason,
    RequestedControl,
    SelectionLayer,
)
from enginery.domain.ids import RunId
from enginery.engine.scheduler import SchedulingLimits
from enginery.ledger.events import AppendCommand, EventWrite
from enginery.ledger.service import LedgerService
from enginery.workflows.evidence import Stage1EvidenceBundle
from enginery.workflows.issue_to_pr import STAGE1_VERIFY_CHECK_ID
from enginery.workflows.pull_request import PullRequestOutcome
from enginery.workflows.review import ReviewReport
from enginery.workflows.stage1 import (
    Stage1ProgressionAction,
    Stage1RunRequest,
    stage1_request_from_state,
)

_WINDOW = timedelta(seconds=60)
_LIMITS = SchedulingLimits(global_concurrency=1, per_repository_concurrency=1)
_PIN = selection_for("omp", model="openai/gpt-5.2", effort="high")
_PRE_SELECTION_KEYS = {
    "run_id",
    "run",
    "work_item",
    "source_revision",
    "manifest",
    "repository_id",
    "repository_path",
    "workspace_path",
    "base_branch",
    "head_branch",
    "validation_commands",
    "applicable_criteria",
    "required_checks",
    "repair_limit",
    "implementation",
    "execution_configuration",
    "status",
    "request_digest",
}


def _drifted_catalog() -> ExecutionCatalog:
    return replace(
        load_execution_catalog(), digest=Digest.of_bytes(b"a later catalog"), version="3"
    )


def _legacy(request: Stage1RunRequest) -> Stage1RunRequest:
    return replace(request, execution_selection=ExecutionSelection.legacy_host_default("omp"))


def test_a_new_request_digest_binds_the_selection_and_changes_with_it(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    host_default, *_ = _check_disposition_fixture(ledger_service, tmp_path, start=False)
    pinned = replace(host_default, execution_selection=_PIN)
    other_model = replace(
        host_default, execution_selection=selection_for("omp", model="openai/gpt-5.3")
    )

    assert host_default.initial_state()["execution_selection"] == (
        host_default.execution_selection.to_state()
    )
    assert len({host_default.digest, pinned.digest, other_model.digest}) == 3
    assert pinned.initial_state()["request_digest"] == str(pinned.digest)
    decoded = stage1_request_from_state(pinned.initial_state())
    assert decoded == pinned
    assert decoded.execution_selection.selected_model == "openai/gpt-5.2"


def test_a_request_rejects_a_selection_for_a_different_harness(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, *_ = _check_disposition_fixture(ledger_service, tmp_path, start=False)

    with pytest.raises(InvalidInputError, match="must match the configured harness"):
        replace(request, execution_selection=selection_for("claude-code"))


def test_a_new_run_must_bind_an_explicit_selection(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(ledger_service, tmp_path, start=False)

    with pytest.raises(InvalidInputError, match="execution selection"):
        service.start(_legacy(request), now=now, heartbeat_window=_WINDOW)

    assert (
        ledger_service.read_projection(aggregate_type="run", aggregate_id=str(request.run.id))
        is None
    )


def test_a_persisted_pre_selection_run_keeps_decoding_and_its_original_fingerprint(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, runtime, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False
    )
    legacy = _legacy(request)
    state = legacy.initial_state()
    runtime.register_run(
        run_id=str(legacy.run.id), initial_state=state, now=now, heartbeat_window=_WINDOW
    )

    assert set(state) == _PRE_SELECTION_KEYS
    restarted = service.start(legacy, now=now, heartbeat_window=_WINDOW)
    read_back = service.read(legacy.run.id)

    assert restarted.request == legacy
    assert read_back.request.execution_selection.legacy
    assert read_back.request.digest == legacy.digest
    projection = ledger_service.read_projection(
        aggregate_type="run", aggregate_id=str(legacy.run.id)
    )
    assert projection is not None
    assert dict(projection.state) == state


def test_resuming_a_pre_selection_run_invents_no_selection_and_never_drifts(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, runtime, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False
    )
    legacy = _legacy(request)
    runtime.register_run(
        run_id=str(legacy.run.id),
        initial_state=legacy.initial_state(),
        now=now,
        heartbeat_window=_WINDOW,
    )
    drifted = replace(service, execution_catalog=_drifted_catalog())

    progression = drifted.next_action(legacy.run.id)
    restarted = drifted.start(legacy, now=now, heartbeat_window=_WINDOW)

    assert progression.action is Stage1ProgressionAction.QUALIFY
    assert progression.hold is None
    selection = restarted.request.execution_selection
    assert selection.legacy
    assert selection.selected_model is None
    assert selection.selected_effort is None
    assert selection.catalog_digest is None
    assert not selection.requires_native_controls


def test_a_changed_catalog_holds_the_run_and_retains_its_original_binding(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )
    service.start(request, now=now, heartbeat_window=_WINDOW)
    before = ledger_service.read_projection(aggregate_type="run", aggregate_id=str(request.run.id))
    drifted = replace(service, execution_catalog=_drifted_catalog())

    assert service.next_action(request.run.id).action is Stage1ProgressionAction.QUALIFY
    progression = drifted.next_action(request.run.id)

    assert progression.action is Stage1ProgressionAction.HOLD
    assert progression.hold is not None
    assert progression.hold.reason is HoldReason.CATALOG_DRIFT
    assert progression.hold.details["bound_catalog_digest"] == str(load_execution_catalog().digest)
    assert progression.hold.details["current_catalog_digest"] == str(
        Digest.of_bytes(b"a later catalog")
    )
    advanced = drifted.advance(
        request.run.id,
        now=now,
        heartbeat_window=_WINDOW,
        lease_window=_WINDOW,
        limits=_LIMITS,
    )
    assert advanced.action is Stage1ProgressionAction.HOLD
    assert (
        ledger_service.read_projection(
            aggregate_type="runtime_node", aggregate_id="run-check:qualify"
        )
        is None
    )
    after = ledger_service.read_projection(aggregate_type="run", aggregate_id=str(request.run.id))
    assert before is not None and after is not None
    assert dict(after.state) == dict(before.state)
    assert drifted.read(request.run.id).request.execution_selection == _PIN


def test_every_service_action_is_refused_while_the_catalog_has_drifted(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )
    service.start(request, now=now, heartbeat_window=_WINDOW)
    drifted = replace(service, execution_catalog=_drifted_catalog())

    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.qualify(
            request,
            external_reference=request.work_snapshot.work_item.external_reference,
            applicable_criteria=request.applicable_criteria,
            now=now,
            heartbeat_window=_WINDOW,
        )
    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.dispatch_implementation(
            request, now=now, heartbeat_window=_WINDOW, lease_window=_WINDOW, limits=_LIMITS
        )
    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.collect_implementation(request, now=now, heartbeat_window=_WINDOW)
    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.validate_implementation(request, now=now, heartbeat_window=_WINDOW)
    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.open_pull_request(request, now=now, heartbeat_window=_WINDOW)
    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.wait_for_ci(request, now=now, heartbeat_window=_WINDOW)
    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.verify_merge_ready(request, now=now, heartbeat_window=_WINDOW)


def test_a_request_that_is_not_the_durable_binding_is_refused_as_stale(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(ledger_service, tmp_path, start=False)
    service.start(request, now=now, heartbeat_window=_WINDOW)
    rebound = replace(request, execution_selection=_PIN)

    with pytest.raises(StaleEvidenceError, match="durable run binding"):
        service.dispatch_implementation(
            rebound, now=now, heartbeat_window=_WINDOW, lease_window=_WINDOW, limits=_LIMITS
        )


def test_a_run_whose_selection_differs_from_the_bound_one_cannot_reach_any_decision_or_output(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )
    service.start(request, now=now, heartbeat_window=_WINDOW)
    rebound = replace(request, execution_selection=selection_for("omp"))

    for refused in (
        lambda: service.review_implementation(
            rebound,
            ReviewReport(producer="omp", reviewer="human", findings=()),
            repair_attempt=0,
            now=now,
            heartbeat_window=_WINDOW,
        ),
        lambda: service.validate_implementation(rebound, now=now, heartbeat_window=_WINDOW),
        lambda: service.open_pull_request(rebound, now=now, heartbeat_window=_WINDOW),
        lambda: service.wait_for_ci(rebound, now=now, heartbeat_window=_WINDOW),
        lambda: service.verify_merge_ready(rebound, now=now, heartbeat_window=_WINDOW),
    ):
        with pytest.raises(StaleEvidenceError, match="durable run binding"):
            refused()

    assert service.read(request.run.id).request.execution_selection == _PIN


def test_a_pinned_run_binds_its_selection_digest_into_the_verify_node_and_evidence_bundle(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    (tmp_path / "pinned").mkdir()
    (tmp_path / "default").mkdir()
    pinned_service, pinned_request, _ = _drive_check_to_non_pass(
        ledger_service,
        tmp_path / "pinned",
        check_id=STAGE1_VERIFY_CHECK_ID,
        result="pass",
        execution_selection=_PIN,
    )
    other = LedgerService.open(tmp_path / "default.db")
    try:
        default_service, _, _ = _drive_check_to_non_pass(
            other, tmp_path / "default", check_id=STAGE1_VERIFY_CHECK_ID, result="pass"
        )

        pinned_node = pinned_service.ledger.read_projection(
            aggregate_type="runtime_node", aggregate_id="run-check:verify"
        )
        default_node = default_service.ledger.read_projection(
            aggregate_type="runtime_node", aggregate_id="run-check:verify"
        )
    finally:
        other.close()

    assert pinned_node is not None and default_node is not None
    assert pinned_node.state["status"] == "passed"
    assert pinned_node.state["execution_selection_digest"] == str(_PIN.digest)
    assert pinned_request.execution_selection.digest == _PIN.digest
    assert default_node.state["execution_selection_digest"] == str(selection_for("omp").digest)
    assert default_node.state["execution_selection_digest"] != str(_PIN.digest)
    assert pinned_node.state["evidence_digest"] != default_node.state["evidence_digest"]


def test_evidence_is_superseded_by_any_change_to_the_bound_selection() -> None:
    def bundle(selection_digest: Digest | None) -> Stage1EvidenceBundle:
        return Stage1EvidenceBundle(
            issue_revision="issue",
            base_revision="base",
            head_revision="head",
            pull_request_number=1,
            implementation_artifacts=(Digest.of_bytes(b"implementation"),),
            verification_artifacts=(Digest.of_bytes(b"verification"),),
            outcome=PullRequestOutcome.MERGE_READY,
            observed_at=datetime(2026, 7, 19, tzinfo=UTC),
            execution_selection_digest=selection_digest,
        )

    legacy = bundle(None)
    pinned = bundle(_PIN.digest)
    other = bundle(selection_for("omp", model="openai/gpt-5.3").digest)

    assert len({legacy.digest, pinned.digest, other.digest}) == 3
    subjects = {"issue_revision": "issue", "base_revision": "base", "head_revision": "head"}
    assert pinned.current_for(**subjects, execution_selection_digest=_PIN.digest)
    assert not pinned.current_for(
        **subjects, execution_selection_digest=other.execution_selection_digest
    )
    assert not pinned.current_for(**subjects)
    assert legacy.current_for(**subjects)


def test_a_hold_is_attached_to_waiting_and_human_review_progressions_too(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, runtime, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )
    service.start(request, now=now, heartbeat_window=_WINDOW)
    drifted = replace(service, execution_catalog=_drifted_catalog())
    epoch = runtime.register_node(
        dispatch=_check_node_dispatch(request, "qualify"), now=now, heartbeat_window=_WINDOW
    )

    waiting = drifted.next_action(request.run.id)

    assert waiting.action is Stage1ProgressionAction.WAIT
    assert waiting.hold is not None and waiting.hold.reason is HoldReason.CATALOG_DRIFT
    assert service.next_action(request.run.id).hold is None

    runtime.complete_node(
        run_id=str(request.run.id),
        node_id="qualify",
        epoch=epoch.epoch,
        now=now,
        outcome="failed",
    )
    reviewing = drifted.next_action(request.run.id)

    assert reviewing.action is Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
    assert reviewing.hold is not None and reviewing.hold.reason is HoldReason.CATALOG_DRIFT


def test_a_review_is_refused_while_the_catalog_has_drifted(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )
    service.start(request, now=now, heartbeat_window=_WINDOW)
    drifted = replace(service, execution_catalog=_drifted_catalog())

    with pytest.raises(HumanActionRequiredError, match="catalog changed"):
        drifted.review_implementation(
            request,
            ReviewReport(producer="omp", reviewer="human", findings=()),
            repair_attempt=0,
            now=now,
            heartbeat_window=_WINDOW,
        )


def _tampered(selection: ExecutionSelection) -> list[tuple[ExecutionSelection, str]]:
    return [
        (
            replace(
                selection,
                model=RequestedControl(value="opus", source=SelectionLayer.RUN, pinned=True),
                selected_model="opus",
            ),
            "not an exact omp model pin",
        ),
        (
            replace(
                selection,
                effort=RequestedControl(value="auto", source=SelectionLayer.RUN, pinned=True),
                selected_effort="auto",
            ),
            "cannot be pinned",
        ),
        (replace(selection, support_tier=SupportTier.QUALIFIED), "tested host entry"),
        (replace(selection, host_version=ExactHostVersion("9.9.9")), "tested host entry"),
    ]


def test_a_tampered_selection_cannot_start_a_new_run(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )

    for tampered, match in _tampered(_PIN):
        with pytest.raises(InvalidInputError, match=match):
            service.start(
                replace(request, execution_selection=tampered), now=now, heartbeat_window=_WINDOW
            )

    assert (
        ledger_service.read_projection(aggregate_type="run", aggregate_id=str(request.run.id))
        is None
    )


def test_a_tampered_persisted_selection_is_refused_wherever_executability_is_required(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, runtime, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )
    for index, (tampered, match) in enumerate(_tampered(_PIN)):
        forged = replace(
            request,
            run=replace(request.run, id=RunId(f"forged-{index}")),
            execution_selection=tampered,
        )
        runtime.register_run(
            run_id=str(forged.run.id),
            initial_state=forged.initial_state(),
            now=now,
            heartbeat_window=_WINDOW,
        )

        with pytest.raises(InvalidInputError, match=match):
            service.dispatch_implementation(
                forged, now=now, heartbeat_window=_WINDOW, lease_window=_WINDOW, limits=_LIMITS
            )


class _StubHarness:
    """A harness stand-in whose installed host is whatever the test says it is."""

    def __init__(self, installed: InstalledHost | None) -> None:
        self.installed = installed
        self.artifact_store = None

    def installed_host(self) -> InstalledHost | None:
        return self.installed

    def supervised_command(self, task: object, *, result_path: Path) -> tuple[str, ...]:
        raise AssertionError("no worker command may be built while the host is held")


_BOUND_HOST = InstalledHost("18.10.3", "json-protocol=3")


@pytest.mark.parametrize(
    "installed",
    [InstalledHost("18.10.2", "json-protocol=3"), None],
    ids=["changed", "unobservable"],
)
def test_a_changed_or_unobservable_host_holds_a_run_with_native_controls(
    ledger_service: LedgerService, tmp_path: Path, installed: InstalledHost | None
) -> None:
    request, service, _, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )
    service.start(request, now=now, heartbeat_window=_WINDOW)
    healthy = replace(service, harness=_StubHarness(_BOUND_HOST))  # type: ignore[arg-type]
    drifted = replace(service, harness=_StubHarness(installed))  # type: ignore[arg-type]

    assert healthy.next_action(request.run.id).action is Stage1ProgressionAction.QUALIFY
    progression = drifted.next_action(request.run.id)

    assert progression.action is Stage1ProgressionAction.HOLD
    assert progression.hold is not None
    assert progression.hold.reason is HoldReason.HOST_FINGERPRINT_DRIFT
    with pytest.raises(HumanActionRequiredError, match="not a host this run"):
        drifted.dispatch_implementation(
            request, now=now, heartbeat_window=_WINDOW, lease_window=_WINDOW, limits=_LIMITS
        )
    assert drifted.read(request.run.id).request.execution_selection == _PIN


def test_a_host_default_run_never_depends_on_the_installed_host_version(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(ledger_service, tmp_path, start=False)
    service.start(request, now=now, heartbeat_window=_WINDOW)
    other = replace(service, harness=_StubHarness(InstalledHost("0.0.1", "other")))  # type: ignore[arg-type]

    assert other.next_action(request.run.id).action is Stage1ProgressionAction.QUALIFY


def test_a_new_run_with_a_forged_support_tier_or_fingerprint_is_rejected(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    request, service, _, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, start=False, execution_selection=_PIN
    )
    forged = replace(
        request,
        execution_selection=replace(_PIN, host_version=ExactHostVersion("0.0.1")),
    )

    with pytest.raises(InvalidInputError, match="tested host entry"):
        service.start(forged, now=now, heartbeat_window=_WINDOW)

    assert (
        ledger_service.read_projection(aggregate_type="run", aggregate_id=str(request.run.id))
        is None
    )


def test_a_recorded_execution_hold_makes_the_next_action_hold_with_that_hold(
    ledger_service: LedgerService, tmp_path: Path
) -> None:
    strict = selection_for("omp", model="openai/gpt-5.2", strict_effective=True)
    request, service, runtime, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, execution_selection=strict
    )
    _complete_check_node(runtime, request, "qualify", now)
    unverified = evaluate_effective_requirement(
        strict,
        build_receipt(
            strict,
            harness="omp",
            catalog=load_execution_catalog(),
            launched_command=("omp", "--model", "openai/gpt-5.2"),
            models=(),
            observed_at_launch="18.10.3",
        ),
    )
    assert unverified is not None
    epoch = runtime.register_node(
        dispatch=_check_node_dispatch(request, "implement"), now=now, heartbeat_window=_WINDOW
    )
    runtime.complete_node(
        run_id=str(request.run.id),
        node_id="implement",
        epoch=epoch.epoch,
        now=now,
        outcome="failed",
    )
    ledger_service.append(
        AppendCommand(
            correlation_id="execution-hold",
            events=(
                EventWrite(
                    aggregate_type="node_attempt",
                    aggregate_id=request.implementation.attempt_id,
                    expected_version=0,
                    event_type="node_attempt.result_ingested",
                    schema_version=1,
                    payload={"result": {"execution_hold": unverified.to_state()}},
                ),
            ),
        )
    )

    progression = service.next_action(request.run.id)

    assert progression.action is Stage1ProgressionAction.HOLD
    assert progression.hold == unverified
    assert progression.hold.reason is HoldReason.EFFECTIVE_CONTROL_UNVERIFIED
    # Without a recorded hold the same failed implementation waits for a human instead.
    other = LedgerService.open(tmp_path / "plain.db")
    try:
        (tmp_path / "plain").mkdir()
        plain_request, plain_service, plain_runtime, _, plain_now = _check_disposition_fixture(
            other, tmp_path / "plain", execution_selection=strict
        )
        _complete_check_node(plain_runtime, plain_request, "qualify", plain_now)
        plain_epoch = plain_runtime.register_node(
            dispatch=_check_node_dispatch(plain_request, "implement"),
            now=plain_now,
            heartbeat_window=_WINDOW,
        )
        plain_runtime.complete_node(
            run_id=str(plain_request.run.id),
            node_id="implement",
            epoch=plain_epoch.epoch,
            now=plain_now,
            outcome="failed",
        )
        plain = plain_service.next_action(plain_request.run.id)
    finally:
        other.close()
    assert plain.action is Stage1ProgressionAction.AWAIT_HUMAN_REVIEW
    assert plain.hold is None


def test_collect_implementation_gives_the_executor_the_services_own_catalog(
    ledger_service: LedgerService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, service, runtime, _, now = _check_disposition_fixture(
        ledger_service, tmp_path, execution_selection=_PIN
    )
    injected = replace(load_execution_catalog())
    service = replace(
        service,
        harness=_StubHarness(InstalledHost("18.10.3", "json-protocol=3")),  # type: ignore[arg-type]
        execution_catalog=injected,
    )
    _complete_check_node(runtime, request, "qualify", now)
    runtime.register_node(
        dispatch=_check_node_dispatch(request, "implement"), now=now, heartbeat_window=_WINDOW
    )
    captured: list[ExecutionCatalog] = []

    class _Spy:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs["catalog"])  # type: ignore[arg-type]

        def collect(self, **kwargs: object) -> str:
            return "collected"

    monkeypatch.setattr("enginery.workflows.stage1.Stage1ImplementationExecutor", _Spy)
    monkeypatch.setattr(
        "enginery.engine.runtime.CoordinatorRuntime.recover_dispatched",
        lambda self, **kwargs: None,
    )

    result = service.collect_implementation(request, now=now, heartbeat_window=_WINDOW)

    assert result == "collected"
    assert len(captured) == 1 and captured[0] is injected
