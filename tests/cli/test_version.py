from __future__ import annotations

from importlib import metadata

import pytest

from enginery.cli.main import main


def test_version_flag_exits_zero_and_prints_installed_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert metadata.version("enginery") in captured.out


def test_installed_version_matches_the_canonical_release() -> None:
    assert metadata.version("enginery") == "0.3.0"
