"""``enginery operator``: the fixed operator recovery entry point.

Run by the operator outside any coding-agent session, this can pause dispatch, drain or stop
running workers, select the pinned runner image, and back up or restore the ledger. It imports
only the domain, the ledger, the engine control modules and the OCI adapter used to qualify an
image -- never the workflows -- so it keeps working when a workflow is broken or a run is
wedged. Recovery commands print JSON; verify and rebuild-projections preserve the ledger
consistency commands' text/JSON output. The legacy console script remains supported.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import container_name_for, worker_container_gone
from enginery.cli._exit_codes import SUCCESS, exit_code_for
from enginery.cli.ledger import (
    run_backup,
    run_rebuild_projections_command,
    run_restore,
    run_verify,
    run_verify_command,
)
from enginery.domain.errors import (
    EngineryError,
    FailureClass,
    HumanActionRequiredError,
    InvalidInputError,
    ValidationFailureError,
)
from enginery.domain.runner import RunnerSelection
from enginery.engine.operator_control import (
    DrainReport,
    OperatorControl,
    SelectedRunner,
    WorkerView,
    drain_stop,
    drain_wait,
    live_workers,
    open_operator_ledger,
    operator_status,
    read_control_state,
)
from enginery.engine.runtime import CoordinatorRuntime
from enginery.ledger.backup import (
    backup_artifacts_path,
    backup_database_path,
    read_backup_manifest,
)
from enginery.ledger.service import LedgerService
from enginery.ledger.verify import VerificationReport, verify_ledger

_DEFAULT_OWNER = "enginery-operator"
_HEARTBEAT_WINDOW = timedelta(seconds=120)
_SIDECAR_SUFFIXES = ("", "-wal", "-shm")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enginery operator",
        description="Operator ledger: Pause, drain, select a runner, back up, restore and verify.",
    )
    _add_operator_commands(parser)
    return parser


def add_operator_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the workflow-free operator group on the root parser."""
    parser = subparsers.add_parser(
        "operator",
        help="Operator ledger: Run recovery, consistency and storage commands.",
        description="Operator ledger: Pause, drain, select a runner, back up, restore and verify.",
    )
    _add_operator_commands(parser)


def _add_operator_commands(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="operator_command")

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(
            name,
            help=f"Operator ledger: {help_text}",
            description=f"Operator ledger: {help_text}",
        )
        sub.add_argument("--database", required=True, type=Path, help="Ledger database path.")
        return sub

    status = command("status", "Report pause state, running workers, runner and ledger health.")
    status.add_argument("--artifacts", type=Path, default=None, help="Artifact store to verify.")
    pause = command("pause", "Stop dispatching new workers; running workers are untouched.")
    pause.add_argument("--reason", default=None, help="One-line reason recorded with the pause.")
    command("resume", "Resume dispatching new workers.")
    drain = command("drain", "Pause, then wait for or stop the running workers.")
    mode = drain.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--wait-seconds",
        type=float,
        metavar="N",
        help="Wait up to N seconds for running workers to exit on their own.",
    )
    mode.add_argument(
        "--stop",
        action="store_true",
        help="Stop running workers (exact process-group termination, container removal).",
    )
    drain.add_argument("--owner", default=_DEFAULT_OWNER, help="Coordinator owner for --stop.")
    drain.add_argument(
        "--docker-context",
        default=None,
        help="Engine context to remove isolated worker containers on (default: selected runner).",
    )
    select = command("select-runner", "Qualify and record the retained pinned runner image.")
    select.add_argument("--image", required=True, metavar="sha256:ID")
    select.add_argument("--docker-context", default=None, help="Docker context of the engine.")
    backup = command("backup", "Verify the ledger, back it up, and verify the backup.")
    backup.add_argument("--output", required=True, type=Path, help="Empty backup directory.")
    backup.add_argument("--artifacts", type=Path, default=None, help="Artifact store to include.")
    restore = command("restore", "Verify a backup, then restore it over a paused ledger.")
    restore.add_argument("--backup", required=True, type=Path, help="Backup directory.")
    restore.add_argument("--artifacts", type=Path, default=None, help="Artifact store to restore.")
    restore.add_argument(
        "--force-unpaused",
        action="store_true",
        help="Restore although the ledger is not paused, has live workers, or cannot be read.",
    )
    verify = command("verify", "Check ledger and artifact-store consistency.")
    verify.add_argument("--artifacts", type=Path, default=None, help="Artifact store to verify.")
    verify.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    command("rebuild-projections", "Rebuild ledger projections from stored events.")


