"""Application layer: orchestrates domain operations through declared ports.

Rule: ``application`` may import ``domain``. It
must not import ``engine``, ``ledger``, ``policy``, ``evidence``,
``evaluation``, ``adapters``, or ``cli``.
"""

from __future__ import annotations
