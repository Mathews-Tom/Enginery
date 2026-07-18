"""A heuristic credential-shaped content scanner.

This is the ledger-level backstop for "raw harness/provider payloads
cannot enter the ledger before adapter-side normalization/redaction": no
adapter exists yet in this milestone, so the write paths that would
otherwise accept unredacted text (artifact bytes, event payload string
values) call :func:`assert_no_raw_credentials` themselves. This is not an
absolute promise to detect every secret format — it matches known
credential shapes (cloud access keys, PEM key blocks, common vendor
token prefixes, bearer headers, and generic ``key = "..."``-style
assignments) and fails loudly on a match rather than silently persisting
it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from enginery.ledger.errors import RawCredentialDetectedError

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "pem_private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer_header", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.]{20,}\b")),
    (
        "generic_secret_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*"
            r"['\"]?[A-Za-z0-9+/=_\-]{16,}['\"]?"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    pattern_name: str
    excerpt: str


def scan_for_credentials(text: str) -> tuple[Finding, ...]:
    """Return every credential-shaped match in ``text``."""
    findings: list[Finding] = []
    for name, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(Finding(pattern_name=name, excerpt=match.group(0)[:12] + "…"))
    return tuple(findings)


def redact_credential_shaped_text(text: str) -> str:
    """Replace known credential-shaped content before persistence."""
    redacted = text
    for name, pattern in _PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    return redacted


def assert_no_raw_credentials(text: str) -> None:
    """Raise :class:`RawCredentialDetectedError` if ``text`` contains
    credential-shaped content."""
    findings = scan_for_credentials(text)
    if findings:
        raise RawCredentialDetectedError(
            f"content matches {len(findings)} credential-shaped pattern(s); refusing to persist",
            details={"patterns": sorted({finding.pattern_name for finding in findings})},
        )


def assert_mapping_has_no_raw_credentials(payload: Mapping[str, object]) -> None:
    """Recursively scan every string leaf of a JSON-shaped payload.

    Used on event payloads, inbox commands, outbox entries, and
    process-manager state before they are written — the same structural
    shapes a harness or provider adapter would otherwise smuggle raw
    output through.
    """
    for value in payload.values():
        _scan_value(value)


def _scan_value(value: object) -> None:
    if isinstance(value, str):
        assert_no_raw_credentials(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _scan_value(item)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            _scan_value(item)


__all__ = [
    "Finding",
    "assert_mapping_has_no_raw_credentials",
    "assert_no_raw_credentials",
    "redact_credential_shaped_text",
    "scan_for_credentials",
]