def main(
    argv: Sequence[str] | None = None,
    *,
    admission: OciRunnerAdmission | None = None,
    engine_factory: Callable[[str | None], OciEngine] = lambda context: OciEngine(context=context),
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Run one operator command; the keyword arguments are test seams, unused by the script."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.operator_command is None:
        parser.print_help(sys.stderr)
        return exit_code_for(FailureClass.INVALID_INPUT)
    try:
        if args.operator_command == "status":
            return _status(args)
        if args.operator_command == "pause":
            return _pause(args, clock)
        if args.operator_command == "resume":
            return _resume(args, clock)
        if args.operator_command == "drain":
            return _drain(args, clock, engine_factory)
        if args.operator_command == "select-runner":
            return _select_runner(args, clock, admission or OciRunnerAdmission())
        if args.operator_command == "backup":
            return _backup(args)
        if args.operator_command == "restore":
            return _restore(args, clock)
        if args.operator_command == "verify":
            return run_verify_command(
                database=args.database, artifacts=args.artifacts, as_json=args.json
            )
        if args.operator_command == "rebuild-projections":
            return run_rebuild_projections_command(database=args.database)
    except EngineryError as error:
        if args.operator_command in {"verify", "rebuild-projections"}:
            print(str(error), file=sys.stderr)
            return exit_code_for(error.failure_class)
        print(
            json.dumps(
                {
                    "error": {
                        "failure_class": error.failure_class.value,
                        "message": str(error),
                        "details": dict(error.details),
                    }
                },
                indent=2,
                sort_keys=True,
                default=str,
            ),
            file=sys.stderr,
        )
        return exit_code_for(error.failure_class)
    raise AssertionError(f"unhandled command: {args.operator_command}")  # pragma: no cover


def _emit(report: Mapping[str, object]) -> None:
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


def _status(args: argparse.Namespace) -> int:
    artifacts = _existing_directory(args.artifacts)
    with open_operator_ledger(args.database) as ledger:
        report = operator_status(ledger, database=args.database, artifacts=artifacts)
    _emit(report)
    ledger_report = report["ledger"]
    assert isinstance(ledger_report, dict)
    return SUCCESS if ledger_report["healthy"] else exit_code_for(FailureClass.VALIDATION_FAILURE)


def _pause(args: argparse.Namespace, clock: Callable[[], datetime]) -> int:
    with open_operator_ledger(args.database) as ledger:
        change = OperatorControl(ledger).pause(now=clock(), reason=args.reason)
    _emit({"changed": change.changed, **change.state.to_report()})
    return SUCCESS


def _resume(args: argparse.Namespace, clock: Callable[[], datetime]) -> int:
    with open_operator_ledger(args.database) as ledger:
        change = OperatorControl(ledger).resume(now=clock())
    _emit({"changed": change.changed, **change.state.to_report()})
    return SUCCESS


def _drain(
    args: argparse.Namespace,
    clock: Callable[[], datetime],
    engine_factory: Callable[[str | None], OciEngine],
) -> int:
    with open_operator_ledger(args.database) as ledger:
        if args.stop:
            report, cleanup = _stop_workers(args, ledger, clock, engine_factory)
        else:
            report = drain_wait(ledger, now=clock(), wait_seconds=args.wait_seconds)
            cleanup = None
    state = report.to_state()
    if cleanup is not None:
        state["container_cleanup"] = cleanup
    _emit(state)
    return _drain_exit_code(report)


def _stop_workers(
    args: argparse.Namespace,
    ledger: LedgerService,
    clock: Callable[[], datetime],
    engine_factory: Callable[[str | None], OciEngine],
    *,
    context_for: Callable[[WorkerView], RunnerSelection | None] | None = None,
) -> tuple[DrainReport, str | list[dict[str, object]]]:
    runtime = CoordinatorRuntime(ledger, owner=args.owner)

    def stop_worker(worker: WorkerView) -> None:
        epoch = runtime.claim_epoch(now=clock(), heartbeat_window=_HEARTBEAT_WINDOW)
        runtime.cancel_node(
            run_id=worker.run_id, node_id=worker.node_id, epoch=epoch.epoch, now=clock()
        )

    per_worker = context_for is not None
    context: str | None = None
    if context_for is None:
        selected = read_control_state(ledger).selected_runner
        if args.docker_context is not None:
            context = args.docker_context
        elif selected is not None:
            context = selected.docker_context
        else:
            report = drain_stop(ledger, now=clock(), stop_worker=stop_worker)
            return report, "skipped: no engine context given and no runner selected"
    contexts: dict[str, str | None] = {}
    skipped: set[str] = set()

    def remove_containers(worker: WorkerView) -> tuple[str, ...]:
        worker_context = context
        if context_for is not None:
            runner = context_for(worker)
            if runner is None:
                skipped.add(worker.operation_id)
                return ()
            worker_context = runner.docker_context
        contexts[worker.operation_id] = worker_context
        engine = engine_factory(worker_context)
        name = container_name_for(worker.operation_id)
        existed = not worker_container_gone(engine, worker.operation_id)
        engine.remove(name, force=True)
        if not worker_container_gone(engine, worker.operation_id):
            raise HumanActionRequiredError(
                f"worker container {name} still exists after removal",
                details={"container": name, "operation_id": worker.operation_id},
            )
        return (name,) if existed else ()

    report = drain_stop(
        ledger, now=clock(), stop_worker=stop_worker, remove_containers=remove_containers
    )
    if not per_worker:
        return report, "performed"
    return report, [
        {
            "run_id": result.worker.run_id,
            "node_id": result.worker.node_id,
            "operation_id": result.worker.operation_id,
            "docker_context": contexts.get(result.worker.operation_id),
            "status": (
                "skipped: worker stop failed"
                if not result.stopped
                else "failed"
                if result.container_error is not None
                else "skipped: trusted-local run has no container"
                if result.worker.operation_id in skipped
                else "performed"
            ),
            "containers_removed": list(result.containers_removed),
            "error": result.container_error,
        }
        for result in report.stopped
    ]


def _drain_exit_code(report: DrainReport) -> int:
    if report.stop_failures:
        classes = {result.error_class for result in report.stop_failures}
        if "internal_invariant_violation" in classes:
            return exit_code_for(FailureClass.INTERNAL_INVARIANT_VIOLATION)
        return exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    if report.drained:
        return SUCCESS
    if report.mode == "wait":
        return exit_code_for(FailureClass.TIMEOUT)
    return exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)


