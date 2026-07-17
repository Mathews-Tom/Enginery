"""Provider-neutral domain layer.

Rule (03_SYSTEM_DESIGN.md §8.1): ``domain`` imports no application,
infrastructure, adapter, or CLI module. It is the innermost layer and every
other layer may depend on it.
"""

from __future__ import annotations
