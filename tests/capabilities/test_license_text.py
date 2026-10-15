from __future__ import annotations

import pytest

from enginery.capabilities import license_text
from enginery.capabilities.license_text import (
    classify_license_text,
    normalize_license_text,
    notice_conflicts,
)

_APACHE = license_text._APACHE_FULL
_BSD_DISCLAIMER = license_text._BSD_DISCLAIMER


def test_apache_is_recognized_with_or_without_the_appendix_but_not_a_trailing_copyright() -> None:
    terms_only = license_text._APACHE_TERMS_ONLY

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
    bsd2 = license_text._BSD_PREAMBLE + _BSD_DISCLAIMER
    bsd3 = license_text._BSD_PREAMBLE + license_text._BSD_3_CLAUSE + _BSD_DISCLAIMER

    assert classify_license_text("Copyright (c) 2026 A\n\n" + bsd2) == "BSD-2-Clause"
    assert classify_license_text("BSD 3-Clause License\n\n" + bsd3) == "BSD-3-Clause"
    assert classify_license_text(license_text._ISC_BODY) == "ISC"
    assert classify_license_text(bsd3 + " and more") == "unknown"
    assert classify_license_text(bsd2.replace("1. ", "1. also ")) == "unknown"


def test_normalization_drops_leading_copyright_lines_but_keeps_lines_that_state_terms() -> None:
    text = "# MIT License\nCopyright (c) 2026 Tom\n(c) 2025 X\nBody  text"

    assert normalize_license_text(text) == "mit license body text"
    kept = normalize_license_text("Copyright (c) 2026 Tom. Free for personal use.")

    assert "personal use" in kept


def test_a_normal_copyright_header_still_yields_mit() -> None:
    text = "MIT License\n\nCopyright (c) 2024 Foo Bar\n\n" + license_text._MIT_BODY

    assert classify_license_text(text) == "MIT"
    assert classify_license_text("Copyright 2020-2024, Foo & Co.\n" + license_text._MIT_BODY) == (
        "MIT"
    )


def test_a_copyright_line_after_the_leading_block_is_not_dropped() -> None:
    body = license_text._MIT_BODY

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
    "Copyright 2024 Foo, selling copies needs a paid agreement with Foo",
    "Copyright 2024 Foo.Selling copies needs a paid agreement",
    "Copyright 2024 Foo, resale requires written consent of Foo",
    "Copyright 2024 Foo - vendor code is gnu affero 3",
    "Copyright 2024 Foo - resale requires a paid agreement",
    "Copyright 2024 Foo, academic purposes exclusively",
    "Copyright 2024 Foo, a fee of 100 usd per seat applies",
    "Copyright 2024 Foo, this grant is revocable at any time",
    "Copyright 2024 Foo, ai training on this package is barred",
    "Copyright 2024 Foo, the vendor directory is apache 2.0",
    "Copyright 2024 Foo, the vendor dir is gnu affero",
    "Copyright 2024 Foo, vendor code is share-alike",
    "Copyright 2024 Foo; vendor code is barred",
    "Copyright 2024 Foo resale-requires-written-consent-of-Foo",
    "Copyright 2024 Foo.Selling.copies.needs.a.paid.agreement.with.Foo",
    "Copyright 2024 Foo ai-training-on-this-package-is-barred",
    "Copyright 2024 Foo the-vendor-directory-is-apache-2.0",
    "Copyright 2024 Foo's-resale-requires-Foo's-written-consent",
    "Copyright 2024 " + "-".join(["resale"] * 60),
    "Copyright 2024 selling-copies-requires-a-paid-agreement-with-foo, Inc.",
    "Copyright 2024 Foo. Resale requires paid agreement.",
    "Copyright 2024 Foo. All resale requires approval.",
    "Copyright 2024 Foo Corp. Resale requires agreement.",
    "Copyright 2024 Foo, Inc. Resale requires agreement.",
    "Copyright 2024 Foo.. Resale requires agreement",
    "Copyright 2024 Foo! Resale requires agreement",
    "Copyright 2024 " + " ".join(["Abcdefghijklmnop"] * 5),
]


@pytest.mark.parametrize("line", _SMUGGLED_LINES)
def test_smuggled_terms_on_copyright_lines_are_unknown(line: str) -> None:
    body = license_text._MIT_BODY

    assert classify_license_text(line + "\n\n" + body) == "unknown"
    assert classify_license_text("MIT License\n\n" + line + "\n\n" + body) == "unknown"
    assert classify_license_text(body.replace("the above", line + "\nthe above")) == "unknown"


@pytest.mark.parametrize("char", ["\u200b", "\u2028", "\u2029", "\ufeff", "\u00ad", "\u00e9"])
def test_format_separator_and_non_ascii_characters_are_unknown(char: str) -> None:
    assert classify_license_text(license_text._MIT_BODY + char) == "unknown"
    assert classify_license_text("a" + char + "\n" + license_text._MIT_BODY) == "unknown"


