"""Policy evaluation, without provider-specific imports.

Rule: ``policy`` may import ``domain``,
``application``, and ``evidence``. It must not import ``engine``,
``ledger``, ``evaluation``, ``adapters``, or ``cli``.
"""

from __future__ import annotations
