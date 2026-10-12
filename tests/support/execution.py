"""Execution-selection builders shared by tests that construct tasks and requests."""

from __future__ import annotations

from enginery.application.execution_catalog import load_execution_catalog
from enginery.domain.execution_selection import (
    ExecutionSelection,
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)


def selection_for(
    harness: str = "omp",
    *,
    model: str | None = None,
    effort: str | None = None,
    strict_effective: bool | None = None,
) -> ExecutionSelection:
    """Resolve a run-layer selection against the packaged catalog; pins when given."""
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
        catalog=load_execution_catalog(),
    )
