"""Evidence verification, without provider-specific imports.

Rule: ``evidence`` may import ``domain`` and
``application``. It must not import ``engine``, ``ledger``, ``policy``,
``evaluation``, ``adapters``, or ``cli``.
"""

from __future__ import annotations
