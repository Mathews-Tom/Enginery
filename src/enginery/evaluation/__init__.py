"""Evaluation and metrics, without provider-specific imports.

Rule: ``evaluation`` may import ``domain``,
``application``, and ``ledger``. It must not import ``engine``, ``policy``,
``evidence``, ``adapters``, or ``cli``.
"""

from __future__ import annotations
