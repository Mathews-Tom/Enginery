"""Policy evaluation, without provider-specific imports.

Rule (03_SYSTEM_DESIGN.md §8.1, §15): ``policy`` may import ``domain``,
``application``, and ``evidence``. It must not import ``engine``,
``ledger``, ``evaluation``, ``adapters``, or ``cli``.
"""

from __future__ import annotations
