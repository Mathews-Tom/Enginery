from __future__ import annotations

from enginery.engine.scheduler import (
    NodeKey,
    ReadinessScheduler,
    SchedulableNode,
    SchedulableState,
    SchedulingLimits,
)


def _node(
    run_id: str,
    node_id: str,
    *,
    dependencies: tuple[NodeKey, ...] = (),
    state: SchedulableState = SchedulableState.QUEUED,
    repository_id: str | None = None,
) -> SchedulableNode:
    return SchedulableNode(
        key=NodeKey(run_id, node_id),
        dependencies=dependencies,
        state=state,
        repository_id=repository_id,
    )


def test_dependencies_must_succeed_before_node_is_ready() -> None:
    prerequisite = _node("run-1", "prepare")
    dependent = _node("run-1", "execute", dependencies=(prerequisite.key,))

    plan = ReadinessScheduler().plan((prerequisite, dependent), limits=SchedulingLimits(4, 2))

    assert plan.selected == (prerequisite.key,)


def test_scheduler_respects_global_and_repository_limits() -> None:
    active = _node("run-0", "active", state=SchedulableState.RUNNING, repository_id="repo-a")
    same_repository = _node("run-1", "same", repository_id="repo-a")
    other_repository = _node("run-2", "other", repository_id="repo-b")

    plan = ReadinessScheduler().plan(
        (active, same_repository, other_repository), limits=SchedulingLimits(2, 1)
    )

    assert plan.selected == (other_repository.key,)


def test_scheduler_round_robins_active_runs() -> None:
    first_run_a = _node("run-a", "a")
    second_run_a = _node("run-a", "b")
    first_run_b = _node("run-b", "a")
    second_run_b = _node("run-b", "b")

    plan = ReadinessScheduler().plan(
        (first_run_a, second_run_a, first_run_b, second_run_b),
        limits=SchedulingLimits(4, 4),
        last_run_id="run-a",
    )

    assert plan.selected == (
        first_run_b.key,
        first_run_a.key,
        second_run_b.key,
        second_run_a.key,
    )
    assert plan.next_run_id == "run-a"


def test_human_wait_does_not_consume_concurrency() -> None:
    human_wait = _node("run-1", "approve", state=SchedulableState.AWAITING_HUMAN)
    ready = _node("run-2", "execute")

    plan = ReadinessScheduler().plan((human_wait, ready), limits=SchedulingLimits(1, 1))

    assert plan.selected == (ready.key,)
