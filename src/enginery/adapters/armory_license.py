"""Exact-template license classification for Armory packages.

A license file is classified only by equality with a canonical license text
after normalization: case-folded, markdown heading markers removed, strictly
shaped copyright lines in the leading block (and the Apache appendix's
``Copyright [yyyy] [name of copyright owner]`` line) removed, whitespace collapsed.
A removable copyright line is ``copyright``, ``(c)`` or ``©`` (optionally
followed by ``(c)``/``©``), a year, year range or list, or ``[yyyy]``, and an
optional holder of letters, digits, spaces and ``.,'&-`` (at most 80
characters, no sentence break, no terms word, no other-license marker); the
leading block ends at the first line longer than 40 characters that is not
such a copyright line. Copyright lines anywhere else stay and break the match.
Text with Unicode format characters, line or paragraph separators, control
characters, HTML comments or any non-ASCII character other than ``©`` is
``unknown``, as are inserted terms, appended terms, carve-outs, dual
licensing, and every other license text.
``NOTICE`` files never decide a license; they only make it ``unknown`` when
they carry another license's marker (a GPL, MPL, SSPL, Creative Commons,
BUSL, Elastic, EPL, PolyForm or OSL family name, copyleft, non-commercial
terms, a proprietary or dual-license statement) or reserve all rights.
"""

from __future__ import annotations

import re

UNKNOWN_LICENSE = "unknown"

_APACHE_APPENDIX_COPYRIGHT = "copyright [yyyy] [name of copyright owner]"
# A removable copyright line is a bare attribution: a prefix, a year (range or list) or
# ``[yyyy]``, then an optional holder of letters, digits, spaces and ``.,'&-``.
_COPYRIGHT_LINE = re.compile(
    r"^(?:copyright|\(c\)|\u00a9)(?:\s*(?:\(c\)|\u00a9))?\s+"
    r"(?:\d{4}(?:\s*[-,]\s*\d{4})*|\[yyyy\])"
    r"(?:[\s,.-][a-z0-9 .,'&-]*)?$"
)
_SENTENCE_BREAK = re.compile(r"[.!?]\s+\S")
_TERMS_WORDS = re.compile(
    r"\b(?:may|must|shall|should|not|no|only|prohibited|forbidden|commercial|without|except|"
    r"unless|provided|free|use|used|using|licen[sc]e[ds]?|permitted|permission|allowed|"
    r"restricted|terms|conditions|redistribut\w*|educational|personal|research)\b"
)
_MAX_COPYRIGHT_LINE = 80
_MAX_TITLE_LINE = 40
_HEADING_MARKERS = re.compile(r"^#+\s*")

