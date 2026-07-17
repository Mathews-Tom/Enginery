"""A generic, self-validating guarded state-transition table.

Every domain aggregate with a lifecycle state (``WorkItem``, ``Run``,
``NodeAttempt``, ``FactoryChange``) reuses one ``TransitionTable`` instance
instead of re-implementing guard logic per aggregate, so the exact edges and
terminal semantics are enforced identically everywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from enginery.domain.errors import InternalInvariantViolationError, InvalidInputError


@dataclass(frozen=True, slots=True)
class TransitionTable[StateT]:
    """An explicit, closed directed graph of legal state transitions.

    ``edges`` maps every non-terminal state to the set of states it may
    transition to. ``terminal_states`` are states with no outgoing edges. A
    state absent from both ``edges`` and ``terminal_states`` is unknown and
    cannot appear anywhere in the table — every reference to it is rejected
    at construction time rather than discovered at transition time.
    """

    edges: Mapping[StateT, frozenset[StateT]]
    terminal_states: frozenset[StateT]

    def __post_init__(self) -> None:
        overlapping = set(self.edges) & set(self.terminal_states)
        if overlapping:
            raise InternalInvariantViolationError(
                "terminal states cannot declare outgoing transitions",
                details={"states": sorted(str(state) for state in overlapping)},
            )
        declared_targets = {target for targets in self.edges.values() for target in targets}
        unknown_targets = declared_targets - set(self.edges) - self.terminal_states
        if unknown_targets:
            raise InvalidInputError(
                "transition table references states with no declared behavior",
                details={"states": sorted(str(state) for state in unknown_targets)},
            )
        if not self.terminal_states:
            raise InvalidInputError("a transition table must declare at least one terminal state")

    def is_terminal(self, state: StateT) -> bool:
        return state in self.terminal_states

    def allows(self, source: StateT, target: StateT) -> bool:
        if self.is_terminal(source):
            return False
        return target in self.edges.get(source, frozenset())

    def require(self, source: StateT, target: StateT) -> None:
        if not self.allows(source, target):
            raise InvalidInputError(
                f"illegal transition: {source} -> {target}",
                details={"source": str(source), "target": str(target)},
            )

    def reachable_terminals(self, source: StateT) -> frozenset[StateT]:
        """Every terminal state reachable from ``source`` via legal transitions.

        An empty result for a non-terminal state means that state is a dead
        end — proving the absence of dead ends is exactly the "unreachable
        terminal claims" invariant this module enforces.
        """
        seen: set[StateT] = set()
        frontier = [source]
        terminals: set[StateT] = set()
        while frontier:
            state = frontier.pop()
            if state in seen:
                continue
            seen.add(state)
            if self.is_terminal(state):
                terminals.add(state)
                continue
            frontier.extend(self.edges.get(state, frozenset()))
        return frozenset(terminals)


__all__ = ["TransitionTable"]
