"""Provider-neutral domain layer.

Rule: ``domain`` imports no application,
infrastructure, adapter, or CLI module. It is the innermost layer and every
other layer may depend on it.
"""

from __future__ import annotations
