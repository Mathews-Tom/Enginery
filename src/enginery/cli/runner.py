"""``enginery runner``: build, pin and qualify the isolated worker image.

``build-image`` builds the pinned worker image from the packaged recipe on a qualified engine and
prints its exact id; that id is what a run pins with ``stage1 build-request --runner-image``.
``status`` reports whether an engine, image and profile are admitted for isolated runs, with the
named hold when they are not. Neither command runs a worker or touches a ledger.
"""

from __future__ import annotations

import argparse
import json

from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.image import build_image
from enginery.domain.errors import HumanActionRequiredError
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


def run_runner(args: argparse.Namespace) -> int:
    """Run one ``runner`` subcommand and print a machine-readable result."""
    command = args.runner_command
    if command == "build-image":
        return _build_image(args)
    if command == "status":
        return _status(args)
    raise HumanActionRequiredError("runner requires a subcommand: build-image or status")


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


__all__ = ["add_runner_parser", "run_runner"]
