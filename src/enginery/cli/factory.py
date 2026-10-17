"""``enginery factory``: guided project setup and prerequisite diagnostics.

* ``init`` previews (default) or, with ``--apply``, performs guided setup: a project policy
  file and a private ownership record, nothing else (see :mod:`enginery.cli.factory_setup`).
* ``doctor`` reports the prerequisites the project's compiled selection needs, read-only.
* ``explain`` shows, per compiled field, the winning layer, the shadowed values, the hard
  constraints and the source files, plus the setup-owned files and the offline holds.
* ``uninstall`` previews (default) or, with ``--apply``, removes exactly the files setup
  created that are still as setup wrote them.

Exit codes reuse the CLI contract: 0 when nothing is held, 5 (human action required) when
a prerequisite or capability is held, 9 (external conflict) when a user-owned file would
have to be overwritten, 2 for invalid input or configuration.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from enginery.application.layered_config import RUNNER_MODES
from enginery.application.project_policy import SetupChoices
from enginery.capabilities.manifest import validate_name, validate_version
from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.cli.factory_behavioral import run_command as run_behavioral_command
from enginery.cli.factory_collect import collect, document_lines
from enginery.cli.factory_control import drain, pause
from enginery.cli.factory_diagnostics import DoctorReport, run_doctor
from enginery.cli.factory_explain import explain, explain_lines
from enginery.cli.factory_run import (
    factory_repairs,
    nonnegative_seconds,
    resume_run,
    run_issue,
    status,
)
from enginery.cli.factory_setup import (
    SetupPlan,
    apply_plan,
    load_project_configuration,
    plan_init,
    resolve_project,
)
from enginery.cli.factory_uninstall import NEVER_TOUCHED, apply_uninstall, plan_uninstall
from enginery.domain.errors import FailureClass, InvalidInputError
from enginery.domain.runner import MODEL_ACCESS_MODES

HOLD_EXIT = exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
CONFLICT_EXIT = exit_code_for(FailureClass.EXTERNAL_CONFLICT)


def add_factory_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``factory``'s subcommands on the root subparsers."""
    parser = subparsers.add_parser(
        "factory", help="Guided project setup and prerequisite diagnostics."
    )
    commands = parser.add_subparsers(dest="factory_command")

    init = commands.add_parser(
        "init",
        help="Preview (default) or apply guided setup: a project policy file and a private "
        "ownership record.",
    )
    _add_project(init)
    init.add_argument(
        "--apply",
        action="store_true",
        help="Perform exactly the previewed plan; default is to preview and write nothing.",
    )
    init.add_argument(
        "--runner",
        required=True,
        choices=RUNNER_MODES,
        help="The trust mode runs use; it is never defaulted.",
    )
    init.add_argument("--profile", default=None, help="Packaged workflow profile to select.")
    init.add_argument("--harness", default=None, choices=("omp", "claude-code"))
    init.add_argument("--model", default=None, help="A configured default model (not a pin).")
    init.add_argument("--effort", default=None, help="A configured default effort (not a pin).")
    init.add_argument("--runner-image", default=None, metavar="sha256:ID")
    init.add_argument("--model-access", default=None, choices=MODEL_ACCESS_MODES)
    init.add_argument(
        "--validation-command", dest="validation_commands", action="append", default=[]
    )
    init.add_argument("--required-check", dest="required_checks", action="append", default=[])
    init.add_argument("--protected-path", dest="protected_paths", action="append", default=[])
    init.add_argument(
        "--capability",
        dest="capabilities",
        action="append",
        default=[],
        metavar="NAME[@VERSION]",
        help="A capability the project permits; init never installs it, it names the "
        "`enginery capability` commands that do.",
    )
    init.add_argument("--json", action="store_true", help="Emit canonical JSON.")

    doctor = commands.add_parser(
        "doctor", help="Report the prerequisites the project's selection needs; read-only."
    )
    _add_project(doctor)
    doctor.add_argument("--json", action="store_true", help="Emit canonical JSON.")

    collect_parser = commands.add_parser("collect", help="Preview or record GitHub issue intake.")
    _add_project(collect_parser)
    collect_parser.add_argument("--dry-run", action="store_true")
    collect_parser.add_argument("--json", action="store_true", help="Emit canonical JSON.")

    run = commands.add_parser("run", help="Bind an issue and advance its durable Stage 1 run.")
    _add_project(run)
    run.add_argument("issue", metavar="OWNER/REPO#N")
    run.add_argument("--once", action="store_true")
    run.add_argument("--wait-seconds", type=nonnegative_seconds, default=0)
    run.add_argument("--supersede", action="store_true")
    run.add_argument("--base-branch", default=None)
    run.add_argument("--model", default=None)
    run.add_argument("--effort", default=None)
    run.add_argument("--runner", choices=RUNNER_MODES, default=None)
    run.add_argument("--profile", default=None)
    run.add_argument("--validation-command", dest="validation_commands", action="append")
    run.add_argument("--required-check", dest="required_checks", action="append")
    run.add_argument("--protected-path", dest="protected_paths", action="append")
    run.add_argument("--time-budget-seconds", type=int, default=None)
    run.add_argument("--json", action="store_true")

    status_parser = commands.add_parser("status", help="Inspect durable runs without advancing.")
    _add_project(status_parser)
    status_parser.add_argument("run_id", nargs="?")
    status_parser.add_argument("--json", action="store_true")

    for name, synopsis in (
        ("calibrate", "Measure behavioral evaluator fault sensitivity with deliberate faults."),
        ("check", "Capture current behavior and record its evaluated acceptance verdict."),
    ):
        behavioral = commands.add_parser(name, help=synopsis, description=synopsis)
        _add_project(behavioral)
        behavioral.add_argument("run_id")
        behavioral.add_argument("--database", type=Path, default=None)
        behavioral.add_argument("--owner", default="factory")
        behavioral.add_argument("--json", action="store_true")
        if name == "calibrate":
            behavioral.add_argument(
                "--without-fault",
                action="append",
                default=[],
                choices=("constant_result", "skipped_persistence", "wrong_build"),
                help="Record a deliberately uninjected fault; this cannot qualify acceptance.",
            )

    resume = commands.add_parser("resume", help="Reconcile workers and continue a durable run.")
    _add_project(resume)
    resume.add_argument("run_id")
    resume.add_argument("--wait-seconds", type=nonnegative_seconds, default=0)
    resume.add_argument("--json", action="store_true")

    pause_parser = commands.add_parser("pause", help="Pause project-ledger dispatch only.")
    _add_project(pause_parser)
    pause_parser.add_argument("--reason", default=None)
    pause_parser.add_argument("--lift", action="store_true")
    pause_parser.add_argument("--json", action="store_true")

    drain_parser = commands.add_parser(
        "drain", help="Pause, then wait for or stop project workers."
    )
    _add_project(drain_parser)
    drain_mode = drain_parser.add_mutually_exclusive_group(required=True)
    drain_mode.add_argument("--wait-seconds", type=nonnegative_seconds)
    drain_mode.add_argument("--stop", action="store_true")
    drain_parser.add_argument("--json", action="store_true")

    explain_parser = commands.add_parser(
        "explain",
        help="Show where every compiled field comes from, which files setup owns and what "
        "is held; read-only.",
    )
    _add_project(explain_parser)
    explain_parser.add_argument("run_id", nargs="?")
    explain_parser.add_argument("--json", action="store_true", help="Emit canonical JSON.")

    uninstall = commands.add_parser(
        "uninstall",
        help="Preview (default) or apply removal of exactly the unmodified files setup created.",
    )
    _add_project(uninstall)
    uninstall.add_argument(
        "--apply",
        action="store_true",
        help="Perform exactly the previewed removals; default is to preview and write nothing.",
    )
    uninstall.add_argument("--json", action="store_true", help="Emit canonical JSON.")


