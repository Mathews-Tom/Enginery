"""``enginery runner build-relay-image`` and ``enginery runner relay up|down|status``.

The relay is the operator-owned container that holds the model gateway bearer so that a worker
never does. ``build-relay-image`` builds its pinned image from the packaged recipe and prints the
exact image id; ``relay up`` runs it, attached to the runner's internal network and to an egress
network, with the operator's token file mounted read-only. ``relay status`` is the same health
check admission applies to a run that asked for model access. None of these commands reads the
token file's content or runs a worker.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.image import build_relay_image
from enginery.adapters.oci.relay import RelayManager, RelayStatus
from enginery.domain.errors import HumanActionRequiredError

_UNHEALTHY_EXIT = 5
"""The exit code ``runner status`` already uses for a hold; an unhealthy relay is the same."""


def add_relay_parsers(
    runner_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the relay commands under ``runner``."""
    build = runner_subparsers.add_parser(
        "build-relay-image",
        help="Build the pinned model-access relay image and print its exact id.",
    )
    build.add_argument("--docker-context", default=None, help="Docker context of the engine.")
    build.add_argument(
        "--tag", default=None, help="Optional convenience tag; relay up pins the id."
    )
    build.add_argument("--no-cache", action="store_true", help="Rebuild every layer.")

    relay = runner_subparsers.add_parser(
        "relay",
        help="Run, inspect and remove the model-access relay that holds the gateway bearer.",
    )
    relay_subparsers = relay.add_subparsers(dest="relay_command")
    up = relay_subparsers.add_parser(
        "up", help="Start the relay (or confirm the running one) and wait until it is healthy."
    )
    up.add_argument("--relay-image", required=True, metavar="sha256:ID")
    up.add_argument(
        "--upstream",
        required=True,
        metavar="URL",
        help="Model gateway base URL as the relay container reaches it, e.g. "
        "http://host.docker.internal:18791 (that name is mapped to the host for you; any other "
        "host must be resolvable from inside a container, an IP address always is).",
    )
    up.add_argument(
        "--token-file",
        required=True,
        type=Path,
        help="Absolute path of the gateway token file (a regular file you own, mode 0600). It is "
        "mounted read-only into the relay only; this command never reads it.",
    )
    up.add_argument(
        "--recreate",
        action="store_true",
        help="Replace a running relay even if it matches (after regenerating the gateway token).",
    )
    up.add_argument("--wait-seconds", type=float, default=30.0)
    up.add_argument("--docker-context", default=None, help="Docker context of the engine.")
    down = relay_subparsers.add_parser("down", help="Remove the relay container.")
    down.add_argument("--docker-context", default=None, help="Docker context of the engine.")
    status = relay_subparsers.add_parser(
        "status", help="Report whether the relay is present, hardened and healthy."
    )
    status.add_argument("--docker-context", default=None, help="Docker context of the engine.")


def run_relay_command(args: argparse.Namespace) -> int:
    """Run ``build-relay-image`` or one ``relay`` subcommand and print a JSON result."""
    if args.runner_command == "build-relay-image":
        return _build(args)
    command = args.relay_command
    if command not in ("up", "down", "status"):
        raise HumanActionRequiredError("runner relay requires a subcommand: up, down or status")
    manager = RelayManager(OciEngine(context=args.docker_context))
    if command == "up":
        return _report(
            manager.up(
                relay_image=args.relay_image,
                upstream=args.upstream,
                token_file=args.token_file,
                recreate=args.recreate,
                wait_seconds=args.wait_seconds,
            )
        )
    if command == "down":
        return _report(manager.down(), healthy_required=False)
    return _report(manager.status())


def _build(args: argparse.Namespace) -> int:
    engine = OciEngine(context=args.docker_context)
    identity = engine.qualify()
    started = time.monotonic()
    image = build_relay_image(engine, tag=args.tag, no_cache=args.no_cache)
    print(
        json.dumps(
            {
                "engine": identity.to_state(),
                "image": image.to_state(),
                "build_seconds": round(time.monotonic() - started, 1),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _report(status: RelayStatus, *, healthy_required: bool = True) -> int:
    print(json.dumps(status.to_state(), indent=2, sort_keys=True))
    return 0 if status.healthy or not healthy_required else _UNHEALTHY_EXIT


__all__ = ["add_relay_parsers", "run_relay_command"]