def test_html_comments_are_unknown() -> None:
    assert classify_license_text("<!-- x -->\n" + license_text._MIT_BODY) == "unknown"


def test_the_copyright_sign_is_allowed_in_a_leading_attribution() -> None:
    assert classify_license_text("\u00a9 2024 Foo\n" + license_text._MIT_BODY) == "MIT"


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


@pytest.mark.parametrize("text", ["G\u200bPLv3", "G\u0420Lv3", "GP\u00adL-3.0"])
def test_a_notice_with_hidden_or_non_ascii_characters_conflicts(text: str) -> None:
    assert notice_conflicts(f"Bundles vendor/ under {text}.")


@pytest.mark.parametrize(
    "text",
    ["SSPLv1", "SSPL-1.0", "GNU Affero", "vendor code is share-alike", "General Public License"],
)
def test_more_notice_markers_are_detected(text: str) -> None:
    assert notice_conflicts(text)


_HOLDERS = [
    "Tom",
    "",
    "Anno Yanzhe Chen",
    "Caylent",
    "shadcn",
    "Matt Pocock",
    "Agents365-ai",
    "Foo Inc.",
    "Foo, Inc.",
    "Foo, and contributors",
    "Foo & Bar, et al.",
]


@pytest.mark.parametrize("holder", _HOLDERS)
def test_name_shaped_holders_are_still_removed(holder: str) -> None:
    line = f"Copyright (c) 2024 {holder}".strip()

    assert classify_license_text(f"{line}\n\n{license_text._MIT_BODY}") == "MIT"


def test_the_leading_block_ends_at_the_first_line_that_is_not_title_or_copyright() -> None:
    body = license_text._MIT_BODY.replace("the above", "x\nCopyright 2024 Foo\nthe above", 1)
    wrapped = "MIT License\nSome other heading\nCopyright 2024 Foo\n" + license_text._MIT_BODY

    assert classify_license_text(body) == "unknown"
    assert classify_license_text(wrapped) == "unknown"


def test_more_than_three_leading_copyright_lines_are_unknown() -> None:
    lines = [f"Copyright 2024 Holder{n}" for n in range(4)]
    body = license_text._MIT_BODY

    assert classify_license_text("\n".join(lines[:3]) + "\n\n" + body) == "MIT"
    assert classify_license_text("\n".join(lines) + "\n\n" + body) == "unknown"


def test_a_copyright_line_longer_than_eighty_characters_is_unknown() -> None:
    holder = " ".join(["Alpha"] * 4)
    short = f"Copyright 2024 {holder}"
    long_line = "Copyright 2024 " + " ".join(["Abcdefghijklmnop"] * 5)

    assert len(short) <= 80 < len(long_line)
    assert classify_license_text(f"{short}\n\n{license_text._MIT_BODY}") == "MIT"
    assert classify_license_text(f"{long_line}\n\n{license_text._MIT_BODY}") == "unknown"


_SPDX_TEXTS = {
    "Unlicense": license_text._UNLICENSE_BODY,
    "0BSD": license_text._ZERO_BSD_BODY,
    "Zlib": license_text._ZLIB_BODY,
    "CC0-1.0": license_text._CC0_BODY,
}


@pytest.mark.parametrize(("spdx", "body"), sorted(_SPDX_TEXTS.items()))
def test_unlicense_zero_bsd_zlib_and_cc0_templates_are_recognized_when_exact(
    spdx: str, body: str
) -> None:
    assert classify_license_text(body) == spdx
    assert classify_license_text(body.upper()) == spdx
    assert classify_license_text(body + " Resale needs a paid agreement.") == "unknown"
    assert classify_license_text(body[:40] + "x" + body[40:]) == "unknown"


def test_template_variants_and_headers_are_recognized() -> None:
    reference = "\n\nFor more information, please refer to <https://unlicense.org>"
    zero_bsd = license_text._ZERO_BSD_BODY
    zlib = license_text._ZLIB_BODY

    assert classify_license_text(license_text._UNLICENSE_BODY + reference) == "Unlicense"
    assert classify_license_text("Copyright (c) 2024 Foo\n\n" + zero_bsd) == "0BSD"
    assert classify_license_text("zlib License\n\n(c) 2024 Foo\n" + zlib) == "Zlib"
    assert classify_license_text("Copyright (C) 1995-2024 Jean-loup Gailly\n" + zlib) == "Zlib"


def test_every_template_text_classifies_to_its_own_id() -> None:
    for spdx, texts in license_text._TEMPLATES.items():
        for text in texts:
            assert classify_license_text(text) == spdx