def _select_runner(
    args: argparse.Namespace, clock: Callable[[], datetime], admission: OciRunnerAdmission
) -> int:
    selection = RunnerSelection.isolated(
        image=args.image, profile=admission.profile, docker_context=args.docker_context
    )
    decision = admission.admit(selection)
    if decision.hold is not None or decision.engine is None or decision.image is None:
        _emit(
            {
                "selected": False,
                "hold": None if decision.hold is None else decision.hold.to_state(),
            }
        )
        return exit_code_for(FailureClass.HUMAN_ACTION_REQUIRED)
    now = clock()
    runner = SelectedRunner(
        image=decision.image.image_id,
        docker_context=args.docker_context,
        profile=admission.profile.name,
        profile_digest=str(admission.profile.digest),
        engine=decision.engine.to_state(),
        image_identity=decision.image.to_state(),
        selected_at=now.isoformat(),
    )
    with open_operator_ledger(args.database) as ledger:
        change = OperatorControl(ledger).select_runner(runner, now=now)
    _emit({"selected": True, "changed": change.changed, **change.state.to_report()})
    return SUCCESS


def _backup(args: argparse.Namespace) -> int:
    artifacts = _existing_directory(args.artifacts)
    before = run_verify(database=args.database, artifacts=artifacts)
    _require_healthy(before, subject=f"ledger {args.database}", action="backed up")
    manifest = run_backup(database=args.database, output=args.output, artifacts=artifacts)
    after = _verify_backup(args.output)
    _require_healthy(after, subject=f"backup {args.output}", action="used; it is not valid")
    _emit(
        {
            "backup": str(args.output),
            "created_at": manifest.created_at,
            "schema_version": manifest.schema_version,
            "includes_artifacts": manifest.includes_artifacts,
            "verified_before": _verification(before),
            "verified_after": _verification(after),
        }
    )
    return SUCCESS


