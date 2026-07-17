"""Evidence verification, without provider-specific imports.

Rule (03_SYSTEM_DESIGN.md §8.1, §16): ``evidence`` may import ``domain`` and
``application``. It must not import ``engine``, ``ledger``, ``policy``,
``evaluation``, ``adapters``, or ``cli``.
"""

from __future__ import annotations
