"""Plan ingestion: turning a validated development plan into normalized,
dependency-checked milestones ready for child-run fan-out.

This package owns exactly three concerns: the ``Plan``/``PlanMilestone``
schema and its dependency-graph validation (``enginery.plans.model``), a
concrete file-format adapter that turns an external plan representation
into that schema (``enginery.plans.loader``), and milestone normalization
into the existing ``WorkItem`` domain model (``enginery.plans.normalization``).
It never schedules, executes, or persists a run itself — that remains the
coordinator runtime's responsibility, consistent with every other work
item entering the system through an adapter boundary rather than a second
execution path.
"""

from __future__ import annotations
