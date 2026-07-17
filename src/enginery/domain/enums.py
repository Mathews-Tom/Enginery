"""Shared closed value enumerations used across multiple aggregates.

Kept separate from any single aggregate module because ``WorkKind`` and
``RiskClass`` are referenced by ``WorkItem`` and, from M4 onward, by policy
inputs — a single closed vocabulary avoids two independently drifting
definitions of the same domain concept.
"""

from __future__ import annotations

import enum


class WorkKind(enum.Enum):
    """The five normalized units of engineering intent."""

    ISSUE = "issue"
    PLAN = "plan"
    MILESTONE = "milestone"
    INCIDENT = "incident"
    FACTORY_CHANGE = "factory_change"


class RiskClass(enum.Enum):
    """Autonomy is granted per action and risk class."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


__all__ = ["RiskClass", "WorkKind"]
