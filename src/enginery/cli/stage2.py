"""``enginery stage2``: read-only inspection of one plan's Stage 2 stack.

Interactive merge/prepare/build/publish orchestration for a live Stage 2
run is driven directly through ``Stage2ReleaseWorkflow`` (see
``scripts/run_stage2_gate.py``), matching the same accepted pattern
Stage 1's own pilot already established: constructing a full lifecycle
run request through a CLI flag surface is deferred, and the CLI's job
here is safe operator inspection, not orchestration.
"""

from __future__ import annotations

import argparse
import json

from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import StackId
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.stack_coordinator import StackCoordinator
from enginery.ledger.service import LedgerService


def run_stage2(args: argparse.Namespace) -> int:
    """Run one Stage 2 command and emit a machine-readable result."""
    command = args.stage2_command
    if command is None:
        raise InvalidInputError("stage2 requires a subcommand")
    if command == "status":
        _status(args)
    else:  # pragma: no cover - argparse restricts command values
        raise AssertionError(f"unhandled Stage 2 command: {command}")
    return 0


def _status(args: argparse.Namespace) -> None:
    ledger = LedgerService.open(args.database)
    try:
        coordinator = StackCoordinator(ledger, CoordinatorRuntime(ledger, owner=args.owner))
        stack = coordinator.read(StackId(args.stack_id))
        if stack is None:
            _print({"stack_id": args.stack_id, "found": False})
            return
        next_mergeable = stack.next_mergeable()
        _print(
            {
                "stack_id": args.stack_id,
                "found": True,
                "plan_id": str(stack.plan_id),
                "base_ref": stack.base_ref,
                "next_mergeable": str(next_mergeable) if next_mergeable is not None else None,
                "slices": [
                    {
                        "milestone_id": str(slice_.milestone_id),
                        "position": slice_.position,
                        "branch_ref": slice_.branch_ref,
                        "state": slice_.state.value,
                        "head_revision": slice_.head_revision,
                    }
                    for slice_ in stack.ordered_slices
                ],
            }
        )
    finally:
        ledger.close()


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


__all__ = ["run_stage2"]
