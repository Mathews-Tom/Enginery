"""Provider implementations: work ledgers, harnesses, workspaces, SCM, CI,
release, and capability registries.

Rule (03_SYSTEM_DESIGN.md §8.1, §14): ``adapters`` may import ``domain``,
``application``, ``engine``, ``ledger``, ``policy``, ``evidence``, and
``evaluation``. Provider SDK objects never cross this boundary into inner
layers. ``adapters`` must not import ``cli``.
"""

from __future__ import annotations
