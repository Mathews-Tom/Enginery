from __future__ import annotations

import pytest

from enginery.adapters import armory_license
from enginery.adapters.armory_license import (
    classify_license_text,
    normalize_license_text,
    notice_conflicts,
)

_APACHE = armory_license._APACHE_FULL
_BSD_DISCLAIMER = armory_license._BSD_DISCLAIMER


def test_apache_is_recognized_with_or_without_the_appendix_and_copyright_line() -> None:
    terms_only = armory_license._APACHE_TERMS_ONLY

    assert classify_license_text(_APACHE) == "Apache-2.0"
    assert classify_license_text(terms_only) == "Apache-2.0"
    assert classify_license_text(terms_only + "\n\nCopyright 2026 Someone\n") == "Apache-2.0"


def test_apache_with_inserted_or_appended_terms_is_unknown() -> None:
    marker = "grant of copyright license"
    inserted = _APACHE.replace(marker, marker + " (revocable)")

    assert inserted != _APACHE
    assert classify_license_text(inserted) == "unknown"
    assert classify_license_text(_APACHE + " this is also available under the gpl.") == "unknown"


def test_bsd_and_isc_templates_are_recognized_only_when_exact() -> None:
    bsd2 = armory_license._BSD_PREAMBLE + _BSD_DISCLAIMER
    bsd3 = armory_license._BSD_PREAMBLE + armory_license._BSD_3_CLAUSE + _BSD_DISCLAIMER

    assert classify_license_text("Copyright (c) 2026 A\n\n" + bsd2) == "BSD-2-Clause"
    assert classify_license_text("BSD 3-Clause License\n\n" + bsd3) == "BSD-3-Clause"
    assert classify_license_text(armory_license._ISC_BODY) == "ISC"
    assert classify_license_text(bsd3 + " and more") == "unknown"
    assert classify_license_text(bsd2.replace("1. ", "1. also ")) == "unknown"


def test_normalization_drops_copyright_lines_but_keeps_lines_that_state_terms() -> None:
    assert normalize_license_text("# Title\nCopyright (c) 2026 Tom\n(c) 2025 X\nBody  text") == (
        "title body text"
    )
    kept = normalize_license_text("Copyright (c) 2026 Tom. Free for personal use.")

    assert "personal use" in kept


@pytest.mark.parametrize(
    "text",
    ["Under GPLv3.", "gpl2", "LGPL-2.1", "AGPLv3", "MPL 2.0", "SSPL", "Creative Commons", "CC-BY"],
)
def test_notice_markers_are_detected(text: str) -> None:
    assert notice_conflicts(text)


def test_a_plain_attribution_notice_does_not_conflict() -> None:
    assert not notice_conflicts("Original work, licensed under the Apache License 2.0.")


def test_a_reservation_on_a_copyright_line_of_a_notice_is_still_detected() -> None:
    assert notice_conflicts("Copyright (c) 2026 Tom. All rights reserved.")
