"""Workflow engine: scheduling and execution without provider-specific imports.

Rule: ``engine`` may import ``domain``,
``application``, ``ledger``, ``policy``, and ``evidence``. It must not import
``adapters`` or ``cli``.
"""

from __future__ import annotations
