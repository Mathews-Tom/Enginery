"""Two safe views of the same review packet, with no authority-bearing controls."""

from __future__ import annotations

import html
import json

from enginery.application.review_packet import MAX_PACKET_BYTES, ReviewPacket
from enginery.domain.errors import InvalidInputError

PACKET_MARKER = "enginery-review-packet-v1"
REVIEW_CSP = (
    "default-src 'none'; script-src 'none'; img-src 'none'; style-src 'none'; "
    "connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
_BIDI_CONTROLS = frozenset({0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x2070)})


def escape_terminal(text: str) -> str:
    """Make all C0/C1/DEL and directional controls visible, including newlines."""
    return "".join(
        f"\\u{ord(character):04x}"
        if ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F or ord(character) in _BIDI_CONTROLS
        else character
        for character in text
    )


def _sections(packet: ReviewPacket) -> tuple[tuple[str, str], ...]:
    return (
        ("Run", packet.run_id),
        ("Subject digest", packet.subject_digest),
        ("Request digest", packet.request_digest),
        ("Configuration snapshot", packet.configuration_snapshot_digest),
        ("Base revision", packet.base_revision),
        ("Verified head revision", packet.head_revision),
        *packet.sections,
        ("Current-head CI", packet.ci_evidence_json),
        ("Unresolved findings", packet.findings_json),
        ("Evidence receipt digests", json.dumps(packet.evidence_receipt_digests)),
        ("Scoped diff", packet.diff),
    )


def render_terminal(packet: ReviewPacket) -> tuple[str, ...]:
    """Return complete terminal lines, escaping data before adding trusted layout."""
    lines = (
        "Enginery review packet — feedback requests repairs; it never approves an action.",
        *(f"{escape_terminal(name)}: {escape_terminal(value)}" for name, value in _sections(packet)),
    )
    _bounded("\n".join(lines))
    return lines


def render_html(packet: ReviewPacket) -> str:
    """Render data only: no scripts, remote resources, active links or worker previews."""
    sections = "".join(
        f"<section><h2>{html.escape(name, quote=True)}</h2>"
        f"<pre>{html.escape(value, quote=True)}</pre></section>"
        for name, value in _sections(packet)
    )
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="'
        + html.escape(REVIEW_CSP, quote=True)
        + '"><meta name="enginery-review-packet" content="'
        + PACKET_MARKER
        + '"><title>Enginery review packet</title></head><body>'
        '<h1>Enginery review packet</h1><p>Feedback requests repairs. '
        'This packet and its annotations cannot approve any action. '
        'Artifact references are text only; worker previews are not embedded.</p>'
        + sections
        + '</body></html>'
    )
    _bounded(document)
    return document


def _bounded(document: str) -> None:
    if len(document.encode()) > MAX_PACKET_BYTES:
        raise InvalidInputError("rendered review packet exceeds the 1 MiB limit")
