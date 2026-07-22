"""``enginery workspace``: inspect and release run-scoped workspace reservations.

``inspect`` is read-only over the same durable reservation state
``CoordinatorRuntime.release_workspace`` already reads
(:meth:`enginery.engine.runtime.CoordinatorRuntime.list_workspace_reservations`).
``release`` calls ``release_workspace`` directly and lets its
fenced-proof checks -- a reservation must exist, belong to the given
run, and carry status ``"retained"`` (never a live-leased
``"materialized"`` workspace) -- raise unmodified; this command adds no
weaker CLI-only check of its own. ``--dry-run`` previews the same
reservation the release call would see, through the same read path,
without releasing anything.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from enginery.cli._exit_codes import SUCCESS
from enginery.domain.errors import InvalidInputError
from enginery.engine.runtime import CoordinatorRuntime
from enginery.engine.workspace import WorkspaceReservation
from enginery.ledger.service import LedgerService

_HEARTBEAT_WINDOW = timedelta(seconds=60)


def run_workspace(args: argparse.Namespace) -> int:
    """Run one ``workspace`` command and emit a machine-readable result."""
    command = args.workspace_command
    if command is None:
        raise InvalidInputError("workspace requires a subcommand")
    if command == "inspect":
        return _inspect(args)
    if command == "release":
        return _release(args)
    raise AssertionError(f"unhandled workspace command: {command}")  # pragma: no cover


def _inspect(args: argparse.Namespace) -> int:
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        reservations = runtime.list_workspace_reservations()
    finally:
        ledger.close()
    _print_reservations(reservations, as_json=args.json)
    return SUCCESS


def _release(args: argparse.Namespace) -> int:
    ledger = LedgerService.open(args.database)
    try:
        runtime = CoordinatorRuntime(ledger, owner=args.owner)
        reservation = runtime.read_workspace_reservation(args.repository_id)
        if args.dry_run:
            _print_dry_run(reservation, run_id=args.run_id)
            return SUCCESS
        epoch = runtime.claim_epoch(now=_now(), heartbeat_window=_HEARTBEAT_WINDOW)
        released = runtime.release_workspace(
            run_id=args.run_id,
            repository_id=args.repository_id,
            epoch=epoch.epoch,
            now=_now(),
        )
    finally:
        ledger.close()
    _print_reservation(released, as_json=args.json)
    return SUCCESS


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _reservation_payload(reservation: WorkspaceReservation) -> dict[str, object]:
    return {
        "repository_id": reservation.repository_id,
        "run_id": reservation.run_id,
        "repository_path": str(reservation.repository_path),
        "workspace_path": str(reservation.workspace_path),
        "base_revision": reservation.base_revision,
        "status": reservation.status,
    }


def _print_reservations(reservations: tuple[WorkspaceReservation, ...], *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                [_reservation_payload(reservation) for reservation in reservations], indent=2
            )
        )
        return
    if not reservations:
        print("no workspace reservations recorded")
        return
    for reservation in reservations:
        print(
            f"[{reservation.status}] {reservation.repository_id} run={reservation.run_id} "
            f"workspace={reservation.workspace_path}"
        )


def _print_reservation(reservation: WorkspaceReservation, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(_reservation_payload(reservation), indent=2))
        return
    print(f"[{reservation.status}] {reservation.repository_id} run={reservation.run_id} released")


def _print_dry_run(reservation: WorkspaceReservation | None, *, run_id: str) -> None:
    if reservation is None:
        print(
            json.dumps(
                {"would_release": False, "reason": "no reservation found for this repository_id"}
            )
        )
        return
    would_release = reservation.run_id == run_id and reservation.status == "retained"
    payload = {
        "would_release": would_release,
        "reservation": _reservation_payload(reservation),
    }
    if not would_release:
        if reservation.run_id != run_id:
            payload["reason"] = "reservation belongs to a different run_id"
        else:
            payload["reason"] = (
                f"reservation status is {reservation.status!r}, not 'retained' "
                "(release requires no live lease)"
            )
    print(json.dumps(payload, indent=2, sort_keys=True))


__all__ = ["run_workspace"]
