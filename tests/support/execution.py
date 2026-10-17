"""Execution-selection builders shared by tests that construct tasks and requests."""

from __future__ import annotations

from dataclasses import replace

from enginery.application.effective_config import EffectiveConfig
from enginery.application.execution_catalog import load_execution_catalog
from enginery.cli.config_binding import EFFECTIVE_CONFIG_KEY, request_values
from enginery.domain.execution_catalog import ExecutionCatalog
from enginery.domain.execution_selection import (
    ExecutionSelection,
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)
from enginery.workflows.stage1 import Stage1RunRequest


def selection_for(
    harness: str = "omp",
    *,
    model: str | None = None,
    effort: str | None = None,
    strict_effective: bool | None = None,
    catalog: ExecutionCatalog | None = None,
) -> ExecutionSelection:
    """Resolve a run-layer selection against a catalog (packaged by default); pins when given."""
    return resolve_execution_selection(
        (
            LayerControls(
                layer=SelectionLayer.RUN,
                harness=harness,
                model=model,
                effort=effort,
                model_pinned=model is not None,
                effort_pinned=effort is not None,
                strict_effective=strict_effective,
            ),
        ),
        catalog=load_execution_catalog() if catalog is None else catalog,
    )


def bound_document(request: Stage1RunRequest) -> dict[str, object]:
    """The ``--request`` document ``engine start`` accepts for a hand-constructed request.

    A library-built request is not compiled, so it carries no configuration snapshot of its
    own. This binds one to the values the request actually carries (every field attributed to
    the run layer) and rebinds the request's configuration digest to that snapshot.
    """
    selection = request.execution_selection

    def entry(value: object) -> dict[str, object]:
        return {
            "layer": "run",
            "value": value,
            "source": "test fixture",
            "source_digest": None,
            "pinned": False,
            "note": None,
        }

    values = {**request_values(request), "profile": "standard"}
    state: dict[str, object] = {
        "schema_version": 1,
        "catalog": {
            "version": selection.catalog_version,
            "digest": str(selection.catalog_digest),
        },
        "selection_digest": str(selection.digest),
        "sources": [],
        "constraints": [],
        "fields": {
            name: {"value": value, "winner": "run", "chain": [entry(value)]}
            for name, value in values.items()
        },
    }
    if request.behavioral_checks is not None:
        state["acceptance"] = {"checks": [check.to_state() for check in request.behavioral_checks]}
    config = EffectiveConfig.from_state(state)
    bound = replace(
        request,
        run=replace(request.run, configuration_snapshot_digest=config.digest),
    )
    return {**bound.initial_state(), EFFECTIVE_CONFIG_KEY: state}
