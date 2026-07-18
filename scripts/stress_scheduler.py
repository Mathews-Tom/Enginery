#!/usr/bin/env python3
"""Exercise deterministic scheduler fairness and repository bounds."""

from __future__ import annotations

import argparse

from enginery.engine.scheduler import NodeKey, ReadinessScheduler, SchedulableNode, SchedulingLimits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    args = parser.parse_args()
    nodes = tuple(
        SchedulableNode(NodeKey(f"run-{index}", "node"), repository_id=f"repo-{index}")
        for index in range(args.runs)
    )
    plan = ReadinessScheduler().plan(
        nodes,
        limits=SchedulingLimits(args.concurrency, 1),
    )
    duplicate_leases = len(plan.selected) - len(set(plan.selected))
    workspace_collisions = len(plan.selected) - len({key.run_id for key in plan.selected})
    print(
        f"runs={args.runs} concurrency={args.concurrency} selected={len(plan.selected)} "
        f"duplicate_leases={duplicate_leases} workspace_collisions={workspace_collisions}"
    )
    return 0 if duplicate_leases == 0 and workspace_collisions == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
