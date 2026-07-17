"""Command-line interface: invokes application services and owns presentation only.

Rule (03_SYSTEM_DESIGN.md §8.1, §23): ``cli`` may import any inner layer. No
other layer may import ``cli``.
"""

from __future__ import annotations
