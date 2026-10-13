from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from enginery.cli.main import main
from enginery.domain.custody import AuthCustodyReceipt

_SECRET = "sk-test-secret-token-value-0123456789"


def _args(token: Path, output: Path, *extra: str, host: str = "macos") -> list[str]:
    return [
        "runner",
        "custody-receipt",
        "--host",
        host,
        "--gateway-url",
        "http://127.0.0.1:7011",
        "--broker-url",
        "http://127.0.0.1:7012",
        "--token-file",
        str(token),
        "--output",
        str(output),
        *extra,
    ]


def _token(tmp_path: Path, mode: int = 0o600) -> Path:
    token = tmp_path / "gateway.token"
    token.write_text(_SECRET, encoding="utf-8")
    token.chmod(mode)
    return token


def test_custody_receipt_writes_canonical_json_whose_sha256_is_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "out" / "receipt.json"

    code = main(_args(_token(tmp_path), output, "--decision", "2026-10-13: approved"))

    printed = json.loads(capsys.readouterr().out)
    data = output.read_bytes()
    assert code == 0
    assert printed["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert printed["conforms"] is True
    receipt = AuthCustodyReceipt.from_state(json.loads(data))
    assert receipt.decisions == ("2026-10-13: approved",)
    assert str(receipt.digest) == printed["digest"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert data == json.dumps(json.loads(data), sort_keys=True, separators=(",", ":")).encode()


def test_custody_receipt_never_contains_the_token_or_a_hash_of_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "receipt.json"

    main(_args(_token(tmp_path), output))

    text = output.read_text(encoding="utf-8") + capsys.readouterr().out
    assert _SECRET not in text
    assert hashlib.sha256(_SECRET.encode()).hexdigest() not in text
    token_state = json.loads(output.read_text())["custody"]["token_file"]
    assert token_state["size_bytes"] == len(_SECRET)
    assert token_state["mode"] == "0600"


def test_custody_receipt_does_not_open_the_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _token(tmp_path)
    attempts: list[str] = []

    def guard(real: Callable[..., Any]) -> Callable[..., Any]:
        def guarded(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(path) == str(token):
                attempts.append(str(path))
            return real(path, *args, **kwargs)

        return guarded

    monkeypatch.setattr(os, "open", guard(os.open))
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))

    assert main(_args(token, tmp_path / "receipt.json")) == 0
    assert attempts == []


def test_lima_receipt_with_a_loose_token_copy_is_written_but_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "receipt.json"

    code = main(_args(_token(tmp_path, 0o644), output, host="ubuntu-lima"))

    printed = json.loads(capsys.readouterr().out)
    assert code == 12
    assert printed["conforms"] is False
    assert "expected 0600" in printed["findings"][0]
    assert json.loads(output.read_text())["conforms"] is False


def test_missing_token_file_is_recorded_as_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "receipt.json"

    code = main(_args(tmp_path / "absent.token", output))

    assert code == 12
    state = json.loads(output.read_text())
    assert state["custody"]["token_file"]["exists"] is False
    assert json.loads(capsys.readouterr().out)["conforms"] is False


def test_custody_receipt_refuses_credentialed_urls_and_existing_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = _token(tmp_path)
    output = tmp_path / "receipt.json"
    bad = _args(token, output)
    bad[bad.index("--gateway-url") + 1] = "http://user:hunter2@127.0.0.1:7011"

    assert main(bad) == 2
    assert not output.exists()
    assert "hunter2" not in capsys.readouterr().out

    assert main(_args(token, output)) == 0
    first = output.read_bytes()
    assert main(_args(token, output)) == 2
    assert output.read_bytes() == first
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".")] == []
