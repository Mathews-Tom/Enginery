from __future__ import annotations

from dataclasses import replace

import pytest

from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError
from enginery.domain.execution_catalog import SupportTier
from enginery.domain.execution_selection import (
    ExecutionSelection,
    HoldReason,
    LayerControls,
    RequestedControl,
    SelectionLayer,
    SelectionMode,
    detect_catalog_drift,
    resolve_execution_selection,
    validate_against_catalog,
)

CATALOG = load_execution_catalog()
OMP_MODEL = "openai/gpt-5.2"
OMP_OTHER_MODEL = "anthropic/claude-sonnet-5-5"
CLAUDE_MODEL = "claude-opus-4-8"


def _resolve(*layers: LayerControls) -> ExecutionSelection:
    return resolve_execution_selection(layers, catalog=CATALOG)


def test_an_unspecified_run_is_a_host_default_that_names_no_model_or_effort() -> None:
    selection = _resolve(LayerControls(layer=SelectionLayer.RUN, harness="omp"))

    assert selection.mode is SelectionMode.HOST_DEFAULT
    assert selection.selected_model is None
    assert selection.selected_effort is None
    assert selection.model.source is None
    assert not selection.requires_native_controls
    assert selection.harness_source is SelectionLayer.RUN
    assert selection.catalog_digest == CATALOG.digest
    assert selection.support_tier is SupportTier.EXPERIMENTAL


def test_unpinned_layers_resolve_by_precedence_run_project_user_workflow_factory() -> None:
    layers = [
        LayerControls(layer=SelectionLayer.FACTORY, harness="omp", model="factory/model"),
        LayerControls(layer=SelectionLayer.WORKFLOW, model="workflow/model", effort="low"),
        LayerControls(layer=SelectionLayer.USER, model="user/model", effort="medium"),
        LayerControls(layer=SelectionLayer.PROJECT, model="project/model"),
    ]
    assert _resolve(*layers).selected_model == "project/model"
    assert _resolve(*layers).model.source is SelectionLayer.PROJECT
    assert _resolve(*layers).selected_effort == "medium"
    assert _resolve(*layers).effort.source is SelectionLayer.USER

    with_run = [*layers, LayerControls(layer=SelectionLayer.RUN, model="run/model")]
    selection = _resolve(*with_run)
    assert selection.selected_model == "run/model"
    assert selection.model.source is SelectionLayer.RUN
    assert selection.mode is SelectionMode.CONFIGURED_DEFAULT


def test_a_lower_pin_never_beats_a_higher_explicit_value_and_the_conflict_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match=r"user layer pins model .* run layer sets"):
        _resolve(
            LayerControls(
                layer=SelectionLayer.USER, harness="omp", model=OMP_MODEL, model_pinned=True
            ),
            LayerControls(layer=SelectionLayer.RUN, model=OMP_OTHER_MODEL),
        )
    with pytest.raises(InvalidInputError, match="project layer pins model"):
        _resolve(
            LayerControls(
                layer=SelectionLayer.PROJECT,
                harness="omp",
                model=OMP_OTHER_MODEL,
                model_pinned=True,
            ),
            LayerControls(layer=SelectionLayer.RUN, model=OMP_MODEL, model_pinned=True),
        )


def test_a_pin_is_exact_at_the_winning_layer_and_a_lower_agreeing_pin_confirms_it() -> None:
    winner = _resolve(
        LayerControls(layer=SelectionLayer.USER, harness="omp", model=OMP_MODEL, model_pinned=True)
    )
    confirmed = _resolve(
        LayerControls(layer=SelectionLayer.USER, harness="omp", model=OMP_MODEL, model_pinned=True),
        LayerControls(layer=SelectionLayer.RUN, model=OMP_MODEL),
    )
    unpinned = _resolve(
        LayerControls(layer=SelectionLayer.USER, harness="omp", model=OMP_MODEL),
        LayerControls(layer=SelectionLayer.RUN, model=OMP_OTHER_MODEL),
    )

    assert winner.model.pinned and winner.mode is SelectionMode.MANUAL_PIN
    assert confirmed.model.pinned and confirmed.model.source is SelectionLayer.RUN
    assert unpinned.selected_model == OMP_OTHER_MODEL and not unpinned.model.pinned


