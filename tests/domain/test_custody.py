from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from enginery.domain.custody import (
    DEFAULT_DECISIONS,
    AuthCustodyReceipt,
    CustodyHost,
    Endpoint,
    TokenFileMetadata,
    build_custody_receipt,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError

_NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def _token(*, path: str = "/run/gateway.token", mode: str | None = "0600") -> TokenFileMetadata:
    if mode is None:
        return TokenFileMetadata(path, False, False, False, None, None)
    return TokenFileMetadata(path, True, True, False, mode, 43)


def _receipt(
    host: CustodyHost = CustodyHost.MACOS,
    token: TokenFileMetadata | None = None,
    **overrides: object,
) -> AuthCustodyReceipt:
    arguments: dict[str, object] = {
        "host": host,
        "gateway_url": "http://127.0.0.1:7011",
        "broker_url": "http://127.0.0.1:7012/broker",
        "token_file": token or _token(),
        "created_at": _NOW,
    }
    arguments.update(overrides)
    return build_custody_receipt(**arguments)  # type: ignore[arg-type]


def test_receipt_round_trips_and_digest_is_the_canonical_state_hash() -> None:
    receipt = _receipt()

    decoded = AuthCustodyReceipt.from_state(json.loads(json.dumps(receipt.to_state())))

    assert decoded == receipt
    assert decoded.digest == receipt.digest
    canonical = json.dumps(receipt.to_state(), sort_keys=True, separators=(",", ":"))
    assert receipt.digest == Digest.of_bytes(canonical.encode("utf-8"))


def test_macos_receipt_records_scope_custody_exposure_and_decisions() -> None:
    state = _receipt().to_state()

    assert state["host"] == "macos"
    assert state["grant_scope"] == {
        "relay_providers": ["anthropic", "openai-codex"],
        "broker_only_providers": ["google-antigravity"],
    }
    custody = state["custody"]
    assert isinstance(custody, dict)
    assert custody["location"] == "macos-host"
    assert state["owner_decisions"] == list(DEFAULT_DECISIONS)
    assert any("relay holds the gateway bearer" in line for line in state["exposure"])  # type: ignore[attr-defined]
    assert any("dummy credential" in line for line in state["exposure"])  # type: ignore[attr-defined]
    assert any("--regenerate" in line for line in state["revocation"])  # type: ignore[attr-defined]
    assert any("relay down" in line for line in state["cleanup"])  # type: ignore[attr-defined]
    assert state["conforms"] is True


def test_lima_receipt_records_remote_custody_and_requires_an_owner_only_copy() -> None:
    lima_gateway = "http://host.lima.internal:7011"
    conforming = _receipt(CustodyHost.UBUNTU_LIMA, gateway_url=lima_gateway).to_state()
    loose = _receipt(CustodyHost.UBUNTU_LIMA, _token(mode="0644"), gateway_url=lima_gateway)

    custody = conforming["custody"]
    assert isinstance(custody, dict)
    assert custody["location"] == "remote-through-macos-gateway"
    assert "host.lima.internal" in str(custody["description"])
    assert conforming["conforms"] is True
    assert not loose.conforms
    assert "expected 0600" in loose.findings[0]
    assert loose.to_state()["conforms"] is False


def test_every_host_applies_the_relay_token_file_rules() -> None:
    for host in CustodyHost:
        assert not _receipt(host, _token(mode="0644")).conforms
        assert "expected 0600" in _receipt(host, _token(mode="0644")).findings[0]
        missing = _receipt(host, _token(mode=None))
        assert not missing.conforms
        assert "does not exist" in missing.findings[0]
        link = TokenFileMetadata("/t", True, False, True, "0600", 1)
        assert any("symbolic link" in f for f in _receipt(host, link).findings)
        directory = TokenFileMetadata("/t", True, False, False, "0600", 1)
        assert any("not a regular file" in f for f in _receipt(host, directory).findings)
        foreign = TokenFileMetadata("/t", True, True, False, "0600", 1, owned_by_invoker=False)
        assert any("not owned by the invoking user" in f for f in _receipt(host, foreign).findings)


def test_owner_flag_is_omitted_when_default_and_round_trips_when_false() -> None:
    assert "owned_by_invoker" not in _token().to_state()
    foreign = TokenFileMetadata("/t", True, True, False, "0600", 1, owned_by_invoker=False)
    receipt = _receipt(token=foreign)
    decoded = AuthCustodyReceipt.from_state(json.loads(json.dumps(receipt.to_state())))
    assert decoded.token_file.owned_by_invoker is False
    assert decoded == receipt


def test_custody_text_is_built_from_the_actual_gateway_and_mode() -> None:
    macos = _receipt(CustodyHost.MACOS, _token(mode="0640"))
    assert "http://127.0.0.1:7011" in macos.custody
    assert "observed mode 0640" in macos.custody
    assert "lima" not in macos.custody.lower()

    other = _receipt(CustodyHost.UBUNTU_LIMA, gateway_url="http://gateway.example:9000")
    assert other.custody_location == "remote-through-macos-gateway"
    assert "http://gateway.example:9000" in other.custody

    lima = _receipt(CustodyHost.UBUNTU_LIMA, gateway_url="http://host.lima.internal:7011")
    assert "host.lima.internal" in lima.custody


def test_receipt_carries_only_token_file_metadata() -> None:
    receipt = _receipt(token=_token(path="/run/gateway.token"))

    token = receipt.to_state()["custody"]["token_file"]  # type: ignore[index]
    assert set(token) == {"path", "exists", "regular_file", "symlink", "mode", "size_bytes"}
    assert token["mode"] == "0600"
    assert token["size_bytes"] == 43


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@127.0.0.1:7011",
        "http://:secret@127.0.0.1:7011",
        "http://127.0.0.1:7011/?token=abc",
        "http://127.0.0.1:7011/#frag",
        "ftp://127.0.0.1/x",
        "127.0.0.1:7011",
        " http://127.0.0.1:7011",
        "",
    ],
)
def test_endpoints_with_credentials_or_unsupported_shapes_are_refused(url: str) -> None:
    with pytest.raises(InvalidInputError):
        Endpoint.parse(url, name="gateway url")
    with pytest.raises(InvalidInputError):
        _receipt(gateway_url=url)
    with pytest.raises(InvalidInputError):
        _receipt(broker_url=url)


