"""Observe relay wait validation through the candidate CLI and manager, offline.

The admission boundary deliberately refuses every engine access. No Docker command,
real credential, relay, model endpoint or worker is used by this observer.
"""

# ruff: noqa: E402 -- candidate metadata and import path must precede candidate imports.
from __future__ import annotations

import io
import sys
import tempfile
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Never, cast
from unittest.mock import patch

CHECKOUT = Path(__file__).resolve().parents[2]
_metadata = tempfile.TemporaryDirectory(prefix="enginery-relay-metadata-")
_project = tomllib.loads((CHECKOUT / "pyproject.toml").read_text())["project"]
_dist_info = Path(_metadata.name) / "enginery.dist-info"
_dist_info.mkdir()
_dist_info.joinpath("METADATA").write_text(
    f"Metadata-Version: 2.1\nName: {_project['name']}\nVersion: {_project['version']}\n",
    encoding="utf-8",
)
sys.path[:0] = [str(CHECKOUT / "src"), _metadata.name]

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.relay import RelayManager, check_token_file
from enginery.cli.main import main
from enginery.domain.errors import InvalidInputError, MissingPrerequisiteError

_INVALID = ("nan", "inf", "-inf", "-1")
_VALID = ("0", "-0.0", "0.01", "30")
_IMAGE = "sha256:" + "c" * 64
_UPSTREAM = "http://host.docker.internal:47400"
_BOUNDARY = "offline observer engine admission boundary"


class AdmissionBoundary:
    """Record admission, but never execute an engine command."""

    def __init__(self) -> None:
        self.calls = 0

    def qualify(self) -> Never:
        self.calls += 1
        raise MissingPrerequisiteError(_BOUNDARY)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise AssertionError(reason)


def forbidden_sleep(_seconds: float) -> Never:
    raise AssertionError("duration validation reached health polling")


def invoke(token: Path, duration: str, engine: AdmissionBoundary) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        patch("enginery.cli.runner_relay.OciEngine", return_value=engine),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        code = main(
            [
                "runner",
                "relay",
                "up",
                "--relay-image",
                _IMAGE,
                "--upstream",
                _UPSTREAM,
                "--token-file",
                str(token),
                f"--wait-seconds={duration}",
            ]
        )
    return code, stdout.getvalue(), stderr.getvalue()


def run() -> int:
    case = "fixture"
    try:
        with tempfile.TemporaryDirectory(prefix="enginery-relay-wait-") as directory:
            token = Path(directory) / "dummy-token"
            token.write_text("not-a-real-credential\n", encoding="utf-8")
            token.chmod(0o600)
            for duration in _INVALID:
                case = f"CLI {duration}"
                engine = AdmissionBoundary()
                with patch(
                    "enginery.adapters.oci.relay.check_token_file", wraps=check_token_file
                ) as token_inspection:
                    code, stdout, stderr = invoke(token, duration, engine)
                require(code == 2, f"invalid wait returned {code}: {stderr}")
                require(stdout == "", "invalid wait produced success output")
                require("Traceback" not in stderr, "invalid wait produced a traceback")
                require(token_inspection.call_count == 0, "invalid wait inspected the token file")
                require(engine.calls == 0, "invalid wait reached engine admission")
            print("PASS CLI invalid waits before effects")
            for duration in _INVALID:
                case = f"manager {duration}"
                engine = AdmissionBoundary()
                manager = RelayManager(cast(OciEngine, engine), sleep=forbidden_sleep)
                with patch(
                    "enginery.adapters.oci.relay.check_token_file", wraps=check_token_file
                ) as token_inspection:
                    try:
                        manager.up(
                            relay_image=_IMAGE,
                            upstream=_UPSTREAM,
                            token_file=token,
                            wait_seconds=float(duration),
                        )
                    except InvalidInputError:
                        pass
                    else:
                        raise AssertionError("manager accepted an invalid wait")
                require(token_inspection.call_count == 0, "manager inspected the token file")
                require(engine.calls == 0, "manager reached engine admission")
            print("PASS manager invalid waits before effects")
            for duration in _VALID:
                case = f"valid CLI {duration}"
                engine = AdmissionBoundary()
                code, stdout, stderr = invoke(token, duration, engine)
                require(code == 3, f"valid wait did not reach missing-prerequisite exit: {code}")
                require(stdout == "", "refused engine admission produced success output")
                require(stderr == _BOUNDARY + "\n", "valid wait failed before engine admission")
                require(engine.calls == 1, "valid wait did not reach engine admission exactly once")
            print("PASS finite nonnegative waits reach engine admission")
        print("relay-wait: ok")
        return 0
    except Exception as error:
        print(f"FAIL {case}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
