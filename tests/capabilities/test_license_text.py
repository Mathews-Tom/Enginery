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


_PUBLISHED_CC0 = """\
Creative Commons Legal Code

CC0 1.0 Universal

    CREATIVE COMMONS CORPORATION IS NOT A LAW FIRM AND DOES NOT PROVIDE LEGAL
    SERVICES. DISTRIBUTION OF THIS DOCUMENT DOES NOT CREATE AN ATTORNEY-CLIENT
    RELATIONSHIP. CREATIVE COMMONS PROVIDES THIS INFORMATION ON AN "AS-IS"
    BASIS. CREATIVE COMMONS MAKES NO WARRANTIES REGARDING THE USE OF THIS
    DOCUMENT OR THE INFORMATION OR WORKS PROVIDED HEREUNDER, AND DISCLAIMS
    LIABILITY FOR DAMAGES RESULTING FROM THE USE OF THIS DOCUMENT OR THE
    INFORMATION OR WORKS PROVIDED HEREUNDER.

Statement of Purpose

The laws of most jurisdictions throughout the world automatically confer
exclusive Copyright and Related Rights (defined below) upon the creator and
subsequent owner(s) (each and all, an "owner") of an original work of
authorship and/or a database (each, a "Work").

Certain owners wish to permanently relinquish those rights to a Work for the
purpose of contributing to a commons of creative, cultural and scientific
works ("Commons") that the public can reliably and without fear of later
claims of infringement build upon, modify, incorporate in other works, reuse
and redistribute as freely as possible in any form whatsoever and for any
purposes, including without limitation commercial purposes. These owners may
contribute to the Commons to promote the ideal of a free culture and the
further production of creative, cultural and scientific works, or to gain
reputation or greater distribution for their Work in part through the use and
efforts of others.

For these and/or other purposes and motivations, and without any expectation
of additional consideration or compensation, the person associating CC0 with a
Work (the "Affirmer"), to the extent that he or she is an owner of Copyright
and Related Rights in the Work, voluntarily elects to apply CC0 to the Work
and publicly distribute the Work under its terms, with knowledge of his or her
Copyright and Related Rights in the Work and the meaning and intended legal
effect of CC0 on those rights.

1. Copyright and Related Rights. A Work made available under CC0 may be
protected by copyright and related or neighboring rights ("Copyright and
Related Rights"). Copyright and Related Rights include, but are not limited
to, the following:

  i. the right to reproduce, adapt, distribute, perform, display,
     communicate, and translate a Work;
 ii. moral rights retained by the original author(s) and/or performer(s);
iii. publicity and privacy rights pertaining to a person's image or
     likeness depicted in a Work;
 iv. rights protecting against unfair competition in regards to a Work,
     subject to the limitations in paragraph 4(a), below;
  v. rights protecting the extraction, dissemination, use and reuse of data
     in a Work;
 vi. database rights (such as those arising under Directive 96/9/EC of the
     European Parliament and of the Council of 11 March 1996 on the legal
     protection of databases, and under any national implementation
     thereof, including any amended or successor version of such
     directive); and
vii. other similar, equivalent or corresponding rights throughout the
     world based on applicable law or treaty, and any national
     implementations thereof.

2. Waiver. To the greatest extent permitted by, but not in contravention
of, applicable law, Affirmer hereby overtly, fully, permanently,
irrevocably and unconditionally waives, abandons, and surrenders all of
Affirmer's Copyright and Related Rights and associated claims and causes
of action, whether now known or unknown (including existing as well as
future claims and causes of action), in the Work (i) in all territories
worldwide, (ii) for the maximum duration provided by applicable law or
treaty (including future time extensions), (iii) in any current or future
medium and for any number of copies, and (iv) for any purpose whatsoever,
including without limitation commercial, advertising or promotional
purposes (the "Waiver"). Affirmer makes the Waiver for the benefit of each
member of the public at large and to the detriment of Affirmer's heirs and
successors, fully intending that such Waiver shall not be subject to
revocation, rescission, cancellation, termination, or any other legal or
equitable action to disrupt the quiet enjoyment of the Work by the public
as contemplated by Affirmer's express Statement of Purpose.

3. Public License Fallback. Should any part of the Waiver for any reason
be judged legally invalid or ineffective under applicable law, then the
Waiver shall be preserved to the maximum extent permitted taking into
account Affirmer's express Statement of Purpose. In addition, to the
extent the Waiver is so judged Affirmer hereby grants to each affected
person a royalty-free, non transferable, non sublicensable, non exclusive,
irrevocable and unconditional license to exercise Affirmer's Copyright and
Related Rights in the Work (i) in all territories worldwide, (ii) for the
maximum duration provided by applicable law or treaty (including future
time extensions), (iii) in any current or future medium and for any number
of copies, and (iv) for any purpose whatsoever, including without
limitation commercial, advertising or promotional purposes (the
"License"). The License shall be deemed effective as of the date CC0 was
applied by Affirmer to the Work. Should any part of the License for any
reason be judged legally invalid or ineffective under applicable law, such
partial invalidity or ineffectiveness shall not invalidate the remainder
of the License, and in such case Affirmer hereby affirms that he or she
will not (i) exercise any of his or her remaining Copyright and Related
Rights in the Work or (ii) assert any associated claims and causes of
action with respect to the Work, in either case contrary to Affirmer's
express Statement of Purpose.

4. Limitations and Disclaimers.

 a. No trademark or patent rights held by Affirmer are waived, abandoned,
    surrendered, licensed or otherwise affected by this document.
 b. Affirmer offers the Work as-is and makes no representations or
    warranties of any kind concerning the Work, express, implied,
    statutory or otherwise, including without limitation warranties of
    title, merchantability, fitness for a particular purpose, non
    infringement, or the absence of latent or other defects, accuracy, or
    the present or absence of errors, whether or not discoverable, all to
    the greatest extent permissible under applicable law.
 c. Affirmer disclaims responsibility for clearing rights of other persons that
    may apply to the Work or any use thereof, including without limitation
    any person's Copyright and Related Rights in the Work. Further,
    Affirmer disclaims responsibility for obtaining any necessary consents,
    permissions or other rights required for any use of the Work.
 d. Affirmer understands and acknowledges that Creative Commons is not a
    party to this document and has no duty or obligation with respect to
    this CC0 or use of the Work.
"""

_PUBLISHED_0BSD = """\
BSD Zero Clause License

Copyright (c) 2024 by Jane Example

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
"""


def test_published_cc0_legalcode_is_recognized() -> None:
    assert classify_license_text(_PUBLISHED_CC0) == "CC0-1.0"
    assert classify_license_text(_PUBLISHED_CC0.replace("Creative Commons Legal Code\n", "")) == (
        "CC0-1.0"
    )
    assert classify_license_text(_PUBLISHED_CC0 + "\nExtra term.\n") == "unknown"


def test_published_zero_bsd_text_is_recognized() -> None:
    assert classify_license_text(_PUBLISHED_0BSD) == "0BSD"
    assert classify_license_text(_PUBLISHED_0BSD + "\nExtra term.\n") == "unknown"
