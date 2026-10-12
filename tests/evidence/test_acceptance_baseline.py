"""Behavior tests for the digest-frozen acceptance baseline."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

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


def test_stage1_manifest_checks_resolve_in_the_baseline() -> None:
    baseline = load_acceptance_baseline()

    baseline.require_manifest_checks(stage1_work_manifest())

    assert {STAGE1_VALIDATE_CHECK_ID, STAGE1_VERIFY_CHECK_ID} <= baseline.check_ids


def test_a_baseline_missing_a_bound_stage1_check_is_rejected() -> None:
    without_verify = _text().replace('check_id = "stage1.verify"', 'check_id = "stage1.other"')
    baseline = parse_baseline_document(without_verify)

    with pytest.raises(AcceptanceBaselineError, match="does not reference"):
        baseline.require_manifest_checks(stage1_work_manifest())


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
    text = _text().replace('check_id = "stage2.release_gate"', 'check_id = "stage1.validate"')

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


def test_baseline_lists_only_current_supported_behavior() -> None:
    baseline = load_acceptance_baseline()

    identities = {journey.journey_id for journey in baseline.journeys} | baseline.check_ids

    assert not any("factory" in identity for identity in identities)


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
