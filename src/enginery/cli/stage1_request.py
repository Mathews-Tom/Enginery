"""``enginery stage1 build-request``: compose a valid ``--request`` document.

Before this command, composing a `Stage1RunRequest` meant hand-writing the
Python script `docs/examples.md` still documents (`build_request.py`):
construct `WorkItem`, `Run`, and `Stage1RunRequest` directly against the
library, then serialize `request.initial_state()` to JSON. This command
performs the same, real construction the CLI's own `stage1 start` already
decodes (`enginery.workflows.stage1.stage1_request_from_state`), driven by
flags instead of a one-off script, and writes the identical JSON shape.

Every default mirrors `docs/examples.md`'s Example B script exactly
(`policy-v1`, `("uv", "run", "pytest", "-q")`, `time_budget_seconds=1800`,
`cost_budget=5.0`, `permitted_capabilities=("git",)`,
`evidence_requirements=("redacted harness transcript",)`,
`required_checks=("CI",)`, `repair_limit=1`, `operator-gh-cli` /
`operator-harness-session` credential references, and the
`no-capabilities-locked` / `local-environment` / `local-configuration`
digest sentinels for a run with no bound capability lock, environment
manifest, or configuration snapshot) so a guided invocation reproduces
today's documented manual procedure without retyping it.
"""

from __future__ import annotations

import argparse
import json
import shlex
from decimal import Decimal, InvalidOperation
from pathlib import Path

from enginery.application.work_ports import WorkLedgerSnapshot
from enginery.capabilities.serialization import read_lock
from enginery.domain.digests import Digest
from enginery.domain.enums import RiskClass, WorkKind
from enginery.domain.errors import InvalidInputError
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId, WorkItemId
from enginery.domain.run import Run, RunState
from enginery.domain.work_item import WorkItem, WorkItemState
from enginery.workflows.issue_to_pr import issue_to_pr_manifest
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
)

_NO_CAPABILITY_LOCK_DIGEST = Digest.of_bytes(b"no-capabilities-locked")
_LOCAL_ENVIRONMENT_DIGEST = Digest.of_bytes(b"local-environment")
_LOCAL_CONFIGURATION_DIGEST = Digest.of_bytes(b"local-configuration")
_DEFAULT_CAPABILITY_LOCKFILE = Path(".enginery/capabilities.lock.json")


