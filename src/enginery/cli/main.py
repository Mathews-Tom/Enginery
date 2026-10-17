"""Enginery's project, host, engine-ledger and operator-ledger CLI."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path

from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.domain.errors import EngineryError, FailureClass, InvalidInputError

_DISTRIBUTION = "enginery"
DEFAULT_G4_FLOOR_CONFIG = Path(__file__).parent.parent / "config" / "gate-g4-floor.toml"


def _build_parser() -> argparse.ArgumentParser:
    from enginery.cli.capability import add_capability_parser
    from enginery.cli.factory import add_factory_parser
    from enginery.cli.operator import add_operator_parser
    from enginery.cli.runner import add_runner_parser

    parser = argparse.ArgumentParser(prog="enginery", description="Enginery control-plane CLI.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {metadata.version(_DISTRIBUTION)}",
    )
    subparsers = parser.add_subparsers(dest="command")

    doctor_parser = subparsers.add_parser(
        "doctor", help="Host: Report locally implemented prerequisites."
    )
    doctor_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    adapter_parser = subparsers.add_parser("adapter", help="Host: Inspect adapter providers.")
    adapter_subparsers = adapter_parser.add_subparsers(dest="adapter_command")
    adapter_doctor_parser = adapter_subparsers.add_parser(
        "doctor", help="Report deterministic local provider capabilities."
    )
    adapter_doctor_parser.add_argument("--json", action="store_true")
    adapter_doctor_parser.add_argument(
        "--github-repository", default=None, help="Defaults to installed package metadata."
    )
    adapter_doctor_parser.add_argument("--github-executable", default="gh")
    adapter_doctor_parser.add_argument(
        "--pypi-project-name", default=None, help="Defaults to installed package metadata."
    )
    adapter_doctor_parser.add_argument("--pypi-executable", default="uv")
    adapter_doctor_parser.add_argument("--deployment-app-script", type=Path, default=None)
    adapter_doctor_parser.add_argument("--deployment-artifacts-root", type=Path, default=None)
    adapter_doctor_parser.add_argument("--deployment-state-root", type=Path, default=None)
    adapter_doctor_parser.add_argument("--deployment-python-executable", default=None)

    policy_parser = subparsers.add_parser("policy", help="Policy: Explain policy decisions.")
    policy_subparsers = policy_parser.add_subparsers(dest="policy_command")
    explain_parser = policy_subparsers.add_parser(
        "explain",
        help="Explain a policy request without authorizing it.",
    )
    explain_parser.add_argument("request", type=Path)

    engine_parser = subparsers.add_parser(
        "engine",
        help="Engine ledger: Execute and inspect durable runs and stacks.",
        description="Engine ledger: Build requests and operate durable runs and stacks; "
        "run and stack commands take an explicit database and owner.",
    )
    engine_commands = engine_parser.add_subparsers(dest="engine_command")
    _add_lifecycle_parsers(engine_commands)
    _add_stack_status_parser(engine_commands)

    outcome_parser = subparsers.add_parser(
        "outcome", help="Engine ledger: Inspect outcome observations and completeness."
    )
    outcome_subparsers = outcome_parser.add_subparsers(dest="outcome_command")
    outcome_list_parser = outcome_subparsers.add_parser(
        "list", help="List registered observations, optionally filtered by state."
    )
    outcome_list_parser.add_argument("--database", required=True, type=Path)
    outcome_list_parser.add_argument(
        "--state", choices=("pending", "captured", "indeterminate"), default=None
    )
    outcome_show_parser = outcome_subparsers.add_parser(
        "show", help="Show one observation and its captured outcome, if any."
    )
    outcome_show_parser.add_argument("--database", required=True, type=Path)
    outcome_show_parser.add_argument("observation_id")
    outcome_completeness_parser = outcome_subparsers.add_parser(
        "completeness", help="Report the versioned outcome-capture completeness derivation."
    )
    outcome_completeness_parser.add_argument("--database", required=True, type=Path)
    outcome_interventions_parser = outcome_subparsers.add_parser(
        "interventions", help="List recorded human decisions for one run."
    )
    outcome_interventions_parser.add_argument("--database", required=True, type=Path)
    outcome_interventions_parser.add_argument("--run-id", required=True)
    outcome_failures_parser = outcome_subparsers.add_parser(
        "failures", help="List failed runtime nodes for one run."
    )
    outcome_failures_parser.add_argument("--database", required=True, type=Path)
    outcome_failures_parser.add_argument("--run-id", required=True)

    gate_parser = subparsers.add_parser(
        "gate", help="Engine ledger: Report decision-gate readiness."
    )
    gate_subparsers = gate_parser.add_subparsers(dest="gate_command")
    gate_status_parser = gate_subparsers.add_parser(
        "status", help="Report the current state of every condition for one registered gate."
    )
    gate_status_parser.add_argument("--gate", required=True, choices=("G4",))
    gate_status_parser.add_argument("--database", required=True, type=Path)
    gate_status_parser.add_argument("--floor-config", type=Path, default=DEFAULT_G4_FLOOR_CONFIG)
    gate_status_parser.add_argument("--json", action="store_true")
    gate_finding_parser = gate_subparsers.add_parser(
        "record-g4-deficiency",
        help="Record one recurring G4 deficiency against eligible classified runs.",
    )
    gate_finding_parser.add_argument("--database", required=True, type=Path)
    gate_finding_parser.add_argument("--finding-id", required=True)
    gate_finding_parser.add_argument("--deficiency", required=True)
    gate_finding_parser.add_argument(
        "--cited-run-id", dest="cited_run_ids", action="append", required=True
    )
    gate_finding_parser.add_argument("--evidence-pull-request-number", required=True, type=int)
    gate_finding_parser.add_argument("--producer-principal-id", required=True)
    gate_finding_parser.add_argument("--evidence-pull-request-author-login", required=True)
    gate_finding_parser.add_argument("--correlation-id", required=True)
    gate_finding_parser.add_argument("--floor-config", type=Path, default=DEFAULT_G4_FLOOR_CONFIG)
    gate_finding_parser.add_argument("--json", action="store_true")
    gate_evidence_parser = gate_subparsers.add_parser(
        "record-g4-deficiency-evidence",
        help="Verify and persist GitHub authority evidence for one G4 deficiency finding.",
    )
    gate_evidence_parser.add_argument("--database", required=True, type=Path)
    gate_evidence_parser.add_argument("--finding-id", required=True)
    gate_evidence_parser.add_argument("--correlation-id", required=True)
    gate_evidence_parser.add_argument("--github-repository", required=True)
    gate_evidence_parser.add_argument("--github-credential-reference", required=True)
    gate_evidence_parser.add_argument("--github-executable", default="gh")
    gate_evidence_parser.add_argument("--floor-config", type=Path, default=DEFAULT_G4_FLOOR_CONFIG)
    gate_evidence_parser.add_argument("--json", action="store_true")

    workspace_parser = subparsers.add_parser(
        "workspace", help="Engine ledger: Inspect and release workspace reservations."
    )
    workspace_subparsers = workspace_parser.add_subparsers(dest="workspace_command")
    workspace_inspect_parser = workspace_subparsers.add_parser(
        "inspect", help="List every repository's current workspace reservation."
    )
    workspace_inspect_parser.add_argument("--database", required=True, type=Path)
    workspace_inspect_parser.add_argument("--owner", required=True)
    workspace_inspect_parser.add_argument("--json", action="store_true")
    workspace_release_parser = workspace_subparsers.add_parser(
        "release",
        help="Release a retained workspace reservation with no live lease.",
    )
    workspace_release_parser.add_argument("--database", required=True, type=Path)
    workspace_release_parser.add_argument("--owner", required=True)
    workspace_release_parser.add_argument("--repository-id", required=True)
    workspace_release_parser.add_argument("--run-id", required=True)
    workspace_release_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report whether release would succeed without releasing anything.",
    )
    workspace_release_parser.add_argument("--json", action="store_true")

    add_runner_parser(subparsers)

    add_capability_parser(subparsers)
    add_factory_parser(subparsers)
    add_operator_parser(subparsers)

    explain_parser.add_argument("--json", action="store_true")

    for entry in subparsers._choices_actions:
        scope = str(entry.help).split(":", 1)[0]
        _describe_scope(subparsers.choices[entry.dest], scope, entry.help)

    return parser


def _describe_scope(parser: argparse.ArgumentParser, scope: str, synopsis: str | None) -> None:
    """Keep meaningful scope descriptions visible for groups and leaves."""
    if parser.description is None:
        parser.description = synopsis
    elif not parser.description.startswith(f"{scope}:"):
        parser.description = f"{scope}: {parser.description}"
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for entry in action._choices_actions:
                text = str(entry.help)
                if not text.startswith(f"{scope}:"):
                    text = f"{scope}: {text}"
                entry.help = text
                _describe_scope(action.choices[entry.dest], scope, text)


def _add_lifecycle_parsers(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    from enginery.cli.stage1_request import add_build_request_parser

    for command, synopsis in (
        ("start", "Start a durable run from a request document."),
        ("next", "Report the next durable-run action and optionally advance it."),
        ("review", "Ingest a review report for a durable run."),
        ("approve", "Authenticated human-wait decision; requires a project review challenge."),
        ("reject", "Authenticated human-wait decision; requires a project review challenge."),
        ("cancel", "Cancel one durable-run node."),
        ("evidence", "Report the evidence retained for a durable run."),
    ):
        lifecycle = commands.add_parser(
            command,
            help=f"Engine ledger: {synopsis}",
            description=f"Engine ledger: {synopsis}",
        )
        lifecycle.set_defaults(stage1_command=command)
        lifecycle.add_argument("--database", required=True, type=Path)
        lifecycle.add_argument("--owner", required=True)
        if command == "start":
            lifecycle.add_argument("--request", required=True, type=Path)
        else:
            lifecycle.add_argument("--run-id", required=True)
        if command == "next":
            lifecycle.add_argument("--advance", action="store_true")
        if command == "review":
            lifecycle.add_argument("--report", required=True, type=Path)
            lifecycle.add_argument("--repair-attempt", required=True, type=int)
        if command in {"approve", "reject", "cancel"}:
            lifecycle.add_argument("--node-id", required=True)
        if command in {"approve", "reject"}:
            lifecycle.add_argument("--subject-digest", required=True)
            lifecycle.add_argument("--challenge", required=True)
            lifecycle.add_argument("--principal-id", default=None)
            lifecycle.add_argument("--reason", required=True)
    add_build_request_parser(commands)
    commands.choices["build-request"].set_defaults(stage1_command="build-request")


def _add_stack_status_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    status = commands.add_parser(
        "status",
        help="Engine ledger: Report one stack's slice states and merge readiness.",
        description="Engine ledger: Inspect a stack with an explicit database and owner.",
    )
    status.set_defaults(stage2_command="status")
    status.add_argument("--database", required=True, type=Path)
    status.add_argument("--owner", required=True)
    status.add_argument("--stack-id", required=True)


def _run_doctor(*, as_json: bool) -> int:
    from enginery.cli.doctor import run_doctor

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


def _run_adapter_doctor(args: argparse.Namespace) -> int:
    from enginery.adapters.broker_doctor import broker_provider_statuses
    from enginery.adapters.local import local_provider_statuses
    from enginery.application.adapter_types import AdapterAvailability

    statuses = local_provider_statuses() + broker_provider_statuses(
        github_repository=args.github_repository,
        github_executable=args.github_executable,
        pypi_project_name=args.pypi_project_name,
        pypi_executable=args.pypi_executable,
        deployment_app_script=args.deployment_app_script,
        deployment_artifacts_root=args.deployment_artifacts_root,
        deployment_state_root=args.deployment_state_root,
        deployment_python_executable=args.deployment_python_executable,
    )
    ok = all(status.availability is AdapterAvailability.AVAILABLE for status in statuses)
    if args.json:
        payload: list[dict[str, object]] = []
        for status in statuses:
            fingerprint = status.fingerprint
            entry: dict[str, object] = {
                "availability": status.availability.value,
                "detail": status.detail,
                "kind": status.kind.value,
            }
            if fingerprint is not None:
                entry["capabilities"] = [capability.name for capability in fingerprint.capabilities]
                entry["fingerprint"] = str(fingerprint.digest)
                entry["provider_id"] = fingerprint.provider_id
            payload.append(entry)
        print(json.dumps(payload, indent=2))
    else:
        for status in statuses:
            fingerprint = status.fingerprint
            if fingerprint is not None:
                print(
                    f"[{status.availability.value}] {status.kind.value}: "
                    f"{fingerprint.provider_id} {fingerprint.digest}"
                )
            else:
                print(f"[{status.availability.value}] {status.kind.value}: {status.detail}")
    return SUCCESS if ok else exit_code_for(FailureClass.MISSING_PREREQUISITE)


def _run_policy(args: argparse.Namespace) -> int:
    from enginery.domain.policy_decision import PolicyResult
    from enginery.policy.evaluator import PolicyEvaluator

    if args.policy_command is None:
        raise InvalidInputError("a policy subcommand is required", details={"command": "policy"})
    if args.policy_command != "explain":
        raise AssertionError(f"unhandled policy command: {args.policy_command}")  # pragma: no cover
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
    except OSError as error:
        raise InvalidInputError(
            "unable to read policy request",
            details={"path": str(args.request), "error": str(error)},
        ) from error
    except json.JSONDecodeError as error:
        raise InvalidInputError(
            "policy request must be JSON",
            details={"path": str(args.request), "error": error.msg},
        ) from error
    if not isinstance(request, dict) or not isinstance(request.get("action"), str):
        raise InvalidInputError("policy request requires a string action")
    explanation = PolicyEvaluator(
        policy_version=str(request.get("policy_version", "unversioned"))
    ).explain_action_name(request["action"])
    payload = {
        "action": explanation.action,
        "result": explanation.result.value,
        "rule_id": explanation.rule_id,
        "rationale": explanation.rationale,
        "normalized_inputs": explanation.normalized_inputs,
    }
    print(json.dumps(payload, sort_keys=True))
    if explanation.result is PolicyResult.ALLOW:
        return SUCCESS
    if explanation.result is PolicyResult.REQUIRE_HUMAN:
        return exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    return exit_code_for(FailureClass.POLICY_DENIAL)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "operator":
        from enginery.cli.operator import main as operator_main

        return operator_main(arguments[1:])

    from enginery.cli.capability import run_capability
    from enginery.cli.factory import run_factory
    from enginery.cli.gate import run_gate
    from enginery.cli.outcome import run_outcome
    from enginery.cli.runner import run_runner
    from enginery.cli.stage1 import run_stage1
    from enginery.cli.stage2 import run_stage2
    from enginery.cli.workspace import run_workspace

    parser = _build_parser()
    args = parser.parse_args(arguments)

    if args.command is None:
        parser.print_help(sys.stderr)
        return exit_code_for(FailureClass.INVALID_INPUT)

    try:
        if args.command == "doctor":
            return _run_doctor(as_json=args.json)
        if args.command == "policy":
            return _run_policy(args)
        if args.command == "capability":
            return run_capability(args)
        if hasattr(args, "factory_command"):
            return run_factory(args)
        if args.command == "engine":
            if args.engine_command is None:
                raise InvalidInputError("engine requires a subcommand")
            return run_stage2(args) if args.engine_command == "status" else run_stage1(args)
        if args.command == "outcome":
            return run_outcome(args)
        if args.command == "gate":
            return run_gate(args)
        if args.command == "workspace":
            return run_workspace(args)
        if args.command == "runner":
            return run_runner(args)
        if args.command == "adapter":
            if args.adapter_command == "doctor":
                return _run_adapter_doctor(args)
            raise InvalidInputError("adapter requires a subcommand")
    except EngineryError as error:
        print(str(error), file=sys.stderr)
        return exit_code_for(error.failure_class)

    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