def _restore(args: argparse.Namespace, clock: Callable[[], datetime]) -> int:
    database: Path = args.database
    backup: Path = args.backup
    # 1. The backup is verified before anything of the current ledger is touched.
    manifest = read_backup_manifest(backup)
    backup_report = _verify_backup(backup)
    _require_healthy(backup_report, subject=f"backup {backup}", action="restored")
    if (
        manifest.includes_artifacts
        and backup_artifacts_path(backup).is_dir()
        and args.artifacts is None
    ):
        raise InvalidInputError("the backup includes artifacts; pass --artifacts to restore them")
    # 2. The runtime must be provably paused with nothing running, unless forced.
    prior = _require_restorable(database, force=args.force_unpaused)
    stamp = clock().strftime("%Y%m%dT%H%M%SZ")
    moved_aside = _move_aside(database, f"pre-restore-{stamp}") if database.exists() else None
    # 3. Restore, then verify the restored ledger; on any failure put the prior ledger back.
    try:
        run_restore(backup=backup, database=database, artifacts=args.artifacts)
        restored_artifacts = (
            args.artifacts if manifest.includes_artifacts and args.artifacts is not None else None
        )
        restored_report = verify_ledger(database, artifact_store_root=restored_artifacts)
    except BaseException:
        _discard_partial(database, moved_aside)
        raise
    if not restored_report.healthy:
        failed = _move_aside(database, f"failed-restore-{stamp}")
        if moved_aside is not None:
            _move_back(moved_aside, database)
        raise ValidationFailureError(
            f"the restored ledger failed verification; the prior ledger was put back and the "
            f"failed copy kept at {failed}",
            details={"issues": _verification(restored_report)["issues"]},
        )
    # 4. The restored ledger starts paused, whatever it was paused as when backed up.
    with open_operator_ledger(database) as ledger:
        control = OperatorControl(ledger).pause(
            now=clock(), reason=f"restored from {backup}; verify, then resume"
        )
    _emit(
        {
            "restored": str(database),
            "backup": str(backup),
            "schema_version": manifest.schema_version,
            "prior": prior,
            "prior_ledger_kept_at": None if moved_aside is None else str(moved_aside),
            "verified_backup": _verification(backup_report),
            "verified_restored": _verification(restored_report),
            **control.state.to_report(),
        }
    )
    return SUCCESS


def _require_restorable(database: Path, *, force: bool) -> str:
    if not database.exists():
        return "absent"
    try:
        with open_operator_ledger(database) as ledger:
            paused = read_control_state(ledger).paused
            running = live_workers(ledger)
    except EngineryError as error:
        if force:
            return f"unreadable (forced): {error}"
        raise HumanActionRequiredError(
            f"cannot prove the current ledger is paused: {error}; confirm nothing runs, then "
            "pass --force-unpaused"
        ) from error
    if force:
        return "forced"
    if not paused:
        raise HumanActionRequiredError(
            "restore requires a paused runtime: run `enginery-operator pause` (and drain the "
            "running workers), or pass --force-unpaused"
        )
    if running:
        raise HumanActionRequiredError(
            f"restore refused: {len(running)} worker(s) are still running; drain them or pass "
            "--force-unpaused",
            details={"workers": [view.to_state() for view in running]},
        )
    return "paused"


def _move_aside(database: Path, label: str) -> Path:
    target = database.with_name(f"{database.name}.{label}")
    if any((target.parent / f"{target.name}{suffix}").exists() for suffix in _SIDECAR_SUFFIXES):
        raise InvalidInputError(f"{target} already exists; refusing to overwrite it")
    for suffix in _SIDECAR_SUFFIXES:
        source = database.parent / f"{database.name}{suffix}"
        if source.exists():
            os.replace(source, target.parent / f"{target.name}{suffix}")
    return target


def _move_back(aside: Path, database: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        source = aside.parent / f"{aside.name}{suffix}"
        if source.exists():
            os.replace(source, database.parent / f"{database.name}{suffix}")


def _discard_partial(database: Path, moved_aside: Path | None) -> None:
    """After a failed restore copy, remove the partial ledger and put the prior one back."""
    for suffix in _SIDECAR_SUFFIXES:
        partial = database.parent / f"{database.name}{suffix}"
        if partial.exists():
            partial.unlink()
    if moved_aside is not None:
        _move_back(moved_aside, database)


def _verify_backup(backup: Path) -> VerificationReport:
    manifest = read_backup_manifest(backup)
    artifacts = backup_artifacts_path(backup)
    return verify_ledger(
        backup_database_path(backup),
        artifact_store_root=artifacts
        if manifest.includes_artifacts and artifacts.is_dir()
        else None,
    )


def _require_healthy(report: VerificationReport, *, subject: str, action: str) -> None:
    if not report.healthy:
        raise ValidationFailureError(
            f"{subject} failed verification and cannot be {action}",
            details={"issues": _verification(report)["issues"]},
        )


def _verification(report: VerificationReport) -> dict[str, object]:
    return {
        "healthy": report.healthy,
        "schema_version": report.schema_version,
        "issues": [{"code": issue.code, "detail": issue.detail} for issue in report.issues],
    }


def _existing_directory(path: Path | None) -> Path | None:
    if path is not None and not path.is_dir():
        raise InvalidInputError(f"artifact store {path} is not an existing directory")
    return path
