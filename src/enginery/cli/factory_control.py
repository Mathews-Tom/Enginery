"""Project-ledger pause and drain using the fixed operator's recovery path."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from enginery.adapters.oci.engine import OciEngine
from enginery.cli.factory_collect import require_state_path
from enginery.cli.factory_run import database_path, workspace_documents
from enginery.cli.operator import _drain_exit_code, _stop_workers
from enginery.engine.operator_control import OperatorControl, drain_wait
from enginery.ledger.service import LedgerService


def _now() -> datetime:
    return datetime.now(UTC)


def _open_ledger(project: Path) -> LedgerService:
    path = database_path(project)
    require_state_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_state_path(path)
    return LedgerService.open(path)


def pause(project: Path, *, lift: bool, reason: str | None) -> dict[str, object]:
    """Only the project's ledger dispatch flag changes; workers remain untouched."""
    ledger = _open_ledger(project)
    try:
        control = OperatorControl(ledger)
        change = control.resume(now=_now()) if lift else control.pause(now=_now(), reason=reason)
        return {
            "scope": f"project ledger {database_path(project)}",
            "database": str(database_path(project)),
            "changed": change.changed,
            "running_workers": "untouched",
            **change.state.to_report(),
        }
    finally:
        ledger.close()


def drain(
    project: Path, *, stop: bool, wait_seconds: float | None
) -> tuple[dict[str, object], int]:
    """Pause and reuse the operator's worker-stop/container cleanup and exit contract."""
    ledger = _open_ledger(project)
    try:
        cleanup = None
        if stop:
            args = argparse.Namespace(owner="factory", docker_context=None)
            report, cleanup = _stop_workers(
                args, ledger, _now, lambda context: OciEngine(context=context)
            )
        else:
            assert wait_seconds is not None
            report = drain_wait(ledger, now=_now(), wait_seconds=wait_seconds)
        document = report.to_state()
        document.update(
            {
                "scope": f"project ledger {database_path(project)}",
                "database": str(database_path(project)),
                "workspaces": workspace_documents(project, ledger),
                "released": (
                    "by resume/reconcile"
                    if not stop
                    else "partial; see stopped"
                    if report.stop_failures
                    else "now"
                ),
                "release_scope": (
                    "successfully stopped workers; previously exited workers require "
                    "resume/reconcile"
                    if stop
                    else "wait observes exit only; it does not collect worker results"
                ),
                "reconcile": (
                    f"enginery factory pause --lift --project {project}; "
                    f"enginery factory resume RUN --project {project}"
                ),
            }
        )
        if cleanup is not None:
            document["container_cleanup"] = cleanup
        return document, _drain_exit_code(report)
    finally:
        ledger.close()