def test_a_pin_from_a_layer_naming_another_harness_is_rejected_not_carried_across() -> None:
    with pytest.raises(InvalidInputError, match="user layer pins the effort for harness 'omp'"):
        _resolve(
            LayerControls(
                layer=SelectionLayer.USER, harness="omp", effort="high", effort_pinned=True
            ),
            LayerControls(layer=SelectionLayer.RUN, harness="claude-code"),
        )
    with pytest.raises(InvalidInputError, match="project layer pins the model for harness"):
        _resolve(
            LayerControls(
                layer=SelectionLayer.PROJECT,
                harness="claude-code",
                model=CLAUDE_MODEL,
                model_pinned=True,
            ),
            LayerControls(layer=SelectionLayer.RUN, harness="omp"),
        )


def test_unpinned_values_and_constraints_from_another_harnesss_layer_are_ignored() -> None:
    selection = _resolve(
        LayerControls(
            layer=SelectionLayer.USER,
            harness="omp",
            model=OMP_MODEL,
            effort="high",
            allowed_models=(OMP_MODEL,),
            allowed_efforts=("high",),
        ),
        LayerControls(layer=SelectionLayer.RUN, harness="claude-code", effort="max"),
    )

    assert selection.harness == "claude-code"
    assert selection.selected_effort == "max"
    assert selection.selected_model is None


def test_strict_effective_combines_with_or_across_eligible_layers() -> None:
    layers = (
        LayerControls(layer=SelectionLayer.USER, harness="omp", strict_effective=True),
        LayerControls(
            layer=SelectionLayer.RUN, model=OMP_MODEL, model_pinned=True, strict_effective=False
        ),
    )

    assert _resolve(*layers).strict_effective


def test_strict_effective_from_a_layer_naming_another_harness_is_rejected_not_carried() -> None:
    with pytest.raises(InvalidInputError, match="user layer requires strict effective controls"):
        _resolve(
            LayerControls(layer=SelectionLayer.USER, harness="omp", strict_effective=True),
            LayerControls(layer=SelectionLayer.RUN, harness="claude-code", model=CLAUDE_MODEL),
        )
    relaxed = _resolve(
        LayerControls(layer=SelectionLayer.USER, harness="omp", strict_effective=False),
        LayerControls(layer=SelectionLayer.RUN, harness="claude-code", model=CLAUDE_MODEL),
    )
    assert not relaxed.strict_effective


@pytest.mark.parametrize(
    ("harness", "model", "effort"),
    [("omp", OMP_MODEL, "high"), ("claude-code", CLAUDE_MODEL, "high")],
)
def test_a_strict_effort_pin_is_rejected_because_no_host_reports_effective_effort(
    harness: str, model: str, effort: str
) -> None:
    with pytest.raises(InvalidInputError, match="does not report its effective effort"):
        _resolve(
            LayerControls(
                layer=SelectionLayer.RUN,
                harness=harness,
                model=model,
                model_pinned=True,
                effort=effort,
                effort_pinned=True,
                strict_effective=True,
            )
        )
    allowed = _resolve(
        LayerControls(
            layer=SelectionLayer.RUN,
            harness=harness,
            model=model,
            model_pinned=True,
            strict_effective=True,
        )
    )
    assert allowed.strict_effective
    crafted = replace(
        _resolve(
            LayerControls(
                layer=SelectionLayer.RUN,
                harness=harness,
                effort=effort,
                effort_pinned=True,
            )
        ),
        strict_effective=True,
    )
    with pytest.raises(InvalidInputError, match="does not report its effective effort"):
        validate_against_catalog(crafted, catalog=CATALOG)


