"""Shared fault-injection framework.

One scenario runner reused by every milestone's fault-injection script,
so ``fault_inject_ledger.py`` and later scripts (``fault_inject_workers.py``
and beyond) are thin entry points over the same reporting and exit-code
contract instead of independent harnesses.
"""

from __future__ import annotations
