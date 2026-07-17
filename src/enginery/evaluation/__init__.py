"""Evaluation and metrics, without provider-specific imports.

Rule (03_SYSTEM_DESIGN.md §8.1, §22): ``evaluation`` may import ``domain``,
``application``, and ``ledger``. It must not import ``engine``, ``policy``,
``evidence``, ``adapters``, or ``cli``.
"""

from __future__ import annotations
