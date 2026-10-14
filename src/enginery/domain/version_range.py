"""Strict numeric versions and the one range form the execution catalog accepts.

A version is exactly ``MAJOR.MINOR.PATCH`` in ASCII decimal digits with no leading
zeros. Pre-release and build tags, extra components, whitespace, and anything else are
rejected, never normalized or truncated, so a host banner like ``18.11.0-beta.1`` can
never be treated as ``18.11.0``.

A range is the canonical text ``>=LOWER,<UPPER``: an inclusive lower bound and an
exclusive upper bound, nothing else. No operator, wildcard, or compound syntax exists,
so a catalog file cannot express a range the comparator does not fully implement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

from enginery.domain.errors import InvalidInputError

_VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_RANGE_PATTERN = re.compile(r">=([^,<>=\s]+),<([^,<>=\s]+)")


@dataclass(frozen=True, slots=True, order=True)
class Version:
    """A plain numeric ``MAJOR.MINOR.PATCH`` version, ordered numerically per component."""

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        if min(self.major, self.minor, self.patch) < 0:
            raise InvalidInputError("version components must be non-negative")

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse a plain numeric version or reject it; nothing is repaired."""
        match = _VERSION_PATTERN.fullmatch(text)
        if match is None:
            raise InvalidInputError(
                f"{text!r} is not a plain numeric MAJOR.MINOR.PATCH version",
                details={"version": text},
            )
        major, minor, patch = (int(part) for part in match.groups())
        return cls(major=major, minor=minor, patch=patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class VersionRange:
    """A half-open ``[lower, upper)`` interval of plain numeric versions."""

    lower: Version
    upper: Version

    def __post_init__(self) -> None:
        if not self.lower < self.upper:
            raise InvalidInputError(
                "a version range must have a lower bound below its upper bound",
                details={"range": f">={self.lower},<{self.upper}"},
            )

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse the canonical ``>=LOWER,<UPPER`` form; any other operator or shape is rejected."""
        match = _RANGE_PATTERN.fullmatch(text)
        if match is None:
            raise InvalidInputError(
                f"{text!r} is not a version range of the form '>=MAJOR.MINOR.PATCH,"
                "<MAJOR.MINOR.PATCH'",
                details={"range": text},
            )
        lower, upper = match.groups()
        return cls(lower=Version.parse(lower), upper=Version.parse(upper))

    def contains(self, version: Version) -> bool:
        return self.lower <= version < self.upper

    def accepts(self, text: str) -> bool:
        """Return whether a banner version is plain numeric and inside the range.

        Text that is not a plain numeric version is never accepted.
        """
        try:
            version = Version.parse(text)
        except InvalidInputError:
            return False
        return self.contains(version)

    def __str__(self) -> str:
        return f">={self.lower},<{self.upper}"


__all__ = ["Version", "VersionRange"]