_MIT_BODY = (
    "permission is hereby granted, free of charge, to any person obtaining a copy of this "
    'software and associated documentation files (the "software"), to deal in the software '
    "without restriction, including without limitation the rights to use, copy, modify, merge, "
    "publish, distribute, sublicense, and/or sell copies of the software, and to permit persons "
    "to whom the software is furnished to do so, subject to the following conditions: the above "
    "copyright notice and this permission notice shall be included in all copies or substantial "
    "portions of the software. "
    'the software is provided "as is", without warranty of any kind, express or implied, '
    "including but not limited to the warranties of merchantability, fitness for a particular "
    "purpose and noninfringement. in no event shall the authors or copyright holders be liable "
    "for any claim, damages or other liability, whether in an action of contract, tort or "
    "otherwise, arising from, out of or in connection with the software or the use or other "
    "dealings in the software."
)
_BSD_PREAMBLE = (
    "redistribution and use in source and binary forms, with or without modification, are "
    "permitted provided that the following conditions are met: "
    "1. redistributions of source code must retain the above copyright notice, this list of "
    "conditions and the following disclaimer. "
    "2. redistributions in binary form must reproduce the above copyright notice, this list of "
    "conditions and the following disclaimer in the documentation and/or other materials "
    "provided with the distribution. "
)
_BSD_3_CLAUSE = (
    "3. neither the name of the copyright holder nor the names of its contributors may be used "
    "to endorse or promote products derived from this software without specific prior written "
    "permission. "
)
_BSD_DISCLAIMER = (
    'this software is provided by the copyright holders and contributors "as is" and any '
    "express or implied warranties, including, but not limited to, the implied warranties of "
    "merchantability and fitness for a particular purpose are disclaimed. in no event shall the "
    "copyright holder or contributors be liable for any direct, indirect, incidental, special, "
    "exemplary, or consequential damages (including, but not limited to, procurement of "
    "substitute goods or services; loss of use, data, or profits; or business interruption) "
    "however caused and on any theory of liability, whether in contract, strict liability, or "
    "tort (including negligence or otherwise) arising in any way out of the use of this "
    "software, even if advised of the possibility of such damage."
)
_ISC_BODY = (
    "permission to use, copy, modify, and/or distribute this software for any purpose with or "
    "without fee is hereby granted, provided that the above copyright notice and this "
    "permission notice appear in all copies. "
    'the software is provided "as is" and the author disclaims all warranties with regard to '
    "this software including all implied warranties of merchantability and fitness. in no "
    "event shall the author be liable for any special, direct, indirect, or consequential "
    "damages or any damages whatsoever resulting from loss of use, data or profits, whether in "
    "an action of contract, negligence or other tortious action, arising out of or in "
    "connection with the use or performance of this software."
)
_APACHE_FULL = (
    "apache license version 2.0, january 2004 http://www.apache.org/licenses/ terms "
    'and conditions for use, reproduction, and distribution 1. definitions. "license"'
    " shall mean the terms and conditions for use, reproduction, and distribution as "
    'defined by sections 1 through 9 of this document. "licensor" shall mean the '
    "copyright owner or entity authorized by the copyright owner that is granting the"
    ' license. "legal entity" shall mean the union of the acting entity and all other'
    " entities that control, are controlled by, or are under common control with that"
    ' entity. for the purposes of this definition, "control" means (i) the power, '
    "direct or indirect, to cause the direction or management of such entity, whether"
    " by contract or otherwise, or (ii) ownership of fifty percent (50%) or more of "
    'the outstanding shares, or (iii) beneficial ownership of such entity. "you" (or '
    '"your") shall mean an individual or legal entity exercising permissions granted '
    'by this license. "source" form shall mean the preferred form for making '
    "modifications, including but not limited to software source code, documentation "
    'source, and configuration files. "object" form shall mean any form resulting '
    "from mechanical transformation or translation of a source form, including but "
    "not limited to compiled object code, generated documentation, and conversions to"
    ' other media types. "work" shall mean the work of authorship, whether in source '
    "or object form, made available under the license, as indicated by a copyright "
    "notice that is included in or attached to the work (an example is provided in "
    'the appendix below). "derivative works" shall mean any work, whether in source '
    "or object form, that is based on (or derived from) the work and for which the "
    "editorial revisions, annotations, elaborations, or other modifications "
    "represent, as a whole, an original work of authorship. for the purposes of this "
    "license, derivative works shall not include works that remain separable from, or"
    " merely link (or bind by name) to the interfaces of, the work and derivative "
    'works thereof. "contribution" shall mean any work of authorship, including the '
    "original version of the work and any modifications or additions to that work or "
    "derivative works thereof, that is intentionally submitted to licensor for "
    "inclusion in the work by the copyright owner or by an individual or legal entity"
    " authorized to submit on behalf of the copyright owner. for the purposes of this"
    ' definition, "submitted" means any form of electronic, verbal, or written '
    "communication sent to the licensor or its representatives, including but not "
    "limited to communication on electronic mailing lists, source code control "
    "systems, and issue tracking systems that are managed by, or on behalf of, the "
    "licensor for the purpose of discussing and improving the work, but excluding "
    "communication that is conspicuously marked or otherwise designated in writing by"
    ' the copyright owner as "not a contribution." "contributor" shall mean licensor '
    "and any individual or legal entity on behalf of whom a contribution has been "
    "received by licensor and subsequently incorporated within the work. 2. grant of "
    "copyright license. subject to the terms and conditions of this license, each "
    "contributor hereby grants to you a perpetual, worldwide, non-exclusive, "
    "no-charge, royalty-free, irrevocable copyright license to reproduce, prepare "
    "derivative works of, publicly display, publicly perform, sublicense, and "
    "distribute the work and such derivative works in source or object form. 3. grant"
    " of patent license. subject to the terms and conditions of this license, each "
    "contributor hereby grants to you a perpetual, worldwide, non-exclusive, "
    "no-charge, royalty-free, irrevocable (except as stated in this section) patent "
    "license to make, have made, use, offer to sell, sell, import, and otherwise "
    "transfer the work, where such license applies only to those patent claims "
    "licensable by such contributor that are necessarily infringed by their "
    "contribution(s) alone or by combination of their contribution(s) with the work "
    "to which such contribution(s) was submitted. if you institute patent litigation "
    "against any entity (including a cross-claim or counterclaim in a lawsuit) "
    "alleging that the work or a contribution incorporated within the work "
    "constitutes direct or contributory patent infringement, then any patent licenses"
    " granted to you under this license for that work shall terminate as of the date "
    "such litigation is filed. 4. redistribution. you may reproduce and distribute "
    "copies of the work or derivative works thereof in any medium, with or without "
    "modifications, and in source or object form, provided that you meet the "
    "following conditions: (a) you must give any other recipients of the work or "
    "derivative works a copy of this license; and (b) you must cause any modified "
    "files to carry prominent notices stating that you changed the files; and (c) you"
    " must retain, in the source form of any derivative works that you distribute, "
    "all copyright, patent, trademark, and attribution notices from the source form "
    "of the work, excluding those notices that do not pertain to any part of the "
    'derivative works; and (d) if the work includes a "notice" text file as part of '
    "its distribution, then any derivative works that you distribute must include a "
    "readable copy of the attribution notices contained within such notice file, "
    "excluding those notices that do not pertain to any part of the derivative works,"
    " in at least one of the following places: within a notice text file distributed "
    "as part of the derivative works; within the source form or documentation, if "
    "provided along with the derivative works; or, within a display generated by the "
    "derivative works, if and wherever such third-party notices normally appear. the "
    "contents of the notice file are for informational purposes only and do not "
    "modify the license. you may add your own attribution notices within derivative "
    "works that you distribute, alongside or as an addendum to the notice text from "
    "the work, provided that such additional attribution notices cannot be construed "
    "as modifying the license. you may add your own copyright statement to your "
    "modifications and may provide additional or different license terms and "
    "conditions for use, reproduction, or distribution of your modifications, or for "
    "any such derivative works as a whole, provided your use, reproduction, and "
    "distribution of the work otherwise complies with the conditions stated in this "
    "license. 5. submission of contributions. unless you explicitly state otherwise, "
    "any contribution intentionally submitted for inclusion in the work by you to the"
    " licensor shall be under the terms and conditions of this license, without any "
    "additional terms or conditions. notwithstanding the above, nothing herein shall "
    "supersede or modify the terms of any separate license agreement you may have "
    "executed with licensor regarding such contributions. 6. trademarks. this license"
    " does not grant permission to use the trade names, trademarks, service marks, or"
    " product names of the licensor, except as required for reasonable and customary "
    "use in describing the origin of the work and reproducing the content of the "
    "notice file. 7. disclaimer of warranty. unless required by applicable law or "
    "agreed to in writing, licensor provides the work (and each contributor provides "
    'its contributions) on an "as is" basis, without warranties or conditions of any '
    "kind, either express or implied, including, without limitation, any warranties "
    "or conditions of title, non-infringement, merchantability, or fitness for a "
    "particular purpose. you are solely responsible for determining the "
    "appropriateness of using or redistributing the work and assume any risks "
    "associated with your exercise of permissions under this license. 8. limitation "
    "of liability. in no event and under no legal theory, whether in tort (including "
    "negligence), contract, or otherwise, unless required by applicable law (such as "
    "deliberate and grossly negligent acts) or agreed to in writing, shall any "
    "contributor be liable to you for damages, including any direct, indirect, "
    "special, incidental, or consequential damages of any character arising as a "
    "result of this license or out of the use or inability to use the work (including"
    " but not limited to damages for loss of goodwill, work stoppage, computer "
    "failure or malfunction, or any and all other commercial damages or losses), even"
    " if such contributor has been advised of the possibility of such damages. 9. "
    "accepting warranty or additional liability. while redistributing the work or "
    "derivative works thereof, you may choose to offer, and charge a fee for, "
    "acceptance of support, warranty, indemnity, or other liability obligations "
    "and/or rights consistent with this license. however, in accepting such "
    "obligations, you may act only on your own behalf and on your sole "
    "responsibility, not on behalf of any other contributor, and only if you agree to"
    " indemnify, defend, and hold each contributor harmless for any liability "
    "incurred by, or claims asserted against, such contributor by reason of your "
    "accepting any such warranty or additional liability. end of terms and conditions"
    " appendix: how to apply the apache license to your work. to apply the apache "
    "license to your work, attach the following boilerplate notice, with the fields "
    'enclosed by brackets "[]" replaced with your own identifying information. (don\'t'
    " include the brackets!) the text should be enclosed in the appropriate comment "
    "syntax for the file format. we also recommend that a file or class name and "
    'description of purpose be included on the same "printed page" as the copyright '
    "notice for easier identification within third-party archives. licensed under the"
    ' apache license, version 2.0 (the "license"); you may not use this file except '
    "in compliance with the license. you may obtain a copy of the license at "
    "http://www.apache.org/licenses/license-2.0 unless required by applicable law or "
    "agreed to in writing, software distributed under the license is distributed on "
    'an "as is" basis, without warranties or conditions of any kind, either express '
    "or implied. see the license for the specific language governing permissions and "
    "limitations under the license."
)
_APACHE_TERMS_END = "end of terms and conditions"
_APACHE_TERMS_ONLY = _APACHE_FULL[: _APACHE_FULL.index(_APACHE_TERMS_END) + len(_APACHE_TERMS_END)]


