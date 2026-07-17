"""Event ledger and artifact store, without provider-specific imports.

Rule: ``ledger`` may import ``domain`` and
``application``. It must not import ``engine``, ``policy``, ``evidence``,
``evaluation``, ``adapters``, or ``cli``.
"""

from __future__ import annotations
