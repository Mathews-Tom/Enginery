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


def test_apache_is_recognized_with_or_without_the_appendix_but_not_a_trailing_copyright() -> None:
    terms_only = armory_license._APACHE_TERMS_ONLY

    assert classify_license_text(_APACHE) == "Apache-2.0"
    assert classify_license_text(terms_only) == "Apache-2.0"
    assert classify_license_text(terms_only + "\n\nCopyright 2026 Someone\n") == "unknown"


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


def test_normalization_drops_leading_copyright_lines_but_keeps_lines_that_state_terms() -> None:
    assert normalize_license_text("# Title\nCopyright (c) 2026 Tom\n(c) 2025 X\nBody  text") == (
        "title body text"
    )
    kept = normalize_license_text("Copyright (c) 2026 Tom. Free for personal use.")

    assert "personal use" in kept


def test_a_normal_copyright_header_still_yields_mit() -> None:
    text = "MIT License\n\nCopyright (c) 2024 Foo Bar\n\n" + armory_license._MIT_BODY

    assert classify_license_text(text) == "MIT"
    assert classify_license_text("Copyright 2020-2024, Foo & Co.\n" + armory_license._MIT_BODY) == (
        "MIT"
    )


def test_a_copyright_line_after_the_leading_block_is_not_dropped() -> None:
    body = armory_license._MIT_BODY

    assert classify_license_text(body + "\n\nCopyright (c) 2024 Foo Bar\n") == "unknown"
    assert classify_license_text(body + "\n\nCopyright (c) 2024 Foo Bar\n\nMore.") == "unknown"


_SMUGGLED_LINES = [
    "Copyright 2024 Foo. Files in vendor/ are GPLv3.",
    "Copyright 2024 Foo. Selling copies needs a paid agreement with Foo.",
    "Copyright (c) 2024 Foo; the Commons Clause v1.0 applies to this package.",
    "(c) 2024 vendor/ code is AGPL-3.0",
    "Copyright 2024 Foo <!-- sale needs a paid agreement; vendor/ is GPLv3 -->",
    "Copyright 2024 Foo. Com\u200bmercial u\u200bse prohi\u200bbited.",
    "Copyright 2024 Foo\u2028Commercial use prohibited",
    "Copyright 2024 Foo\u2029Commercial use prohibited",
    "Copyright 2024 Foo: all copies need a paid agreement",
    "Copyright 2024 Foo!",
    "Copyright 2024 Foo? Ask first.",
    "Copyright 2024 Foo All rights reserved.",
    "Copyright 2024 Foo <foo@example.com>",
    "Copyright 2024 Fo\u00f6",
    "Copyright 2024 " + "Foo " * 30,
    "Copyright 2024 Foo vendor is licensed under the GPL",
]


@pytest.mark.parametrize("line", _SMUGGLED_LINES)
def test_smuggled_terms_on_copyright_lines_are_unknown(line: str) -> None:
    body = armory_license._MIT_BODY

    assert classify_license_text(line + "\n\n" + body) == "unknown"
    assert classify_license_text("MIT License\n\n" + line + "\n\n" + body) == "unknown"
    assert classify_license_text(body.replace("the above", line + "\nthe above")) == "unknown"


@pytest.mark.parametrize("char", ["\u200b", "\u2028", "\u2029", "\ufeff", "\u00ad", "\u00e9"])
def test_format_separator_and_non_ascii_characters_are_unknown(char: str) -> None:
    assert classify_license_text(armory_license._MIT_BODY + char) == "unknown"
    assert classify_license_text("a" + char + "\n" + armory_license._MIT_BODY) == "unknown"


def test_html_comments_are_unknown() -> None:
    assert classify_license_text("<!-- x -->\n" + armory_license._MIT_BODY) == "unknown"


def test_the_copyright_sign_is_allowed_in_a_leading_attribution() -> None:
    assert classify_license_text("\u00a9 2024 Foo\n" + armory_license._MIT_BODY) == "MIT"


@pytest.mark.parametrize(
    "text",
    [
        "Under GPLv3.",
        "gpl2",
        "LGPL-2.1",
        "AGPLv3",
        "MPL 2.0",
        "SSPL",
        "Creative Commons",
        "CC-BY",
        "vendor/ is BUSL-1.1",
        "Business Source License 1.1",
        "Elastic License 2.0",
        "vendor/ is EPL-2.0",
        "Released under the EPL.",
        "PolyForm Shield 1.0.0",
        "OSL-3.0",
        "copyleft parts",
    ],
)
def test_notice_markers_are_detected(text: str) -> None:
    assert notice_conflicts(text)


def test_a_plain_attribution_notice_does_not_conflict() -> None:
    assert not notice_conflicts("Original work, licensed under the Apache License 2.0.")


def test_a_reservation_on_a_copyright_line_of_a_notice_is_still_detected() -> None:
    assert notice_conflicts("Copyright (c) 2026 Tom. All rights reserved.")