def _with_titles(body: str, *titles: str) -> frozenset[str]:
    return frozenset({body, *(f"{title} {body}" for title in titles)})


_TEMPLATES: dict[str, frozenset[str]] = {
    "MIT": _with_titles(_MIT_BODY, "mit license", "the mit license"),
    "Apache-2.0": frozenset({_APACHE_FULL, _APACHE_TERMS_ONLY}),
    "BSD-2-Clause": _with_titles(
        _BSD_PREAMBLE + _BSD_DISCLAIMER, "bsd 2-clause license", "bsd-2-clause license"
    ),
    "BSD-3-Clause": _with_titles(
        _BSD_PREAMBLE + _BSD_3_CLAUSE + _BSD_DISCLAIMER,
        "bsd 3-clause license",
        "bsd-3-clause license",
    ),
    "ISC": _with_titles(_ISC_BODY, "isc license"),
}
_BY_TEXT = {text: spdx for spdx, texts in _TEMPLATES.items() for text in texts}

_OTHER_LICENSE_MARKERS = re.compile(
    r"\b[la]?gpl(?:v?\d|\b)"
    r"|general public license"
    r"|\bmpl(?:v?\d|\b)"
    r"|mozilla public license"
    r"|\bsspl\b"
    r"|server side public license"
    r"|creative commons"
    r"|\bcc[-\s]by\b"
    r"|non[-\s]?commercial"
    r"|commons clause"
    r"|\beupl\b"
    r"|\bcddl\b"
    r"|eclipse public license"
    r"|proprietary"
    r"|dual[-\s]licen"
    r"|at your option"
    r"|choose either"
    r"|\bbusl\b"
    r"|business source license"
    r"|elastic license"
    r"|\bepl(?:-?\d|\b)"
    r"|polyform"
    r"|\bosl\b"
    r"|copyleft"
)
_PERMISSIVE_GRANT_MARKERS = (
    "permission is hereby granted, free of charge",
    "licensed under the apache license",
)


