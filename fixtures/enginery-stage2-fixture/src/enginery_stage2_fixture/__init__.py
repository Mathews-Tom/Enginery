"""Disposable Stage 2 release-provider proof fixture.

This package exists solely to exercise Enginery's Stage 2 plan-to-release
workflow -- root-to-leaf merge, version/changelog preparation, wheel/sdist
build, and real GitHub Release + PyPI publication -- end to end against a
real, disposable public artifact. It is not a usable library and carries
no functional guarantees beyond the two smoke-testable values below.

``__version__`` is read from installed package metadata rather than
hardcoded here, so it always matches whatever version the wheel was
actually built and published under -- the single source of truth is
``pyproject.toml``'s ``[project] version`` field, never a duplicated
string in source that could drift after a release.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("enginery-stage2-fixture")
except PackageNotFoundError:  # running from source without an installed distribution
    __version__ = "0.0.0.dev0"


def fixture_marker() -> str:
    """A stable, self-identifying value the Stage 2 clean-install smoke check reads."""
    return "enginery-stage2-fixture"