def test_a_tampered_selection_is_rejected_by_catalog_validation() -> None:
    good = _resolve(
        LayerControls(layer=SelectionLayer.RUN, harness="omp", model=OMP_MODEL, model_pinned=True)
    )
    assert validate_against_catalog(good, catalog=CATALOG).harness == "omp"
    alias = replace(
        good,
        model=RequestedControl(value="opus", source=SelectionLayer.RUN, pinned=True),
        selected_model="opus",
    )
    auto = replace(
        good,
        effort=RequestedControl(value="auto", source=SelectionLayer.RUN, pinned=True),
        selected_effort="auto",
    )
    for tampered, match in (
        (alias, "not an exact omp model pin"),
        (auto, "cannot be pinned"),
        (replace(good, support_tier=SupportTier.QUALIFIED), "tested host entry"),
        (replace(good, host_executable_version="9.9.9"), "tested host entry"),
        (replace(good, host_protocol_fingerprint="json-protocol=9"), "tested host entry"),
        (replace(good, catalog_version="9"), "different catalog"),
        (replace(good, catalog_digest=Digest.of_bytes(b"x")), "different catalog"),
    ):
        with pytest.raises(InvalidInputError, match=match):
            validate_against_catalog(tampered, catalog=CATALOG)
    with pytest.raises(InvalidInputError, match="names no catalog"):
        validate_against_catalog(ExecutionSelection.legacy_host_default("omp"), catalog=CATALOG)


def test_a_selection_cannot_replace_a_pinned_control_with_a_different_selected_value() -> None:
    selection = _resolve(
        LayerControls(
            layer=SelectionLayer.RUN,
            harness="omp",
            model=OMP_MODEL,
            model_pinned=True,
            effort="high",
            effort_pinned=True,
        )
    )

    with pytest.raises(InvalidInputError, match="pinned model is exact"):
        replace(selection, selected_model=OMP_OTHER_MODEL)
    with pytest.raises(InvalidInputError, match="pinned effort is exact"):
        replace(selection, selected_effort="low")
    with pytest.raises(InvalidInputError, match="pinned model is exact"):
        replace(selection, selected_model=None)


def test_hard_constraints_at_any_layer_reject_a_violating_pin_instead_of_replacing_it() -> None:
    with pytest.raises(InvalidInputError, match="violates the factory layer constraint"):
        _resolve(
            LayerControls(
                layer=SelectionLayer.FACTORY, harness="omp", allowed_models=(OMP_OTHER_MODEL,)
            ),
            LayerControls(layer=SelectionLayer.RUN, model=OMP_MODEL, model_pinned=True),
        )
    with pytest.raises(InvalidInputError, match="violates the user layer constraint"):
        _resolve(
            LayerControls(
                layer=SelectionLayer.RUN, harness="omp", effort="max", effort_pinned=True
            ),
            LayerControls(layer=SelectionLayer.USER, allowed_efforts=("low", "medium")),
        )
    with pytest.raises(InvalidInputError, match="harness 'omp' violates"):
        _resolve(
            LayerControls(layer=SelectionLayer.RUN, harness="omp"),
            LayerControls(layer=SelectionLayer.PROJECT, allowed_harnesses=("claude-code",)),
        )


def test_a_constrained_control_left_to_the_host_default_is_rejected() -> None:
    with pytest.raises(InvalidInputError, match="explicit model is required"):
        _resolve(
            LayerControls(layer=SelectionLayer.RUN, harness="omp"),
            LayerControls(layer=SelectionLayer.PROJECT, allowed_models=(OMP_MODEL,)),
        )


def test_constraints_accept_a_value_every_layer_allows() -> None:
    selection = _resolve(
        LayerControls(layer=SelectionLayer.RUN, harness="omp", model=OMP_MODEL, model_pinned=True),
        LayerControls(layer=SelectionLayer.PROJECT, allowed_models=(OMP_MODEL, OMP_OTHER_MODEL)),
        LayerControls(layer=SelectionLayer.USER, allowed_models=(OMP_MODEL,)),
    )

    assert selection.selected_model == OMP_MODEL


@pytest.mark.parametrize(
    ("harness", "model", "match"),
    [
        ("omp", "opus", "not an exact omp model pin"),
        ("omp", "gpt-5.2", "not an exact omp model pin"),
        ("claude-code", "opus", "not an exact claude-code model pin"),
        ("claude-code", "sonnet", "not an exact claude-code model pin"),
        ("claude-code", "claude-opus", "not an exact claude-code model pin"),
        ("claude-code", "openai/gpt-5.2", "not an exact claude-code model pin"),
    ],
)
def test_aliases_and_bare_names_are_not_exact_model_pins(
    harness: str, model: str, match: str
) -> None:
    with pytest.raises(InvalidInputError, match=match):
        _resolve(
            LayerControls(layer=SelectionLayer.RUN, harness=harness, model=model, model_pinned=True)
        )


