"""Tests for enginery.domain.state_machine."""

from __future__ import annotations

import enum

import pytest

from enginery.domain.state_machine import TransitionTable


class _Light(enum.Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"
    BROKEN = "broken"


_LIGHT_TABLE: TransitionTable[_Light] = TransitionTable(
    edges={
        _Light.RED: frozenset({_Light.GREEN}),
        _Light.GREEN: frozenset({_Light.YELLOW}),
        _Light.YELLOW: frozenset({_Light.RED, _Light.BROKEN}),
    },
    terminal_states=frozenset({_Light.BROKEN}),
)


class TestTransitionTable:
    def test_allows_a_declared_transition(self) -> None:
        assert _LIGHT_TABLE.allows(_Light.RED, _Light.GREEN)

    def test_rejects_an_undeclared_transition(self) -> None:
        assert not _LIGHT_TABLE.allows(_Light.RED, _Light.YELLOW)

    def test_rejects_any_transition_out_of_a_terminal_state(self) -> None:
        assert not _LIGHT_TABLE.allows(_Light.BROKEN, _Light.RED)

    def test_require_raises_on_an_illegal_transition(self) -> None:
        with pytest.raises(Exception, match="illegal transition"):
            _LIGHT_TABLE.require(_Light.RED, _Light.YELLOW)

    def test_require_is_silent_on_a_legal_transition(self) -> None:
        _LIGHT_TABLE.require(_Light.RED, _Light.GREEN)

    def test_is_terminal(self) -> None:
        assert _LIGHT_TABLE.is_terminal(_Light.BROKEN)
        assert not _LIGHT_TABLE.is_terminal(_Light.RED)

    def test_reachable_terminals_from_a_non_terminal_state(self) -> None:
        assert _LIGHT_TABLE.reachable_terminals(_Light.RED) == frozenset({_Light.BROKEN})

    def test_reachable_terminals_from_a_terminal_state_is_itself(self) -> None:
        assert _LIGHT_TABLE.reachable_terminals(_Light.BROKEN) == frozenset({_Light.BROKEN})

    def test_construction_rejects_a_terminal_state_with_declared_outgoing_edges(self) -> None:
        with pytest.raises(Exception, match="outgoing"):
            TransitionTable(
                edges={_Light.BROKEN: frozenset({_Light.RED})},
                terminal_states=frozenset({_Light.BROKEN}),
            )

    def test_construction_rejects_a_target_state_with_no_declared_behavior(self) -> None:
        with pytest.raises(Exception, match="no declared behavior"):
            TransitionTable(
                edges={_Light.RED: frozenset({_Light.GREEN})},
                terminal_states=frozenset(),
            )

    def test_construction_requires_at_least_one_terminal_state(self) -> None:
        with pytest.raises(Exception, match="terminal state"):
            TransitionTable(
                edges={_Light.RED: frozenset()},
                terminal_states=frozenset(),
            )

    def test_is_immutable(self) -> None:
        with pytest.raises(AttributeError):
            _LIGHT_TABLE.terminal_states = frozenset()  # type: ignore[misc]


class TestEveryDomainTransitionTableHasNoDeadEnds:
    """A generic liveness proof reused by every concrete state machine's own
    test module: no non-terminal state may be a dead end that can never
    reach a terminal state ("unreachable terminal claims")."""

    @staticmethod
    def assert_every_non_terminal_state_reaches_a_terminal[StateT: enum.Enum](
        table: TransitionTable[StateT],
    ) -> None:
        for state in table.edges:
            assert table.reachable_terminals(state), f"{state} cannot reach any terminal state"
