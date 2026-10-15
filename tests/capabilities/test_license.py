from __future__ import annotations

import pytest

from enginery.capabilities.license import LicenseDisposition, classify_license


@pytest.mark.parametrize(
    "license_id", ["MIT", "Apache-2.0", "BSD-3-Clause", "ISC", " MIT ", "CC0-1.0"]
)
def test_reviewed_permissive_licenses_are_redistributable(license_id: str) -> None:
    disposition = classify_license(license_id)

    assert disposition is LicenseDisposition.REDISTRIBUTABLE
    assert disposition.is_hold is False


@pytest.mark.parametrize(
    "license_id", [None, "", "  ", "unknown", "Unknown", "NOASSERTION", "none"]
)
def test_absent_or_unknown_licenses_hold_as_unknown(license_id: str | None) -> None:
    disposition = classify_license(license_id)

    assert disposition is LicenseDisposition.HOLD_UNKNOWN
    assert disposition.is_hold is True


@pytest.mark.parametrize(
    "license_id", ["GPL-3.0-only", "AGPL-3.0", "LicenseRef-proprietary", "mit"]
)
def test_unreviewed_licenses_hold_as_not_allowed(license_id: str) -> None:
    disposition = classify_license(license_id)

    assert disposition is LicenseDisposition.HOLD_NOT_ALLOWED
    assert disposition.is_hold is True
