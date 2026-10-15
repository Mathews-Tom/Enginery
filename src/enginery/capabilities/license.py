"""Per-package license classification for redistribution.

A package's license is declared metadata, never a verified fact, and the
check runs package by package (every transitive dependency has its own
entry). Redistribution fails closed: only an SPDX identifier on the reviewed
permissive list is redistributable; an absent, blank, ``unknown``, or
``NOASSERTION`` license is an *unknown* hold, and any other identifier is a
*not-allowed* hold until it is reviewed and added here.
"""

from __future__ import annotations

import enum

REDISTRIBUTABLE_LICENSES: frozenset[str] = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "MIT",
        "Unlicense",
        "Zlib",
    }
)
_UNKNOWN_MARKERS = frozenset({"", "unknown", "noassertion", "none"})


class LicenseDisposition(enum.StrEnum):
    """Whether a package's declared license permits redistribution."""

    REDISTRIBUTABLE = "redistributable"
    HOLD_UNKNOWN = "hold_unknown"
    HOLD_NOT_ALLOWED = "hold_not_allowed"

    @property
    def is_hold(self) -> bool:
        return self is not LicenseDisposition.REDISTRIBUTABLE


def classify_license(license_id: str | None) -> LicenseDisposition:
    """Classify one declared SPDX identifier for redistribution."""

    if license_id is None or license_id.strip().lower() in _UNKNOWN_MARKERS:
        return LicenseDisposition.HOLD_UNKNOWN
    if license_id.strip() in REDISTRIBUTABLE_LICENSES:
        return LicenseDisposition.REDISTRIBUTABLE
    return LicenseDisposition.HOLD_NOT_ALLOWED


__all__ = ["REDISTRIBUTABLE_LICENSES", "LicenseDisposition", "classify_license"]
