"""Enginery CLI entry point.

Implements only ``--version`` and ``doctor`` for this milestone
(03_SYSTEM_DESIGN.md §23.1). Later milestones add the ``work``, ``run``,
``evidence``, ``workflow``, ``factory-change``, ``adapter``, ``policy``, and
``gc`` command families.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from importlib import metadata

from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.cli.doctor import run_doctor
from enginery.domain.errors import EngineryError, FailureClass

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return exit_code_for(FailureClass.INVALID_INPUT)

    try:
        if args.command == "doctor":
            return _run_doctor(as_json=args.json)
    except EngineryError as error:
        print(str(error), file=sys.stderr)
        return exit_code_for(error.failure_class)

    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