def test_custom_decisions_replace_the_defaults_and_must_be_one_short_line() -> None:
    receipt = _receipt(decisions=["2026-10-14: second host rotated"])

    assert receipt.decisions == ("2026-10-14: second host rotated",)
    for bad in ("two\nlines", "x" * 201, "  "):
        with pytest.raises(InvalidInputError):
            _receipt(decisions=[bad])


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(InvalidInputError):
        _receipt(created_at=datetime(2026, 10, 13, 12, 0))


def test_tampered_state_is_refused_on_decode() -> None:
    state = json.loads(json.dumps(_receipt().to_state()))
    state["conforms"] = False
    with pytest.raises(InvalidInputError):
        AuthCustodyReceipt.from_state(state)

    state = json.loads(json.dumps(_receipt().to_state()))
    state["endpoints"]["gateway"]["host"] = "evil.example"
    with pytest.raises(InvalidInputError):
        AuthCustodyReceipt.from_state(state)

    state = json.loads(json.dumps(_receipt().to_state()))
    state["extra"] = "field"
    with pytest.raises(InvalidInputError):
        AuthCustodyReceipt.from_state(state)


def test_token_metadata_must_be_consistent() -> None:
    with pytest.raises(InvalidInputError):
        TokenFileMetadata("/t", True, True, False, None, None)
    with pytest.raises(InvalidInputError):
        TokenFileMetadata("/t", False, False, False, "0600", 1)
    with pytest.raises(InvalidInputError):
        TokenFileMetadata("/t", True, True, False, "600", 1)
