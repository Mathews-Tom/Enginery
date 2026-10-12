"""Behavior tests for the digest-frozen acceptance baseline."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from enginery.domain.workflow.check import CheckDisposition, CheckObserver, CommandSource
from enginery.evidence.acceptance_baseline import (
    AcceptanceBaselineError,
    load_acceptance_baseline,
    parse_acceptance_baseline,
    parse_baseline_document,
)
from enginery.workflows.issue_to_pr import (
    STAGE1_VALIDATE_CHECK_ID,
    STAGE1_VERIFY_CHECK_ID,
    stage1_work_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_FILE = REPO_ROOT / "src" / "enginery" / "config" / "acceptance-baseline-v1.toml"

# The frozen identity of baseline version 1: these journeys are the contract.
V1_JOURNEY_IDS = (
    "stage1_issue_to_merge_ready_pull_request",
    "stage2_plan_to_verified_release",
    "stage3_incident_to_hotfix_and_rollback",
    "doctor_reports_local_prerequisites",
    "g4_status_fails_closed",
)


def _text() -> str:
    return BASELINE_FILE.read_text(encoding="utf-8")


def test_packaged_baseline_loads_after_digest_verification() -> None:
    baseline = load_acceptance_baseline("1")

    assert baseline.baseline_version == "1"
    assert baseline.calibration_categories == {"caught", "escaped", "not_injected", "inconclusive"}
    assert baseline.scenario_custody.storage == "outside_worker_readable_mounts"
    assert baseline.scenario_custody.public_content == "identities_only"


def test_tampered_baseline_bytes_fail_the_digest_check() -> None:
    tampered = _text().replace('id = "doctor.prerequisites_ok"', 'id = "doctor.weakened"')

    with pytest.raises(AcceptanceBaselineError, match="registered digest"):
        parse_acceptance_baseline(tampered.encode("utf-8"), version="1")


def test_even_trailing_whitespace_fails_the_digest_check() -> None:
    with pytest.raises(AcceptanceBaselineError, match="registered digest"):
        parse_acceptance_baseline(BASELINE_FILE.read_bytes() + b"\n", version="1")


def test_an_unregistered_version_is_rejected() -> None:
    with pytest.raises(AcceptanceBaselineError, match="not registered"):
        parse_acceptance_baseline(BASELINE_FILE.read_bytes(), version="2")
    with pytest.raises(AcceptanceBaselineError):
        load_acceptance_baseline("2")


def test_v1_journeys_are_exactly_the_frozen_set() -> None:
    baseline = load_acceptance_baseline()

    assert tuple(journey.journey_id for journey in baseline.journeys) == V1_JOURNEY_IDS


def test_every_baseline_check_is_a_fully_typed_contract() -> None:
    baseline = load_acceptance_baseline()
    checks = [a.check for journey in baseline.journeys for a in journey.assertions]

    assert {check.check_id for check in checks} == {
        "stage1.validate",
        "stage1.verify",
        "stage2.release_gate",
        "stage3.rollback_gate",
        "doctor.prerequisites",
        "gate.g4_fail_closed",
    }
    for check in checks:
        assert (check.command is None) != (check.observer is None), check.check_id
        assert check.on_fail in {CheckDisposition.STOP, CheckDisposition.REPAIR_OR_STOP}
        assert check.on_indeterminate in {CheckDisposition.STOP, CheckDisposition.HOLD}
    by_id = {check.check_id: check for check in checks}
    stage2 = by_id["stage2.release_gate"].command
    assert stage2 is not None
    assert stage2.source is CommandSource.DECLARED
    assert stage2.argv[-2:] == ("--stages", "2")
    assert by_id["gate.g4_fail_closed"].observer is CheckObserver.G4_GATE_FAIL_CLOSED


def test_stage1_manifest_checks_match_the_baseline_declarations() -> None:
    baseline = load_acceptance_baseline()

    baseline.require_manifest_checks(stage1_work_manifest())

    assert {STAGE1_VALIDATE_CHECK_ID, STAGE1_VERIFY_CHECK_ID} <= baseline.check_ids


def test_a_baseline_missing_a_bound_stage1_check_is_rejected() -> None:
    renamed = _text().replace('id = "stage1.verify"', 'id = "stage1.other"')
    baseline = parse_baseline_document(renamed)

    with pytest.raises(AcceptanceBaselineError, match="does not reference"):
        baseline.require_manifest_checks(stage1_work_manifest())


def test_a_manifest_check_that_differs_from_the_baseline_declaration_is_rejected() -> None:
    weakened = _text().replace(
        'observer = "pull_request_merge_readiness"\non_fail = "stop"\non_indeterminate = "hold"',
        'observer = "pull_request_merge_readiness"\non_fail = "stop"\non_indeterminate = "stop"',
    )
    baseline = parse_baseline_document(weakened)

    with pytest.raises(AcceptanceBaselineError, match="differently"):
        baseline.require_manifest_checks(stage1_work_manifest())


def test_an_assertion_without_a_valid_typed_check_is_rejected() -> None:
    both = _text().replace(
        'observer = "g4_gate_fail_closed"',
        'observer = "g4_gate_fail_closed"\ncommand = { source = "request" }',
    )
    with pytest.raises(AcceptanceBaselineError, match="exactly one"):
        parse_baseline_document(both)

    shell = _text().replace(
        'argv = ["uv", "run", "enginery", "doctor"]', 'argv = ["bash", "-c", "enginery doctor"]'
    )
    with pytest.raises(AcceptanceBaselineError, match="shell"):
        parse_baseline_document(shell)

    unknown = _text().replace('subject = "workspace_head"', 'subject = "workspace_head"\nx = 1')
    with pytest.raises(AcceptanceBaselineError, match="unknown keys"):
        parse_baseline_document(unknown)


def test_duplicate_assertion_ids_are_rejected() -> None:
    text = _text().replace(
        'id = "stage2.slices_merged_and_release_published"',
        'id = "stage1.validation_passes_at_workspace_head"',
    )

    with pytest.raises(AcceptanceBaselineError, match="assertion ids must be unique"):
        parse_baseline_document(text)


def test_duplicate_journey_ids_are_rejected() -> None:
    text = _text().replace(
        'id = "stage2_plan_to_verified_release"',
        'id = "stage1_issue_to_merge_ready_pull_request"',
    )

    with pytest.raises(AcceptanceBaselineError, match="journey ids must be unique"):
        parse_baseline_document(text)


def test_duplicate_check_ids_are_rejected() -> None:
    text = _text().replace('id = "stage2.release_gate"', 'id = "stage1.validate"')

    with pytest.raises(AcceptanceBaselineError, match="check ids must be unique"):
        parse_baseline_document(text)


@pytest.mark.parametrize(
    "categories",
    [
        '["caught", "escaped", "not_injected"]',
        '["caught", "escaped", "not_injected", "inconclusive", "missed"]',
        '["caught", "escaped", "not_injected", "unknown"]',
        '["caught", "caught", "escaped", "not_injected", "inconclusive"]',
    ],
)
def test_calibration_categories_must_be_exactly_the_four_defined(categories: str) -> None:
    text = _text().replace(
        'categories = ["caught", "escaped", "not_injected", "inconclusive"]',
        f"categories = {categories}",
    )

    with pytest.raises(AcceptanceBaselineError, match="calibration categories"):
        parse_baseline_document(text)


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(AcceptanceBaselineError, match="unknown keys"):
        parse_baseline_document(_text() + '\nscenario_content = "private"\n')


def test_scenario_custody_cannot_move_scenarios_into_the_public_file() -> None:
    text = _text().replace(
        'storage = "outside_worker_readable_mounts"', 'storage = "worker_readable_repository"'
    )

    with pytest.raises(AcceptanceBaselineError, match="scenario custody"):
        parse_baseline_document(text)


def test_every_journey_names_an_existing_script_or_test() -> None:
    baseline = load_acceptance_baseline()

    for journey in baseline.journeys:
        for reference in journey.exercised_by:
            target = reference.split(" ", 1)[0]
            path_text, _, test_name = target.partition("::")
            path = REPO_ROOT / path_text
            assert path.is_file(), f"{journey.journey_id}: {path_text} does not exist"
            if test_name:
                defined = {
                    node.name
                    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                    if isinstance(node, ast.FunctionDef)
                }
                assert test_name in defined, f"{journey.journey_id}: {reference} is not defined"
