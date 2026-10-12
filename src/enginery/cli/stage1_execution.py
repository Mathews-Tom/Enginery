"""Execution-selection diagnostics for the Stage 1 lifecycle commands.

One report answers four separate questions about a run: what was requested (with the
layer that supplied it), what was selected, what was actually submitted to the host,
and what the host was observed to run. The four are never merged: an unreported
effective value is shown as unknown, never as the request.
"""

from __future__ import annotations

from collections.abc import Mapping

from enginery.domain.execution_receipt import ExecutionReceipt
from enginery.domain.execution_selection import ExecutionHold, ExecutionSelection, RequestedControl
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import Stage1Run

_HOST_DEFAULT = "host default"


def execution_report(
    ledger: LedgerService, run: Stage1Run, *, hold: ExecutionHold | None
) -> dict[str, object]:
    """Return requested, selected, submitted, and effective controls with tier and holds."""
    selection = run.request.execution_selection
    receipt, recorded_hold = _implementation_receipt(ledger, run)
    unique: list[ExecutionHold] = []
    for candidate in (hold, recorded_hold):
        if candidate is not None and candidate not in unique:
            unique.append(candidate)
    holds = [item.to_state() for item in unique]
    return {
        "binding": "legacy" if selection.legacy else "bound",
        "harness": selection.harness,
        "harness_source": None
        if selection.harness_source is None
        else selection.harness_source.value,
        "mode": selection.mode.value,
        "selection_digest": None if selection.legacy else str(selection.digest),
        "support_tier": None if selection.support_tier is None else selection.support_tier.value,
        "catalog": {
            "version": selection.catalog_version,
            "digest": None if selection.catalog_digest is None else str(selection.catalog_digest),
        },
        "tested_host": {
            "executable_version": selection.host_executable_version,
            "protocol_fingerprint": selection.host_protocol_fingerprint,
        },
        "strict_effective": selection.strict_effective,
        "requested": {
            "model": _requested(selection.model),
            "effort": _requested(selection.effort),
        },
        "selected": {
            "model": selection.selected_model or _HOST_DEFAULT,
            "effort": selection.selected_effort or _HOST_DEFAULT,
        },
        "submitted": None if receipt is None else list(receipt.submitted_arguments),
        "effective": _effective(selection, receipt),
        "holds": holds,
    }


def _requested(control: RequestedControl) -> dict[str, object]:
    return {
        "value": control.value or _HOST_DEFAULT,
        "source": None if control.source is None else control.source.value,
        "pinned": control.pinned,
    }


def _effective(
    selection: ExecutionSelection, receipt: ExecutionReceipt | None
) -> dict[str, object]:
    if receipt is None:
        reason = (
            "pre-selection run: effective controls are not recorded"
            if selection.legacy
            else "no execution receipt recorded yet"
        )
        return {
            "model": {"state": "unknown", "value": None, "source": reason},
            "effort": {"state": "unknown", "value": None, "source": reason},
        }
    return {
        "model": receipt.effective_model.to_state(),
        "effort": receipt.effective_effort.to_state(),
    }


def _implementation_receipt(
    ledger: LedgerService, run: Stage1Run
) -> tuple[ExecutionReceipt | None, ExecutionHold | None]:
    run_id = str(run.request.run.id)
    node = ledger.read_projection(aggregate_type="runtime_node", aggregate_id=f"{run_id}:implement")
    if node is None:
        return None, None
    attempt_id = node.state.get("attempt_id")
    if not isinstance(attempt_id, str):
        return None, None
    attempt = ledger.read_projection(aggregate_type="node_attempt", aggregate_id=attempt_id)
    result = None if attempt is None else attempt.state.get("result")
    if not isinstance(result, Mapping):
        return None, None
    raw_receipt = result.get("execution_receipt")
    raw_hold = result.get("execution_hold")
    return (
        None if raw_receipt is None else ExecutionReceipt.from_state(raw_receipt),
        None if raw_hold is None else ExecutionHold.from_state(raw_hold),
    )


__all__ = ["execution_report"]
