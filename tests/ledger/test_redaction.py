from __future__ import annotations

import pytest

from enginery.ledger.errors import RawCredentialDetectedError
from enginery.ledger.redaction import (
    assert_mapping_has_no_raw_credentials,
    assert_no_raw_credentials,
    redact_credential_shaped_text,
    scan_for_credentials,
)


@pytest.mark.parametrize(
    "text",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "xoxb-1234567890-abcdefghij",
        "Authorization: Bearer abcdefghijklmnopqrstuvwx",
        'api_key = "sk-abcdefghijklmnopqrstuvwx"',
        "password: 'hunter2hunter2hunter2'",
    ],
)
def test_credential_shaped_text_is_detected(text: str) -> None:
    findings = scan_for_credentials(text)
    assert findings
    with pytest.raises(RawCredentialDetectedError):
        assert_no_raw_credentials(text)


def test_credential_shaped_text_is_redacted_before_persistence() -> None:
    redacted = redact_credential_shaped_text(
        "token=abcdefghijklmnopqrstuvwx and AKIAABCDEFGHIJKLMNOP"
    )

    assert "abcdefghijklmnopqrstuvwx" not in redacted
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert_no_raw_credentials(redacted)


@pytest.mark.parametrize(
    "text",
    [
        "just a normal sentence about deploying the service",
        "digest: sha256:5d41402abc4b2a76b9719d911017c592",
        "correlation_id cmd-0001-example",
        "token_count is 42",
    ],
)
def test_ordinary_text_is_not_flagged(text: str) -> None:
    assert scan_for_credentials(text) == ()
    assert_no_raw_credentials(text)  # does not raise


def test_assert_mapping_scans_nested_structures() -> None:
    payload = {
        "outer": {"inner": ["fine", "AKIAABCDEFGHIJKLMNOP"]},
    }
    with pytest.raises(RawCredentialDetectedError):
        assert_mapping_has_no_raw_credentials(payload)


def test_assert_mapping_passes_clean_nested_structures() -> None:
    payload = {"outer": {"inner": ["fine", "also fine"]}, "count": 3, "flag": True}
    assert_mapping_has_no_raw_credentials(payload)  # does not raise


def test_findings_report_matched_pattern_names() -> None:
    findings = scan_for_credentials("AKIAABCDEFGHIJKLMNOP and ghp_" + "a1b2c3d4e5" * 4)
    names = {finding.pattern_name for finding in findings}
    assert "aws_access_key_id" in names
    assert "github_token" in names