@pytest.mark.parametrize(
    ("harness", "effort", "match"),
    [
        ("omp", "ultra", "not a supported omp effort level"),
        ("omp", "auto", "cannot be pinned"),
        ("claude-code", "minimal", "not a supported claude-code effort level"),
        ("claude-code", "off", "not a supported claude-code effort level"),
        ("claude-code", "auto", "not a supported claude-code effort level"),
    ],
)
def test_effort_levels_are_the_hosts_own_pinnable_vocabulary(
    harness: str, effort: str, match: str
) -> None:
    with pytest.raises(InvalidInputError, match=match):
        _resolve(
            LayerControls(
                layer=SelectionLayer.RUN, harness=harness, effort=effort, effort_pinned=True
            )
        )


def test_each_host_accepts_its_own_exact_forms_and_levels() -> None:
    omp = _resolve(
        LayerControls(
            layer=SelectionLayer.RUN,
            harness="omp",
            model=OMP_MODEL,
            model_pinned=True,
            effort="minimal",
            effort_pinned=True,
        )
    )
    claude = _resolve(
        LayerControls(
            layer=SelectionLayer.RUN,
            harness="claude-code",
            model=CLAUDE_MODEL,
            model_pinned=True,
            effort="xhigh",
            effort_pinned=True,
        )
    )

    assert (omp.selected_model, omp.selected_effort) == (OMP_MODEL, "minimal")
    assert (claude.selected_model, claude.selected_effort) == (CLAUDE_MODEL, "xhigh")
    assert omp.host_protocol_fingerprint == "json-protocol=3"
    assert claude.host_protocol_fingerprint == "stream-json-schema=1"


def test_an_unknown_harness_and_a_missing_harness_are_rejected() -> None:
    with pytest.raises(InvalidInputError, match="not in the execution catalog"):
        _resolve(LayerControls(layer=SelectionLayer.RUN, harness="gemini-cli"))
    with pytest.raises(InvalidInputError, match="no layer selected a harness"):
        _resolve(LayerControls(layer=SelectionLayer.RUN, model=OMP_MODEL))


def test_a_catalog_marking_the_host_unsupported_rejects_every_selection() -> None:
    omp = CATALOG.host("omp")
    unsupported = replace(
        CATALOG,
        hosts=(replace(omp, support_tier=SupportTier.UNSUPPORTED), CATALOG.host("claude-code")),
    )

    with pytest.raises(MissingPrerequisiteError, match="unsupported"):
        resolve_execution_selection(
            (LayerControls(layer=SelectionLayer.RUN, harness="omp"),), catalog=unsupported
        )


def test_a_layer_cannot_be_supplied_twice_or_pin_what_it_does_not_specify() -> None:
    with pytest.raises(InvalidInputError, match="supplied more than once"):
        _resolve(
            LayerControls(layer=SelectionLayer.RUN, harness="omp"),
            LayerControls(layer=SelectionLayer.RUN, model=OMP_MODEL),
        )
    with pytest.raises(InvalidInputError, match="pins a model it does not specify"):
        LayerControls(layer=SelectionLayer.RUN, model_pinned=True)


def test_strict_effective_requires_a_selected_control() -> None:
    with pytest.raises(InvalidInputError, match="needs at least one selected control"):
        _resolve(LayerControls(layer=SelectionLayer.RUN, harness="omp", strict_effective=True))

    strict = _resolve(
        LayerControls(
            layer=SelectionLayer.RUN,
            harness="omp",
            model=OMP_MODEL,
            model_pinned=True,
            strict_effective=True,
        )
    )
    assert strict.strict_effective