def _add_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="The project directory (default: the current directory).",
    )


def _emit(args: argparse.Namespace, payload: Mapping[str, object], lines: list[str]) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in lines:
            print(line)


def _capability_choices(values: list[str]) -> tuple[tuple[str, ...], dict[str, str]]:
    names: list[str] = []
    versions: dict[str, str] = {}
    for raw in values:
        name, separator, version = raw.partition("@")
        if not name.strip() or (separator and not version.strip()):
            raise InvalidInputError(
                "--capability must be NAME or NAME@VERSION", details={"capability": raw}
            )
        validate_name(name)
        if separator:
            validate_version(version)
        if name not in names:
            names.append(name)
        if separator:
            versions[name] = version
    return tuple(names), versions


def _plan_lines(plan: SetupPlan, *, apply: bool, performed: tuple[str, ...]) -> list[str]:
    lines = [f"{'apply' if apply else 'preview'} setup for {plan.project}"]
    for item in plan.files:
        lines.append(
            f"  {item.action.upper():9} {item.tracking:7} {item.path}\n            {item.detail}"
        )
    lines.append("  installs: none performed by init")
    for hold in plan.installs:
        lines.append(f"  HOLD {hold.name}: {hold.detail}")
        lines.extend(f"    run: {command}" for command in hold.commands)
    if plan.harness_probe_failure is not None:
        lines.append(f"  HOLD harness: {plan.harness_probe_failure}")
        lines.append(f"    resolve: {plan.harness_probe_resolve}")
    lines.append("  network access: " + ("; ".join(plan.network) if plan.network else "none"))
    for requirement in plan.auth:
        lines.append(
            f"  auth requirement {requirement['name']}: {requirement['detail']} "
            f"(resolve: {requirement['resolve']})"
        )
    lines.append(f"  effective configuration {plan.configuration_digest}")
    if plan.conflicts:
        lines.append("conflict: a user-owned file would be overwritten; nothing was written")
    elif apply and plan.harness_probe_failure is not None:
        lines.append("apply refused until the harness hold is resolved; nothing was written")
    elif apply and plan.harness_probe_failure is None:
        lines.append(
            "wrote: " + (", ".join(performed) if performed else "nothing (already set up)")
        )
    else:
        lines.append("nothing was written; pass --apply to perform exactly this plan")
    return lines