def _is_suspect(text: str) -> bool:
    """Hidden or non-ASCII content that could smuggle terms past a line-based reading.

    Every Unicode format character (category ``Cf``) and every line or
    paragraph separator is non-ASCII, so the ASCII-plus-``©`` rule covers them.
    """

    if "<!--" in text:
        return True
    return any(
        (ord(char) < 0x20 and char not in "\t\n\r") or (ord(char) > 0x7E and char != "\u00a9")
        for char in text
    )


def _removable_copyright(line: str) -> bool:
    """Whether ``line`` is a strictly shaped attribution that states no terms."""

    return (
        len(line) <= _MAX_COPYRIGHT_LINE
        and _COPYRIGHT_LINE.match(line) is not None
        and _TERMS_WORDS.search(line) is None
        and _OTHER_LICENSE_MARKERS.search(line) is None
        and "all rights reserved" not in line
        and _SENTENCE_BREAK.search(line) is None
    )


def normalize_license_text(text: str) -> str:
    """Case-fold, drop heading markers and leading copyright lines, collapse whitespace.

    Only strictly shaped copyright lines in the leading block (before the first
    long line of prose) are dropped; anywhere else they stay and break the
    exact match.
    """

    kept: list[str] = []
    leading = True
    for raw in text.casefold().splitlines():
        line = _HEADING_MARKERS.sub("", raw.strip())
        if line == _APACHE_APPENDIX_COPYRIGHT:
            continue
        if leading:
            if _removable_copyright(line):
                continue
            if len(line) > _MAX_TITLE_LINE:
                leading = False
        kept.append(line)
    return " ".join(" ".join(kept).split())


def classify_license_text(text: str) -> str:
    """The SPDX id of a text equal to a canonical template, else ``unknown``.

    Text with Unicode format characters, line or paragraph separators, control
    characters, HTML comments or non-ASCII characters other than ``©`` is
    ``unknown``.
    """

    if _is_suspect(text):
        return UNKNOWN_LICENSE
    return _BY_TEXT.get(normalize_license_text(text), UNKNOWN_LICENSE)


def notice_conflicts(text: str) -> bool:
    """Whether an attribution notice names a license family, or reserves all rights."""

    normalized = " ".join(text.casefold().split())
    if _OTHER_LICENSE_MARKERS.search(normalized) is not None:
        return True
    return "all rights reserved" in normalized and not any(
        marker in normalized for marker in _PERMISSIVE_GRANT_MARKERS
    )


__all__ = [
    "UNKNOWN_LICENSE",
    "classify_license_text",
    "normalize_license_text",
    "notice_conflicts",
]