def add_build_request_parser(
    stage1_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register ``stage1 build-request``'s flags on the shared ``stage1`` subparsers."""
    parser = stage1_subparsers.add_parser(
        "build-request",
        help="Write a Stage 1 --request document from flags instead of a hand-written script.",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Path to write the request JSON."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--work-item-id", default=None, help="Defaults to work-<run-id>.")
    parser.add_argument(
        "--repository", required=True, help="owner/name; binds every repository field."
    )
    parser.add_argument("--source-provider", default="github")
    parser.add_argument(
        "--work-kind", choices=tuple(kind.value for kind in WorkKind), default="issue"
    )
    parser.add_argument("--external-reference", required=True)
    parser.add_argument("--source-snapshot-reference", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--base-revision", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument(
        "--acceptance-criterion",
        dest="acceptance_criteria",
        action="append",
        required=True,
        metavar="CRITERION",
        help="Repeatable; at least one is required.",
    )
    parser.add_argument(
        "--inapplicable-criterion",
        dest="inapplicable_criteria",
        action="append",
        type=int,
        default=[],
        metavar="INDEX",
        help="0-based index of an acceptance criterion this run does not need to satisfy.",
    )
    parser.add_argument("--constraint", dest="constraints", action="append", default=[])
    parser.add_argument(
        "--risk-class", choices=tuple(risk.value for risk in RiskClass), default=RiskClass.LOW.value
    )
    parser.add_argument("--policy-set-version", default="policy-v1")
    parser.add_argument(
        "--capability-lockfile",
        type=Path,
        default=_DEFAULT_CAPABILITY_LOCKFILE,
        help="Read and bind the real lock digest when present; falls back to the "
        "documented no-capabilities-locked sentinel otherwise.",
    )
    _add_digest_override(parser, flag="environment_manifest")
    _add_digest_override(parser, flag="configuration_snapshot")
    parser.add_argument("--repository-path", required=True, type=Path)
    parser.add_argument("--workspace-path", required=True, type=Path)
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--head-branch", default=None, help="Defaults to enginery/<run-id>.")
    parser.add_argument(
        "--validation-command",
        dest="validation_commands",
        action="append",
        default=None,
        metavar='"COMMAND ARG..."',
        help='Repeatable, shell-split; defaults to "uv run pytest -q".',
    )
    parser.add_argument("--required-check", dest="required_checks", action="append", default=None)
    parser.add_argument("--repair-limit", type=int, default=1)
    parser.add_argument("--implementation-attempt-id", default="implement-0")
    parser.add_argument(
        "--implementation-operation-id", default=None, help="Defaults to implement:<run-id>."
    )
    parser.add_argument("--implementation-time-budget-seconds", type=int, default=1800)
    parser.add_argument("--implementation-cost-budget", default="5.0")
    parser.add_argument("--implementation-no-cost-budget", action="store_true")
    parser.add_argument(
        "--permitted-capability", dest="permitted_capabilities", action="append", default=None
    )
    parser.add_argument(
        "--evidence-requirement", dest="evidence_requirements", action="append", default=None
    )
    parser.add_argument("--github-repository", default=None, help="Defaults to --repository.")
    parser.add_argument("--github-credential-reference", default="operator-gh-cli")
    parser.add_argument("--github-executable", default="gh")
    parser.add_argument("--harness-provider", choices=("omp", "claude-code"), default="omp")
    parser.add_argument("--harness-credential-reference", default="operator-harness-session")
    parser.add_argument(
        "--harness-executable", default=None, help="Defaults to omp/claude by provider."
    )
    parser.add_argument("--artifact-root", required=True, type=Path)


def _add_digest_override(parser: argparse.ArgumentParser, *, flag: str) -> None:
    option_stem = flag.replace("_", "-")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(f"--{option_stem}-digest", metavar="ALGORITHM:HEX")
    group.add_argument(f"--{option_stem}-file", type=Path, metavar="PATH")


def _parse_digest(value: str) -> Digest:
    algorithm, separator, hex_value = value.partition(":")
    if not separator:
        raise InvalidInputError("digest overrides must use the algorithm:hex form")
    return Digest(algorithm=algorithm, hex_value=hex_value.lower())


def _digest_override(args: argparse.Namespace, *, flag: str, default: Digest) -> Digest:
    digest_value = getattr(args, f"{flag}_digest")
    file_value = getattr(args, f"{flag}_file")
    if digest_value is not None:
        return _parse_digest(digest_value)
    if file_value is not None:
        return Digest.of_bytes(file_value.read_bytes())
    return default


def _capability_lock_digest(lockfile: Path) -> Digest:
    if not lockfile.is_file():
        return _NO_CAPABILITY_LOCK_DIGEST
    return read_lock(lockfile).digest()


def _cost_budget(args: argparse.Namespace) -> Decimal | None:
    if args.implementation_no_cost_budget:
        return None
    try:
        return Decimal(args.implementation_cost_budget)
    except InvalidOperation as error:
        raise InvalidInputError(
            "--implementation-cost-budget must be a decimal",
            details={"value": args.implementation_cost_budget},
        ) from error


def _validation_commands(values: list[str] | None) -> tuple[tuple[str, ...], ...]:
    if values is None:
        return (("uv", "run", "pytest", "-q"),)
    return tuple(tuple(shlex.split(value)) for value in values)


def _harness_executable(args: argparse.Namespace) -> str:
    if args.harness_executable is not None:
        return str(args.harness_executable)
    return "claude" if args.harness_provider == "claude-code" else "omp"


def build_request(args: argparse.Namespace) -> Stage1RunRequest:
    """Construct one durable Stage 1 request from ``build-request`` flags."""
    run_id = args.run_id
    work_item_id = args.work_item_id or f"work-{run_id}"
    head_branch = args.head_branch or f"enginery/{run_id}"
    implementation_operation_id = OperationId(
        args.implementation_operation_id or f"implement:{run_id}"
    )
    manifest = issue_to_pr_manifest()

    work_item = WorkItem(
        id=WorkItemId(work_item_id),
        work_kind=WorkKind(args.work_kind),
        source_provider=args.source_provider,
        external_reference=args.external_reference,
        source_snapshot_reference=args.source_snapshot_reference,
        title=args.title,
        objective=args.objective,
        acceptance_criteria=tuple(args.acceptance_criteria),
        constraints=tuple(args.constraints),
        risk_class=RiskClass(args.risk_class),
        repository_targets=(args.repository,),
        dependencies=(),
        state=WorkItemState.QUALIFYING,
    )
    snapshot = WorkLedgerSnapshot(work_item=work_item, source_revision=args.source_revision)
    applicable_criteria = tuple(
        index not in set(args.inapplicable_criteria)
        for index in range(len(work_item.acceptance_criteria))
    )

    run = Run(
        id=RunId(run_id),
        work_item_id=work_item.id,
        work_item_snapshot_digest=work_item.bound_field_digest,
        workflow_definition_id=WorkflowDefinitionId(manifest.id.value),
        workflow_definition_digest=manifest.content_digest,
        repository=args.repository,
        base_revision=args.base_revision,
        policy_set_version=args.policy_set_version,
        adapter_versions={},
        adapter_fingerprints={},
        capability_lock_digest=_capability_lock_digest(args.capability_lockfile),
        environment_manifest_digest=_digest_override(
            args, flag="environment_manifest", default=_LOCAL_ENVIRONMENT_DIGEST
        ),
        configuration_snapshot_digest=_digest_override(
            args, flag="configuration_snapshot", default=_LOCAL_CONFIGURATION_DIGEST
        ),
        state=RunState.CREATED,
    )

    return Stage1RunRequest(
        run=run,
        work_snapshot=snapshot,
        manifest=manifest,
        repository_id=args.repository,
        repository_path=args.repository_path.resolve(),
        workspace_path=args.workspace_path.resolve(),
        base_branch=args.base_branch,
        head_branch=head_branch,
        validation_commands=_validation_commands(args.validation_commands),
        applicable_criteria=applicable_criteria,
        required_checks=tuple(args.required_checks)
        if args.required_checks is not None
        else ("CI",),
        repair_limit=args.repair_limit,
        implementation=Stage1ImplementationRequest(
            attempt_id=args.implementation_attempt_id,
            operation_id=implementation_operation_id,
            time_budget_seconds=args.implementation_time_budget_seconds,
            cost_budget=_cost_budget(args),
            permitted_capabilities=(
                tuple(args.permitted_capabilities)
                if args.permitted_capabilities is not None
                else ("git",)
            ),
            evidence_requirements=(
                tuple(args.evidence_requirements)
                if args.evidence_requirements is not None
                else ("redacted harness transcript",)
            ),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository=args.github_repository or args.repository,
            github_credential_reference=args.github_credential_reference,
            github_executable=args.github_executable,
            harness_provider=args.harness_provider,
            harness_credential_reference=args.harness_credential_reference,
            harness_executable=_harness_executable(args),
            artifact_root=args.artifact_root.resolve(),
        ),
    )


def run_build_request(args: argparse.Namespace) -> int:
    request = build_request(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(request.initial_state(), indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "request_digest": str(request.digest),
                "run_id": str(request.run.id),
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = ["add_build_request_parser", "build_request", "run_build_request"]