def _run_init(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    names, versions = _capability_choices(args.capabilities)
    choices = SetupChoices(
        runner_mode=args.runner,
        profile=args.profile,
        harness=args.harness,
        model=args.model,
        effort=args.effort,
        runner_image=args.runner_image,
        model_access=args.model_access,
        validation_commands=tuple(args.validation_commands),
        required_checks=tuple(args.required_checks),
        protected_paths=tuple(args.protected_paths),
        capabilities=names,
    )
    plan = plan_init(project, choices, capability_versions=versions)
    performed = apply_plan(plan) if args.apply else ()
    _emit(
        args,
        plan.to_json(applied=args.apply, performed=performed),
        _plan_lines(plan, apply=args.apply, performed=performed),
    )
    if plan.conflicts:
        return CONFLICT_EXIT
    return HOLD_EXIT if plan.installs or plan.harness_probe_failure else SUCCESS


def _doctor_lines(report: DoctorReport) -> list[str]:
    lines = [f"factory doctor for {report.project}"]
    for check in report.checks:
        lines.append(f"[{check.status}] {check.name}: {check.detail}")
        if check.resolve is not None and check.status == "hold":
            lines.append(f"       resolve: {check.resolve}")
    for note in report.network:
        lines.append(f"network access: {note}")
    lines.append("status: " + report.status)
    return lines


def _run_doctor(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    report = run_doctor(load_project_configuration(project))
    _emit(args, report.to_json(), _doctor_lines(report))
    return HOLD_EXIT if report.holds else SUCCESS


def _run_explain(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    if args.run_id is not None:
        document = status(project, args.run_id)
        _emit(args, document, document_lines(document))
        return HOLD_EXIT if document["holds"] else SUCCESS
    document = explain(load_project_configuration(project))
    _emit(args, document, explain_lines(document))
    return HOLD_EXIT if document["holds"] else SUCCESS


def _run_uninstall(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    plan = plan_uninstall(project)
    removed = apply_uninstall(plan) if args.apply else ()
    lines = [f"{'apply' if args.apply else 'preview'} uninstall for {plan.project}"]
    if not plan.has_record:
        lines.append("nothing to uninstall: setup has no ownership record for this project")
    for item in plan.outcomes:
        lines.append(f"  {item.action.upper():15} {item.path}\n                  {item.detail}")
    if plan.has_record:
        lines.append(f"  REMOVE          {plan.record_path} (the ownership record, last)")
    lines.append("never touched: " + ", ".join(NEVER_TOUCHED))
    lines.append(
        ("removed: " + (", ".join(removed) if removed else "nothing"))
        if args.apply
        else "nothing was removed; pass --apply to perform exactly this plan"
    )
    _emit(args, plan.to_json(applied=args.apply, removed=removed), lines)
    return SUCCESS


def run_factory(args: argparse.Namespace) -> int:
    command = args.factory_command
    document: Mapping[str, object]
    if command == "init":
        return _run_init(args)
    if command == "doctor":
        return _run_doctor(args)
    if command in {"calibrate", "check"}:
        document = run_behavioral_command(
            resolve_project(args.project),
            args.run_id,
            calibrate=command == "calibrate",
            without_faults=tuple(getattr(args, "without_fault", ())),
            database=args.database,
            owner=args.owner,
        )
        _emit(args, document, [json.dumps(document, sort_keys=True)])
        return (
            SUCCESS
            if document.get("qualified") is True or document.get("result") == "pass"
            else HOLD_EXIT
        )
    if command == "collect":
        project = resolve_project(args.project)
        collected = collect(project, dry_run=args.dry_run)
        holds = cast(list[dict[str, str]], collected["holds"])
        document = {**collected, "holds": factory_repairs(project, holds)}
        _emit(args, document, document_lines(document))
        return HOLD_EXIT if document["holds"] else SUCCESS
    if command == "resume":
        document = resume_run(resolve_project(args.project), args.run_id, args.wait_seconds)
        _emit(args, document, document_lines(document))
        return HOLD_EXIT if document.get("holds") else SUCCESS
    if command == "pause":
        document = pause(resolve_project(args.project), lift=args.lift, reason=args.reason)
        _emit(args, document, document_lines(document))
        return SUCCESS
    if command == "drain":
        document, code = drain(
            resolve_project(args.project), stop=args.stop, wait_seconds=args.wait_seconds
        )
        _emit(args, document, document_lines(document))
        return code
    if command in {"run", "status"}:
        project = resolve_project(args.project)
        document = run_issue(project, args) if command == "run" else status(project, args.run_id)
        _emit(args, document, document_lines(document))
        return HOLD_EXIT if document.get("holds") else SUCCESS
    if command == "explain":
        return _run_explain(args)
    if command == "uninstall":
        return _run_uninstall(args)
    raise InvalidInputError(
        "factory requires a subcommand; see enginery factory --help",
        details={"command": str(command)},
    )


__all__ = ["add_factory_parser", "run_factory"]
