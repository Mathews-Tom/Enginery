"""Enginery: the control plane that turns engineering intent into verified outcomes.

``__version__`` is read from installed package metadata rather than hardcoded
here, so ``pyproject.toml`` remains the single canonical source for the
project's version.
"""

from __future__ import annotations

from importlib import metadata

__version__ = metadata.version(__name__)

__all__ = ["__version__"]
