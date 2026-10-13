from __future__ import annotations

from datetime import UTC, datetime, timedelta

from enginery.engine.scheduler import (
    NodeKey,
    ReadinessScheduler,
    SchedulableNode,
    SchedulableState,
    SchedulingLimits,
)

_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _node(
    run_id: str,
    node_id: str,
    *,
    dependencies: tuple[NodeKey, ...] = (),
    state: SchedulableState = SchedulableState.QUEUED,
    repository_id: str | None = None,
    queued_since: datetime | None = None,
) -> SchedulableNode:
    return SchedulableNode(
        key=NodeKey(run_id, node_id),
        dependencies=dependencies,
        state=state,
        repository_id=repository_id,
        queued_since=queued_since,
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
    )

    assert plan.selected == (
        first_run_a.key,
        first_run_b.key,
        second_run_a.key,
        second_run_b.key,
    )


def test_the_oldest_queued_run_is_granted_capacity_first_whatever_its_run_id() -> None:
    older_but_higher_id = _node("run-z", "work", queued_since=_AT)
    newer_but_lower_id = _node("run-a", "work", queued_since=_AT + timedelta(seconds=5))
    untimed = _node("run-0", "work")

    one_slot = ReadinessScheduler().plan(
        (newer_but_lower_id, untimed, older_but_higher_id), limits=SchedulingLimits(1, 1)
    )
    two_slots = ReadinessScheduler().plan(
        (newer_but_lower_id, untimed, older_but_higher_id), limits=SchedulingLimits(2, 2)
    )

    assert one_slot.selected == (older_but_higher_id.key,)
    assert two_slots.selected == (older_but_higher_id.key, newer_but_lower_id.key)


def test_equal_queue_times_fall_back_to_run_id_order() -> None:
    second = _node("run-b", "work", queued_since=_AT)
    first = _node("run-a", "work", queued_since=_AT)

    plan = ReadinessScheduler().plan((second, first), limits=SchedulingLimits(2, 2))

    assert plan.selected == (first.key, second.key)


def test_human_wait_does_not_consume_concurrency() -> None:
    human_wait = _node("run-1", "approve", state=SchedulableState.AWAITING_HUMAN)
    ready = _node("run-2", "execute")

    plan = ReadinessScheduler().plan((human_wait, ready), limits=SchedulingLimits(1, 1))

    assert plan.selected == (ready.key,)


def test_every_unselected_queued_node_reports_its_single_blocking_reason() -> None:
    active = _node("run-0", "active", state=SchedulableState.RUNNING, repository_id="repo-a")
    capacity = _node("run-1", "capacity", repository_id="repo-b")
    repository = _node("run-2", "repository", repository_id="repo-a")
    prerequisite = _node("run-3", "prepare", state=SchedulableState.RUNNING, repository_id="repo-c")
    waiting = _node("run-3", "execute", dependencies=(prerequisite.key,), repository_id="repo-c")
    failed = _node("run-4", "build", state=SchedulableState.FAILED)
    orphaned = _node("run-4", "ship", dependencies=(failed.key,))

    plan = ReadinessScheduler().plan(
        (active, capacity, repository, prerequisite, waiting, failed, orphaned),
        limits=SchedulingLimits(2, 1),
    )

    assert plan.selected == ()
    assert {deferred.key: deferred.reason.value for deferred in plan.deferred} == {
        capacity.key: "global_capacity",
        repository.key: "repository_capacity",
        waiting.key: "dependencies",
        orphaned.key: "dependency_failed",
    }


def test_repository_limit_is_reported_when_global_capacity_remains() -> None:
    active = _node("run-0", "active", state=SchedulableState.RUNNING, repository_id="repo-a")
    blocked = _node("run-1", "work", repository_id="repo-a")
    other = _node("run-2", "work", repository_id="repo-b")

    plan = ReadinessScheduler().plan((active, blocked, other), limits=SchedulingLimits(4, 1))

    assert plan.selected == (other.key,)
    assert [(item.key, item.reason.value) for item in plan.deferred] == [
        (blocked.key, "repository_capacity")
    ]


def test_a_node_released_by_its_dependency_is_selected_with_no_deferral() -> None:
    done = _node("run-1", "prepare", state=SchedulableState.SUCCEEDED)
    released = _node("run-1", "execute", dependencies=(done.key,))

    plan = ReadinessScheduler().plan((done, released), limits=SchedulingLimits(1, 1))

    assert plan.selected == (released.key,)
    assert plan.deferred == ()