def test_the_selection_digest_binds_controls_sources_and_catalog() -> None:
    base = _resolve(
        LayerControls(layer=SelectionLayer.RUN, harness="omp", model=OMP_MODEL, model_pinned=True)
    )
    same = _resolve(
        LayerControls(layer=SelectionLayer.RUN, harness="omp", model=OMP_MODEL, model_pinned=True)
    )
    other_model = _resolve(
        LayerControls(
            layer=SelectionLayer.RUN, harness="omp", model=OMP_OTHER_MODEL, model_pinned=True
        )
    )
    other_source = _resolve(
        LayerControls(layer=SelectionLayer.USER, harness="omp", model=OMP_MODEL, model_pinned=True)
    )
    other_effort = _resolve(
        LayerControls(
            layer=SelectionLayer.RUN,
            harness="omp",
            model=OMP_MODEL,
            model_pinned=True,
            effort="high",
            effort_pinned=True,
        )
    )
    other_catalog = replace(base, catalog_digest=Digest.of_bytes(b"another catalog"))

    assert base.digest == same.digest
    digests = {
        base.digest,
        other_model.digest,
        other_source.digest,
        other_effort.digest,
        other_catalog.digest,
    }
    assert len(digests) == 5


def test_the_selection_round_trips_and_rejects_an_unknown_schema_version() -> None:
    selection = _resolve(
        LayerControls(
            layer=SelectionLayer.RUN,
            harness="claude-code",
            model=CLAUDE_MODEL,
            model_pinned=True,
            effort="high",
            effort_pinned=True,
        )
    )
    selection = replace(selection, strict_effective=True)

    assert ExecutionSelection.from_state(selection.to_state()) == selection

    state = selection.to_state()
    state["schema_version"] = 2
    with pytest.raises(InvalidInputError, match="schema version is unsupported"):
        ExecutionSelection.from_state(state)


def test_a_legacy_binding_is_a_bare_host_default_with_no_catalog_identity() -> None:
    legacy = ExecutionSelection.legacy_host_default("omp")

    assert legacy.legacy
    assert legacy.mode is SelectionMode.HOST_DEFAULT
    assert legacy.catalog_digest is None
    assert legacy.support_tier is None
    assert not legacy.requires_native_controls
    with pytest.raises(InvalidInputError, match="bare host default"):
        replace(legacy, strict_effective=True)
    with pytest.raises(InvalidInputError, match="must equal the requested"):
        replace(legacy, selected_model=OMP_MODEL)


def test_a_bound_selection_must_name_its_catalog_and_a_consistent_mode() -> None:
    selection = _resolve(LayerControls(layer=SelectionLayer.RUN, harness="omp"))

    with pytest.raises(InvalidInputError, match="catalog identity"):
        replace(selection, catalog_digest=None)
    with pytest.raises(InvalidInputError, match="mode does not match"):
        replace(selection, mode=SelectionMode.MANUAL_PIN)


def test_a_requested_control_must_name_its_source_and_a_missing_one_has_none() -> None:
    with pytest.raises(InvalidInputError, match="source layer"):
        RequestedControl(value=OMP_MODEL, source=None, pinned=False)
    with pytest.raises(InvalidInputError, match="cannot carry a source or a pin"):
        RequestedControl(value=None, source=SelectionLayer.RUN, pinned=False)


def test_a_changed_catalog_holds_a_bound_selection_without_rebinding_it() -> None:
    selection = _resolve(
        LayerControls(layer=SelectionLayer.RUN, harness="omp", model=OMP_MODEL, model_pinned=True)
    )
    drifted = replace(CATALOG, digest=Digest.of_bytes(b"a later catalog"), version="3")

    assert detect_catalog_drift(selection, current=CATALOG) is None
    hold = detect_catalog_drift(selection, current=drifted)

    assert hold is not None
    assert hold.reason is HoldReason.CATALOG_DRIFT
    assert hold.details["bound_catalog_digest"] == str(CATALOG.digest)
    assert hold.details["current_catalog_digest"] == str(drifted.digest)
    assert selection.catalog_digest == CATALOG.digest
    assert selection.selected_model == OMP_MODEL


def test_a_legacy_binding_never_drifts() -> None:
    drifted = replace(CATALOG, digest=Digest.of_bytes(b"a later catalog"), version="3")

    assert (
        detect_catalog_drift(ExecutionSelection.legacy_host_default("omp"), current=drifted) is None
    )
