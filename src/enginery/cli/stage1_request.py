"""``enginery stage1 build-request``: compose a valid ``--request`` document.

The command builds the same `Stage1RunRequest` that `stage1 start` decodes
(`enginery.workflows.stage1.stage1_request_from_state`) and writes the identical
JSON shape. The source issue is read, not typed: the work item, its source
revision, and its declared classification (the canonical `enginery/work-kind/*`
and `enginery/risk/*` labels) come through the same GitHub adapter that
`stage1 watch --advance` uses to qualify the run, and are bound exactly as the
adapter returns them, so the request carries the digest qualification
recomputes from the labelled issue. An issue whose labels are missing,
ambiguous, or outside the declared vocabulary is refused with the adapter's own
error and nothing is written.

Flags only choose how the run is executed. Their defaults are
`policy-v1`, `("uv", "run", "pytest", "-q")` (trusted-local only: an isolated run must name its
validation commands, because its validation runs offline in the pinned runner image),
`time_budget_seconds=1800`,
`cost_budget=None` (no supported harness enforces a hard-dollar budget, so one is refused
rather than carried), `permitted_capabilities=("git",)`,
`evidence_requirements=("redacted harness transcript",)`,
`required_checks=("CI",)`, `repair_limit=1`, `operator-gh-cli` /
`operator-harness-session` credential references, and the
`no-capabilities-locked` / `local-environment` / `local-configuration`
digest sentinels for a run with no bound capability lock, environment
manifest, or configuration snapshot.
"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

from enginery.adapters.claude_code import probe_installed_host as probe_installed_claude
from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.adapters.omp import probe_installed_host as probe_installed_omp
from enginery.application.execution_catalog import load_execution_catalog
from enginery.capabilities.serialization import read_lock
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.execution_selection import (
    LayerControls,
    SelectionLayer,
    resolve_execution_selection,
)
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId
from enginery.domain.run import Run, RunState
from enginery.domain.runner import (
    MODEL_ACCESS_MODES,
    MODEL_ACCESS_NONE,
    RunnerProfile,
    RunnerSelection,
)
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.stage1 import (
    DEFAULT_VALIDATION_COMMAND,
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    first_attempt_generation,
    implementation_attempt_id,
    require_explicit_isolated_validation,
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
        help="Write a Stage 1 --request document for one GitHub issue.",
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
        help='Repeatable, shell-split. Defaults to "uv run pytest -q" for trusted-local; '
        "required with isolated-oci, whose validation runs offline in the pinned runner image "
        "(python3, node, git, uv, sh) and so cannot resolve a project's test dependencies.",
    )
    parser.add_argument("--required-check", dest="required_checks", action="append", default=None)
    parser.add_argument("--repair-limit", type=int, default=1)
    parser.add_argument(
        "--protected-path",
        dest="protected_paths",
        action="append",
        default=None,
        metavar="GLOB",
        help="Repeatable. An extra path pattern whose change holds the branch for an explicit "
        "operator decision before it is published. It only adds to the built-in protected "
        "set (.github/**, CI configuration, CODEOWNERS, .enginery/**, holdouts/**).",
    )
    parser.add_argument(
        "--implementation-attempt-id",
        default=None,
        help="Defaults to <run-id>-implement-0, unique per run on a shared ledger. Must end in "
        "-<number>: that number is the run's first generation and each repair adds one.",
    )
    parser.add_argument(
        "--implementation-operation-id", default=None, help="Defaults to implement:<run-id>."
    )
    parser.add_argument("--implementation-time-budget-seconds", type=int, default=1800)
    parser.add_argument(
        "--implementation-cost-budget",
        default=None,
        help="Refused: no supported harness enforces a hard-dollar budget, so none can be bound. "
        "The time budget (--implementation-time-budget-seconds) is enforced.",
    )
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
    parser.add_argument(
        "--runner",
        required=True,
        choices=("trusted-local", "isolated-oci"),
        help="The trust mode the worker runs in. trusted-local runs the worker as you, with "
        "your filesystem and environment, and is not containment. isolated-oci runs it in a "
        "hardened task-local container; a run bound to it is held, never run as trusted-local, "
        "when no qualified engine and image are available.",
    )
    parser.add_argument(
        "--runner-image",
        default=None,
        metavar="sha256:ID",
        help="With isolated-oci: the exact worker image id to pin (see `enginery runner "
        "build-image`). Never a mutable tag.",
    )
    parser.add_argument(
        "--docker-context",
        default=None,
        help="With isolated-oci: the docker context of the container engine to run on.",
    )
    parser.add_argument(
        "--model-access",
        default=MODEL_ACCESS_NONE,
        choices=MODEL_ACCESS_MODES,
        help="With isolated-oci: how the worker reaches a model. none gives it no route to any "
        "model. relay lets it reach only the operator's model-access relay (see `enginery "
        "runner relay up`), which holds the gateway credential so the worker never does; a run "
        "bound to relay is held, never run without model access, when the relay is not healthy.",
    )


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


def _reject_cost_budget(args: argparse.Namespace) -> None:
    """Refuse a hard-dollar budget instead of recording a number nothing enforces."""
    if args.implementation_cost_budget is not None:
        raise InvalidInputError(
            "no supported harness enforces a hard-dollar budget; none can be bound "
            "(the time budget, --implementation-time-budget-seconds, is enforced)",
            details={"value": args.implementation_cost_budget},
        )


def _validation_commands(values: list[str] | None) -> tuple[tuple[str, ...], ...]:
    if values is None:
        return (DEFAULT_VALIDATION_COMMAND,)
    return tuple(tuple(shlex.split(value)) for value in values)


def _harness_executable(args: argparse.Namespace, harness: str) -> str:
    if args.harness_executable is not None:
        return str(args.harness_executable)
    return "claude" if harness == "claude-code" else "omp"


def _probe_host(args: argparse.Namespace, harness: str) -> InstalledHost | None:
    """Observe the installed host the run will use, only when its controls are pinned."""
    executable = _harness_executable(args, harness)
    if harness == "claude-code":
        return probe_installed_claude(executable)
    if harness == "omp":
        return probe_installed_omp(executable)
    raise InvalidInputError(
        "Stage 1 harness_provider is not a configured harness", details={"harness": harness}
    )


def _runner_selection(args: argparse.Namespace) -> RunnerSelection:
    """Bind the explicitly requested trust mode; isolation needs an exact pinned image."""
    if args.runner == "trusted-local":
        if (
            args.runner_image is not None
            or args.docker_context is not None
            or args.model_access != MODEL_ACCESS_NONE
        ):
            raise InvalidInputError(
                "--runner-image, --docker-context and --model-access apply only to "
                "--runner isolated-oci"
            )
        return RunnerSelection.trusted_local()
    if args.runner_image is None:
        raise InvalidInputError(
            "--runner isolated-oci requires --runner-image sha256:<image id> "
            "(build one with `enginery runner build-image`)"
        )
    return RunnerSelection.isolated(
        image=args.runner_image,
        profile=RunnerProfile(),
        docker_context=args.docker_context,
        model_access=args.model_access,
    )


def build_request(args: argparse.Namespace) -> Stage1RunRequest:
    """Construct one durable Stage 1 request for the GitHub issue named by
    ``--external-reference``.
    """
    _reject_cost_budget(args)
    runner_selection = _runner_selection(args)
    if args.runner == "isolated-oci" and args.validation_commands is None:
        raise InvalidInputError(
            "--runner isolated-oci requires --validation-command: isolated validation runs "
            "offline in the pinned runner image, which has python3, node, git, uv and sh but "
            "not the project's test dependencies, so there is no default that can run there"
        )
    run_id = args.run_id
    if args.implementation_attempt_id is None:
        try:
            attempt_id = implementation_attempt_id(run_id, 0)
        except InvalidInputError as error:
            raise InvalidInputError(
                f"{error}, or pass --implementation-attempt-id", details=error.details
            ) from error
    else:
        attempt_id = args.implementation_attempt_id
    first_generation = first_attempt_generation(attempt_id)
    if args.repair_limit > 0:
        # Repair attempt ids are always derived from the run id, whatever the first attempt's
        # id is, so the last one the run may need must be valid for it to be able to repair.
        implementation_attempt_id(run_id, first_generation + args.repair_limit)
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
        # An isolated run executes the image's pinned harness, never the operator's binary, so
        # the host binary is neither consulted nor recorded as observed at bind time.
        probe_host=None
        if runner_selection.isolated_mode
        else lambda harness: _probe_host(args, harness),
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
    criteria_count = len(work_item.acceptance_criteria)
    unknown = sorted(
        {index for index in args.inapplicable_criteria if not 0 <= index < criteria_count}
    )
    if unknown:
        raise InvalidInputError(
            f"--inapplicable-criterion {unknown} is outside the issue's {criteria_count} "
            "acceptance criteria (0-based)",
            details={"criteria": criteria_count},
        )
    applicable_criteria = tuple(
        index not in set(args.inapplicable_criteria) for index in range(criteria_count)
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

    request = Stage1RunRequest(
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
            cost_budget=None,
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
        runner=runner_selection,
        protected_paths=tuple(args.protected_paths or ()),
    )
    require_explicit_isolated_validation(request)
    return request


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
