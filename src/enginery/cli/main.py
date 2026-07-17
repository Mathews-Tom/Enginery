"""Enginery CLI entry point.

Implements ``--version``, ``doctor``, and the ``ledger`` verify/backup/
restore/rebuild-projections command family for this milestone. Later
milestones add the ``work``, ``run``, ``evidence``, ``workflow``,
``factory-change``, ``adapter``, ``policy``, and ``gc`` command families.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.cli.doctor import run_doctor
from enginery.cli.ledger import (
    run_backup,
    run_rebuild_projections,
    run_restore,
    run_verify,
)
from enginery.domain.errors import EngineryError, FailureClass, InvalidInputError

_DISTRIBUTION = "enginery"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enginery", description="Enginery control-plane CLI.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {metadata.version(_DISTRIBUTION)}",
    )
    subparsers = parser.add_subparsers(dest="command")

    doctor_parser = subparsers.add_parser(
        "doctor", help="Report locally implemented prerequisites."
    )
    doctor_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    ledger_parser = subparsers.add_parser("ledger", help="Ledger consistency and storage commands.")
    ledger_subparsers = ledger_parser.add_subparsers(dest="ledger_command")

    verify_parser = ledger_subparsers.add_parser(
        "verify", help="Check ledger and artifact-store consistency."
    )
    verify_parser.add_argument("--database", required=True, type=Path)
    verify_parser.add_argument("--artifacts", type=Path, default=None)
    verify_parser.add_argument("--json", action="store_true")

    backup_parser = ledger_subparsers.add_parser("backup", help="Snapshot a ledger to a directory.")
    backup_parser.add_argument("--database", required=True, type=Path)
    backup_parser.add_argument("--output", required=True, type=Path)
    backup_parser.add_argument("--artifacts", type=Path, default=None)

    restore_parser = ledger_subparsers.add_parser(
        "restore", help="Restore a ledger from a backup directory."
    )
    restore_parser.add_argument("--backup", required=True, type=Path)
    restore_parser.add_argument("--database", required=True, type=Path)
    restore_parser.add_argument("--artifacts", type=Path, default=None)

    rebuild_parser = ledger_subparsers.add_parser(
        "rebuild-projections", help="Rebuild ledger projections from stored events."
    )
    rebuild_parser.add_argument("--database", required=True, type=Path)

    return parser


def _run_doctor(*, as_json: bool) -> int:
    report = run_doctor()
    if as_json:
        payload = {
            "ok": report.ok,
            "checks": [
                {"name": check.name, "ok": check.ok, "detail": check.detail}
                for check in report.checks
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        for check in report.checks:
            status = "ok" if check.ok else "fail"
            print(f"[{status}] {check.name}: {check.detail}")
    return SUCCESS if report.ok else exit_code_for(FailureClass.MISSING_PREREQUISITE)


def _run_ledger_verify(args: argparse.Namespace) -> int:
    report = run_verify(database=args.database, artifacts=args.artifacts)
    if args.json:
        payload = {
            "healthy": report.healthy,
            "schema_version": report.schema_version,
            "issues": [{"code": issue.code, "detail": issue.detail} for issue in report.issues],
        }
        print(json.dumps(payload, indent=2))
    elif report.healthy:
        print("healthy")
    else:
        for issue in report.issues:
            print(f"[{issue.code}] {issue.detail}", file=sys.stderr)
        print("unhealthy")
    return SUCCESS if report.healthy else exit_code_for(FailureClass.VALIDATION_FAILURE)


def _run_ledger_backup(args: argparse.Namespace) -> int:
    manifest = run_backup(database=args.database, output=args.output, artifacts=args.artifacts)
    print(f"backup written to {args.output} (schema version {manifest.schema_version})")
    return SUCCESS


def _run_ledger_restore(args: argparse.Namespace) -> int:
    manifest = run_restore(backup=args.backup, database=args.database, artifacts=args.artifacts)
    print(f"restored {args.database} (schema version {manifest.schema_version})")
    return SUCCESS


def _run_ledger_rebuild_projections(args: argparse.Namespace) -> int:
    report = run_rebuild_projections(database=args.database)
    print(f"rebuilt {report.aggregates_rebuilt} projection(s)")
    return SUCCESS


def _run_ledger(args: argparse.Namespace) -> int:
    if args.ledger_command is None:
        raise InvalidInputError("a ledger subcommand is required", details={"command": "ledger"})
    if args.ledger_command == "verify":
        return _run_ledger_verify(args)
    if args.ledger_command == "backup":
        return _run_ledger_backup(args)
    if args.ledger_command == "restore":
        return _run_ledger_restore(args)
    if args.ledger_command == "rebuild-projections":
        return _run_ledger_rebuild_projections(args)
    raise AssertionError(f"unhandled ledger command: {args.ledger_command}")  # pragma: no cover


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return exit_code_for(FailureClass.INVALID_INPUT)

    try:
        if args.command == "doctor":
            return _run_doctor(as_json=args.json)
        if args.command == "ledger":
            return _run_ledger(args)
    except EngineryError as error:
        print(str(error), file=sys.stderr)
        return exit_code_for(error.failure_class)

    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
