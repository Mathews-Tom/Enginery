"""``enginery stage1 build-request``: compose a valid ``--request`` document.

Before this command, composing a `Stage1RunRequest` meant hand-writing the
Python script `docs/examples.md` still documents (`build_request.py`):
construct `WorkItem`, `Run`, and `Stage1RunRequest` directly against the
library, then serialize `request.initial_state()` to JSON. This command
performs the same, real construction the CLI's own `stage1 start` already
decodes (`enginery.workflows.stage1.stage1_request_from_state`), driven by
flags instead of a one-off script, and writes the identical JSON shape.

What the source decides is not operator input. The work item, its source
revision, and its declared classification (the canonical `enginery/work-kind/*`
and `enginery/risk/*` labels) are read through the same GitHub adapter that
`stage1 watch --advance` uses to qualify the run, and are bound exactly as the
adapter returns them, so the request carries the digest qualification
recomputes from the labelled issue. An issue whose labels are missing,
ambiguous, or outside the declared vocabulary is refused with the adapter's own
error and nothing is written.

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

from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.application.execution_catalog import load_execution_catalog
from enginery.capabilities.serialization import read_lock
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import (
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId
from enginery.domain.run import Run, RunState
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.stage1 import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    implementation_attempt_id,
)

_NO_CAPABILITY_LOCK_DIGEST = Digest.of_bytes(b"no-capabilities-locked")
_LOCAL_ENVIRONMENT_DIGEST = Digest.of_bytes(b"local-environment")
_LOCAL_CONFIGURATION_DIGEST = Digest.of_bytes(b"local-configuration")
_DEFAULT_CAPABILITY_LOCKFILE = Path(".enginery/capabilities.lock.json")
_FACTORY_DEFAULT_HARNESS = "omp"


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
    parser.add_argument(
        "--repository", required=True, help="owner/name; binds every repository field."
    )
    parser.add_argument(
        "--external-reference",
        required=True,
        metavar="OWNER/REPO#N",
        help="The GitHub issue to build the request from. It is read through the GitHub "
        "adapter, which decides the work item, source revision, and declared classification.",
    )
    parser.add_argument("--base-revision", required=True)
    parser.add_argument(
        "--inapplicable-criterion",
        dest="inapplicable_criteria",
        action="append",
        type=int,
        default=[],
        metavar="INDEX",
        help="0-based index into the issue's acceptance criteria (the issue body is the only "
        "criterion) that this run does not need to satisfy.",
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
    parser.add_argument(
        "--implementation-attempt-id",
        default=None,
        help="Defaults to <run-id>-implement-0, unique per run on a shared ledger.",
    )
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
    parser.add_argument(
        "--harness-provider",
        choices=("omp", "claude-code"),
        default=None,
        help=f"The harness to run. Omitted, the built-in default ({_FACTORY_DEFAULT_HARNESS}) "
        "is recorded as a factory-layer choice rather than an explicit run-layer one.",
    )
    parser.add_argument("--harness-credential-reference", default="operator-harness-session")
    parser.add_argument(
        "--harness-executable", default=None, help="Defaults to omp/claude by provider."
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Pin an exact model for the harness: provider-qualified provider/id for omp, a "
        "full model name (not an alias) for claude-code. Omit to use the host's own default.",
    )
    parser.add_argument(
        "--effort",
        default=None,
        help="Pin the harness's own native effort level (omp: off, minimal, low, medium, high, "
        "xhigh, max; claude-code: low, medium, high, xhigh, max). Omit for the host default.",
    )
    parser.add_argument(
        "--strict-effective",
        action="store_true",
        help="Modifier for --model: hold the run unless the host reports exactly the pinned "
        "model. Rejected with --effort, since neither host reports effective effort.",
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


def _harness_executable(args: argparse.Namespace, harness: str) -> str:
    if args.harness_executable is not None:
        return str(args.harness_executable)
    return "claude" if harness == "claude-code" else "omp"


def build_request(args: argparse.Namespace) -> Stage1RunRequest:
    """Construct one durable Stage 1 request from ``build-request`` flags."""
    run_id = args.run_id
    if args.repair_limit > 0:
        # Repair attempt ids are always derived from the run id, whatever the first attempt's
        # id is, so the longest one must be valid for the run to be able to repair.
        implementation_attempt_id(run_id, args.repair_limit)
    if args.implementation_attempt_id is None:
        try:
            attempt_id = implementation_attempt_id(run_id, 0)
        except InvalidInputError as error:
            raise InvalidInputError(
                f"{error}, or pass --implementation-attempt-id", details=error.details
            ) from error
    else:
        attempt_id = args.implementation_attempt_id
    head_branch = args.head_branch or f"enginery/{run_id}"
    implementation_operation_id = OperationId(
        args.implementation_operation_id or f"implement:{run_id}"
    )
    manifest = stage1_work_manifest()

    # The built-in default harness is a factory-layer input; only a harness the operator named
    # on the command line is a run-layer input, so the recorded source says which it was.
    selection = resolve_execution_selection(
        (
            LayerControls(layer=SelectionLayer.FACTORY, harness=_FACTORY_DEFAULT_HARNESS),
            LayerControls(
                layer=SelectionLayer.RUN,
                harness=args.harness_provider,
                model=args.model,
                effort=args.effort,
                model_pinned=args.model is not None,
                effort_pinned=args.effort is not None,
                strict_effective=True if args.strict_effective else None,
            ),
        ),
        catalog=load_execution_catalog(),
    )

    # Everything the source decides is read once the operator's own flags are valid, and is
    # bound exactly as the adapter returns it.
    github = GitHubAdapterConfig(
        repository=args.github_repository or args.repository,
        credential_reference=args.github_credential_reference,
        executable=args.github_executable,
    )
    snapshot = GitHubWorkLedger(github).fetch(args.external_reference)
    work_item = snapshot.work_item
    applicable_criteria = tuple(
        index not in set(args.inapplicable_criteria)
        for index in range(len(work_item.acceptance_criteria))
    )

    run = Run(
        id=RunId(run_id),
        work_item_id=work_item.id,
        work_item_snapshot_digest=snapshot.bound_digest,
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
            attempt_id=attempt_id,
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
            github_repository=github.repository,
            github_credential_reference=github.credential_reference,
            github_executable=github.executable,
            harness_provider=selection.harness,
            harness_credential_reference=args.harness_credential_reference,
            harness_executable=_harness_executable(args, selection.harness),
            artifact_root=args.artifact_root.resolve(),
        ),
        execution_selection=selection,
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
