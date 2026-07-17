"""Workflow engine: scheduling and execution without provider-specific imports.

Rule (03_SYSTEM_DESIGN.md §8.1, §8): ``engine`` may import ``domain``,
``application``, ``ledger``, ``policy``, and ``evidence``. It must not import
``adapters`` or ``cli``.
"""

from __future__ import annotations
