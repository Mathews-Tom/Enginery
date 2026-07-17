"""Invariant coverage summary: proves every designed state-machine edge from
03_SYSTEM_DESIGN.md §10 is exercised by a dedicated test, not merely sampled.

Each per-aggregate test module (``test_work_item.py``, ``test_run.py``,
``test_node_attempt.py``, ``test_factory_change.py``) parametrizes over the
*complete* set of edges transcribed from the design's transition tables.
This module makes that completeness claim independently checkable: it reads
the exact same ``TransitionTable`` the guarded ``transition_to`` methods
enforce and asserts the transcribed edge count matches the design, so a
transcription mistake (a dropped or duplicated edge) fails loudly here
instead of silently degrading a sampled test suite.
"""

from __future__ import annotations

from typing import Any

from enginery.domain.factory_change import FACTORY_CHANGE_TRANSITIONS
from enginery.domain.node_attempt import NODE_ATTEMPT_TRANSITIONS
from enginery.domain.run import RUN_TRANSITIONS
from enginery.domain.state_machine import TransitionTable
from enginery.domain.work_item import WORK_ITEM_TRANSITIONS


def _edge_count(table: TransitionTable[Any]) -> int:
    return sum(len(targets) for targets in table.edges.values())


def _edges(table: TransitionTable[Any]) -> set[tuple[object, object]]:
    return {(source, target) for source, targets in table.edges.items() for target in targets}


class TestDesignedEdgeCounts:
    """One assertion per state machine, matching the exact edge count
    transcribed by hand from 03_SYSTEM_DESIGN.md §10 in the PR description
    for m02/domain-03. A change to any table must update this constant
    deliberately, which is the point: it cannot drift silently."""

    def test_work_item_has_seventeen_designed_edges(self) -> None:
        assert _edge_count(WORK_ITEM_TRANSITIONS) == 17

    def test_run_has_forty_one_designed_edges(self) -> None:
        assert _edge_count(RUN_TRANSITIONS) == 41

    def test_node_attempt_has_twenty_two_designed_edges(self) -> None:
        assert _edge_count(NODE_ATTEMPT_TRANSITIONS) == 22

    def test_factory_change_has_thirteen_designed_edges(self) -> None:
        assert _edge_count(FACTORY_CHANGE_TRANSITIONS) == 13


class TestNoTransitionTableAllowsAnUndeclaredEdge:
    """For every (source, target) pair over each table's own state universe,
    ``allows`` is true iff the pair is a declared edge — proving the guard
    has no accidental extra permissiveness beyond exactly what §10 lists."""

    @staticmethod
    def _assert_allows_matches_declared_edges(table: TransitionTable[Any]) -> None:
        declared = _edges(table)
        all_states = set(table.edges) | set(table.terminal_states)
        for source in all_states:
            for target in all_states:
                expected = (source, target) in declared
                assert table.allows(source, target) is expected, (source, target)

    def test_work_item(self) -> None:
        self._assert_allows_matches_declared_edges(WORK_ITEM_TRANSITIONS)

    def test_run(self) -> None:
        self._assert_allows_matches_declared_edges(RUN_TRANSITIONS)

    def test_node_attempt(self) -> None:
        self._assert_allows_matches_declared_edges(NODE_ATTEMPT_TRANSITIONS)

    def test_factory_change(self) -> None:
        self._assert_allows_matches_declared_edges(FACTORY_CHANGE_TRANSITIONS)
