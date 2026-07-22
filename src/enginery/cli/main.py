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

from enginery.adapters.broker_doctor import broker_provider_statuses
from enginery.adapters.local import local_provider_statuses
from enginery.application.adapter_types import AdapterAvailability
from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.cli.capability import check_lock
from enginery.cli.doctor import run_doctor
from enginery.cli.gate import run_gate
from enginery.cli.ledger import (
    run_backup,
    run_rebuild_projections,
    run_restore,
    run_verify,
)
from enginery.cli.outcome import run_outcome
from enginery.cli.stage1 import run_stage1
from enginery.cli.stage1_request import add_build_request_parser
from enginery.cli.stage2 import run_stage2
from enginery.cli.workspace import run_workspace
from enginery.domain.errors import EngineryError, FailureClass, InvalidInputError
from enginery.domain.policy_decision import PolicyResult
from enginery.policy.evaluator import PolicyEvaluator

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

    adapter_parser = subparsers.add_parser("adapter", help="Inspect configured adapter providers.")
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

    policy_parser = subparsers.add_parser("policy", help="Explain policy decisions.")
    policy_subparsers = policy_parser.add_subparsers(dest="policy_command")
    explain_parser = policy_subparsers.add_parser(
        "explain",
        help="Explain a policy request without authorizing it.",
    )
    explain_parser.add_argument("request", type=Path)
    stage1_parser = subparsers.add_parser("stage1", help="Run the Stage 1 issue-to-PR lifecycle.")
    stage1_subparsers = stage1_parser.add_subparsers(dest="stage1_command")
    for command in (
        "start",
        "watch",
        "review",
        "approve",
        "reject",
        "cancel",
        "resume",
        "evidence",
    ):
        lifecycle_parser = stage1_subparsers.add_parser(command)
        lifecycle_parser.add_argument("--database", required=True, type=Path)
        lifecycle_parser.add_argument("--owner", required=True)
        if command == "start":
            lifecycle_parser.add_argument("--request", required=True, type=Path)
        else:
            lifecycle_parser.add_argument("--run-id", required=True)
        if command == "watch":
            lifecycle_parser.add_argument("--advance", action="store_true")
        if command == "review":
            lifecycle_parser.add_argument("--report", required=True, type=Path)
            lifecycle_parser.add_argument("--repair-attempt", required=True, type=int)
        if command in {"approve", "reject", "cancel", "resume"}:
            lifecycle_parser.add_argument("--node-id", required=True)
        if command in {"approve", "reject"}:
            lifecycle_parser.add_argument("--reason", required=True)
        if command == "resume":
            lifecycle_parser.add_argument("--attempt-id", required=True)
            lifecycle_parser.add_argument("--operation-id", required=True)
    add_build_request_parser(stage1_subparsers)

    stage2_parser = subparsers.add_parser(
        "stage2", help="Inspect Stage 2 plan-to-release stack state."
    )
    stage2_subparsers = stage2_parser.add_subparsers(dest="stage2_command")
    stage2_status_parser = stage2_subparsers.add_parser(
        "status", help="Report one stack's slice states and merge readiness."
    )
    stage2_status_parser.add_argument("--database", required=True, type=Path)
    stage2_status_parser.add_argument("--owner", required=True)
    stage2_status_parser.add_argument("--stack-id", required=True)

    outcome_parser = subparsers.add_parser(
        "outcome", help="Inspect raw outcome observations and completeness."
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

    gate_parser = subparsers.add_parser("gate", help="Report readiness against a decision gate.")
    gate_subparsers = gate_parser.add_subparsers(dest="gate_command")
    gate_status_parser = gate_subparsers.add_parser(
        "status", help="Report the current state of every condition for one registered gate."
    )
    gate_status_parser.add_argument("--gate", required=True, choices=("G4",))
    gate_status_parser.add_argument("--database", required=True, type=Path)
    gate_status_parser.add_argument(
        "--floor-config", type=Path, default=Path("config/gate-g4-floor.toml")
    )
    gate_status_parser.add_argument("--json", action="store_true")

    workspace_parser = subparsers.add_parser(
        "workspace", help="Inspect and release run-scoped workspace reservations."
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

    capability_parser = subparsers.add_parser("capability", help="Capability lock commands.")
    capability_subparsers = capability_parser.add_subparsers(dest="capability_command")
    capability_lock_parser = capability_subparsers.add_parser(
        "lock", help="Inspect or verify a capability lock."
    )
    capability_lock_parser.add_argument(
        "--check", action="store_true", help="Verify the materialized store against the lockfile."
    )
    capability_lock_parser.add_argument(
        "--lockfile", type=Path, default=Path(".enginery/capabilities.lock.json")
    )
    capability_lock_parser.add_argument(
        "--capabilities-root", type=Path, default=Path(".enginery/capabilities")
    )
    capability_lock_parser.add_argument("--json", action="store_true")

    explain_parser.add_argument("--json", action="store_true")

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


def _run_adapter_doctor(args: argparse.Namespace) -> int:
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


def _run_capability_lock(args: argparse.Namespace) -> int:
    if not args.check:
        raise InvalidInputError("`enginery capability lock` currently requires --check")
    report = check_lock(lockfile=args.lockfile, capabilities_root=args.capabilities_root)
    if report.lockfile is None:
        detail = f"no capability lock at {args.lockfile}; nothing to check"
        if args.json:
            print(json.dumps({"lockfile": None, "ok": True, "detail": detail}, sort_keys=True))
        else:
            print(detail)
        return SUCCESS
    if args.json:
        payload = {
            "lockfile": str(report.lockfile),
            "ok": report.ok,
            "findings": [
                {"name": f.name, "version": f.version, "ok": f.ok, "detail": f.detail}
                for f in report.findings
            ],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        for finding in report.findings:
            status = "OK" if finding.ok else "DRIFT"
            print(f"{status} {finding.name}@{finding.version}: {finding.detail}")
        print("no drift" if report.ok else "drift detected")
    return SUCCESS if report.ok else exit_code_for(FailureClass.VALIDATION_FAILURE)


def _run_capability(args: argparse.Namespace) -> int:
    if args.capability_command is None:
        raise InvalidInputError(
            "a capability subcommand is required", details={"command": "capability"}
        )
    if args.capability_command == "lock":
        return _run_capability_lock(args)
    raise AssertionError(  # pragma: no cover
        f"unhandled capability command: {args.capability_command}"
    )


def _run_policy(args: argparse.Namespace) -> int:
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
        if args.command == "policy":
            return _run_policy(args)
        if args.command == "capability":
            return _run_capability(args)
        if args.command == "stage1":
            return run_stage1(args)
        if args.command == "stage2":
            return run_stage2(args)
        if args.command == "outcome":
            return run_outcome(args)
        if args.command == "gate":
            return run_gate(args)
        if args.command == "workspace":
            return run_workspace(args)
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
