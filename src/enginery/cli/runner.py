"""``enginery runner``: build, pin and qualify the isolated worker image.

``build-image`` builds the pinned worker image from the packaged recipe on a qualified engine and
prints its exact id; that id is what a run pins with ``stage1 build-request --runner-image``.
``status`` reports whether an engine, image and profile are admitted for isolated runs, with the
named hold when they are not. Neither command runs a worker or touches a ledger.
``build-relay-image`` and ``relay up|down|status`` (in ``runner_relay``) build and run the
model-access relay that holds the gateway credential so a worker never does.
``custody-receipt`` writes a retained receipt of where model credentials live on this host; it
records metadata about the gateway token file and never reads the token.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.image import build_image
from enginery.cli._exit_codes import exit_code_for
from enginery.cli.runner_relay import add_relay_parsers, run_relay_command
from enginery.domain.custody import (
    CustodyHost,
    TokenFileMetadata,
    build_custody_receipt,
)
from enginery.domain.errors import (
    FailureClass,
    HumanActionRequiredError,
    InvalidInputError,
    MissingPrerequisiteError,
)
from enginery.domain.runner import RunnerProfile, RunnerSelection


def add_runner_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``runner``'s subcommands on the root subparsers."""
    parser = subparsers.add_parser(
        "runner", help="Build and qualify the isolated worker image and engine."
    )
    runner_subparsers = parser.add_subparsers(dest="runner_command")
    build = runner_subparsers.add_parser(
        "build-image", help="Build the pinned worker image and print its exact id."
    )
    build.add_argument("--docker-context", default=None, help="Docker context of the engine.")
    build.add_argument("--tag", default=None, help="Optional convenience tag; runs pin the id.")
    build.add_argument("--no-cache", action="store_true", help="Rebuild every layer.")
    status = runner_subparsers.add_parser(
        "status", help="Report whether an engine and pinned image are admitted for isolated runs."
    )
    status.add_argument("--image", required=True, metavar="sha256:ID")
    status.add_argument("--docker-context", default=None, help="Docker context of the engine.")
    custody = runner_subparsers.add_parser(
        "custody-receipt",
        help="Record where model credentials live on this host (metadata only, never the token).",
    )
    custody.add_argument("--host", required=True, choices=[host.value for host in CustodyHost])
    custody.add_argument("--gateway-url", required=True)
    custody.add_argument("--broker-url", required=True)
    custody.add_argument(
        "--token-file",
        required=True,
        type=Path,
        help="Gateway token file; only its existence, type, mode and size are recorded.",
    )
    custody.add_argument("--output", required=True, type=Path, help="Receipt file to create.")
    custody.add_argument(
        "--decision",
        action="append",
        default=None,
        metavar="LABEL",
        help="Owner decision reference (date: label); repeatable. Defaults to the recorded set.",
    )
    add_relay_parsers(runner_subparsers)


def run_runner(args: argparse.Namespace) -> int:
    """Run one ``runner`` subcommand and print a machine-readable result."""
    command = args.runner_command
    if command == "build-image":
        return _build_image(args)
    if command == "status":
        return _status(args)
    if command == "custody-receipt":
        return _custody_receipt(args)
    if command in ("build-relay-image", "relay"):
        return run_relay_command(args)
    raise HumanActionRequiredError(
        "runner requires a subcommand: build-image, build-relay-image, relay, status or "
        "custody-receipt"
    )


def _build_image(args: argparse.Namespace) -> int:
    engine = OciEngine(context=args.docker_context)
    identity = engine.qualify()
    image = build_image(engine, tag=args.tag, no_cache=args.no_cache)
    print(
        json.dumps(
            {"engine": identity.to_state(), "image": image.to_state()},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    profile = RunnerProfile()
    selection = RunnerSelection.isolated(
        image=args.image, profile=profile, docker_context=args.docker_context
    )
    decision = OciRunnerAdmission(profile=profile).admit(selection)
    report: dict[str, object] = {
        "admitted": decision.hold is None,
        "profile": profile.name,
        "profile_digest": str(profile.digest),
        "hold": None if decision.hold is None else decision.hold.to_state(),
        "engine": None if decision.engine is None else decision.engine.to_state(),
        "image": None if decision.image is None else decision.image.to_state(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if decision.hold is None else 5


def _custody_receipt(args: argparse.Namespace) -> int:
    token_file = _inspect_token_file(args.token_file)
    receipt = build_custody_receipt(
        host=CustodyHost(args.host),
        gateway_url=args.gateway_url,
        broker_url=args.broker_url,
        token_file=token_file,
        created_at=datetime.now(UTC),
        decisions=args.decision or (),
    )
    canonical = json.dumps(receipt.to_state(), sort_keys=True, separators=(",", ":"))
    _write_new_file(args.output, canonical.encode("utf-8"))
    print(
        json.dumps(
            {
                "output": str(args.output),
                "digest": str(receipt.digest),
                "conforms": receipt.conforms,
                "findings": list(receipt.findings),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if receipt.conforms else exit_code_for(FailureClass.VALIDATION_FAILURE)


def _inspect_token_file(path: Path) -> TokenFileMetadata:
    """Describe the token file from its inode alone: it is never opened or read."""
    symlink = path.is_symlink()
    try:
        info = path.stat()
    except FileNotFoundError:
        return TokenFileMetadata(
            path=str(path),
            exists=False,
            regular_file=False,
            symlink=symlink,
            mode=None,
            size_bytes=None,
        )
    except OSError as error:
        raise MissingPrerequisiteError(f"the token file cannot be inspected: {error}") from error
    return TokenFileMetadata(
        path=str(path),
        exists=True,
        regular_file=stat.S_ISREG(info.st_mode),
        symlink=symlink,
        mode=f"{stat.S_IMODE(info.st_mode):04o}",
        size_bytes=info.st_size,
    )


def _write_new_file(path: Path, data: bytes) -> None:
    """Atomically create ``path`` (mode 0644, not secret); an existing receipt is never replaced."""
    if path.exists() or path.is_symlink():
        raise InvalidInputError(f"receipt output {path} already exists; receipts are not replaced")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.link(temporary, path)
    except FileExistsError as error:
        raise InvalidInputError(f"receipt output {path} already exists") from error
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


__all__ = ["add_runner_parser", "run_runner"]
