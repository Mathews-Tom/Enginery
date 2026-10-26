"""``enginery engine build-request``: compose a valid ``--request`` document.

The command builds the same `Stage1RunRequest` that `engine start` decodes
(`enginery.workflows.run_lifecycle.stage1_request_from_state`) and writes the identical
JSON shape. The source issue is read, not typed: the work item, its source
revision, and its declared classification (the canonical `enginery/work-kind/*`
and `enginery/risk/*` labels) come through the same GitHub adapter that
`engine next --advance` uses to qualify the run, and are bound exactly as the
adapter returns them, so the request carries the digest qualification
recomputes from the labelled issue. An issue whose labels are missing,
ambiguous, or outside the declared vocabulary is refused with the adapter's own
error and nothing is written.

Flags are the run layer of the layered configuration: omitted, the project
(``.enginery/project.toml``), user (``user.toml``), workflow-profile and factory layers
decide, and :mod:`enginery.application.effective_config` compiles them. The sha256 of the
compiled snapshot is the request's ``configuration_snapshot_digest`` (never supplied by
the operator) and the snapshot travels in the request document so ``engine start`` can
verify the request against it. ``cost_budget=None`` (no supported harness enforces a
hard-dollar budget, so one is refused rather than carried). The ``no-capabilities-locked``
and ``local-environment`` digest sentinels stand for a run with no bound capability lock
or environment manifest.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from enginery.adapters.claude_code import probe_installed_host as probe_installed_claude
from enginery.adapters.github import GitHubAdapterConfig, GitHubWorkLedger
from enginery.adapters.omp import probe_installed_host as probe_installed_omp
from enginery.application.effective_config import (
    CompiledConfig,
    compile_effective_config,
)
from enginery.application.layered_config import (
    DEFAULT_PROFILE,
    RUNNER_MODES,
    LayerDocument,
    run_layer,
)
from enginery.capabilities.serialization import read_lock
from enginery.cli.config_binding import EFFECTIVE_CONFIG_KEY, checks_at_base
from enginery.cli.config_locations import (
    read_project_layer,
    read_user_layer,
    record_absent_sources,
    require_project_runner,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.ids import OperationId, RunId, WorkflowDefinitionId
from enginery.domain.run import Run, RunState
from enginery.domain.runner import MODEL_ACCESS_MODES
from enginery.workflows.issue_to_pr import stage1_work_manifest
from enginery.workflows.run_lifecycle import (
    Stage1ExecutionConfiguration,
    Stage1ImplementationRequest,
    Stage1RunRequest,
    first_attempt_generation,
    implementation_attempt_id,
    require_explicit_isolated_validation,
)

_NO_CAPABILITY_LOCK_DIGEST = Digest.of_bytes(b"no-capabilities-locked")
_LOCAL_ENVIRONMENT_DIGEST = Digest.of_bytes(b"local-environment")
_DEFAULT_CAPABILITY_LOCKFILE = Path(".enginery/capabilities.lock.json")


@dataclass(frozen=True, slots=True)
class RequestInputs:
    """Compiler inputs shared by the CLI and factory callers.

    Defaults match ``engine build-request``. Compilation reads the source issue and
    configuration but does not write ``output``; the calling command owns that write.
    """

    output: Path
    run_id: str
    repository: str
    external_reference: str
    base_revision: str
    repository_path: Path
    workspace_path: Path
    artifact_root: Path
    inapplicable_criteria: list[int] = field(default_factory=list)
    policy_set_version: str = "policy-v1"
    capability_lockfile: Path = _DEFAULT_CAPABILITY_LOCKFILE
    environment_manifest_digest: str | None = None
    environment_manifest_file: Path | None = None
    base_branch: str = "main"
    head_branch: str | None = None
    validation_commands: list[str] | None = None
    required_checks: list[str] | None = None
    repair_limit: int | None = None
    protected_paths: list[str] | None = None
    implementation_attempt_id: str | None = None
    implementation_operation_id: str | None = None
    implementation_time_budget_seconds: int | None = None
    implementation_cost_budget: str | None = None
    permitted_capabilities: list[str] | None = None
    evidence_requirements: list[str] | None = None
    github_repository: str | None = None
    github_credential_reference: str | None = None
    github_executable: str | None = None
    harness_provider: str | None = None
    harness_credential_reference: str | None = None
    harness_executable: str | None = None
    model: str | None = None
    effort: str | None = None
    strict_effective: bool = False
    profile: str | None = None
    runner: str | None = None
    runner_image: str | None = None
    docker_context: str | None = None
    model_access: str | None = None


def add_build_request_parser(
    stage1_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register ``engine build-request``'s flags on the shared engine subparsers."""
    parser = stage1_subparsers.add_parser(
        "build-request",
        help="Engine ledger: Write a bound request document for one GitHub issue.",
        description="Engine ledger: Compile a request for one GitHub issue.",
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
    parser.add_argument("--repair-limit", type=int, default=None)
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
    parser.add_argument("--implementation-time-budget-seconds", type=int, default=None)
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
    parser.add_argument("--github-credential-reference", default=None)
    parser.add_argument("--github-executable", default=None)
    parser.add_argument(
        "--harness-provider",
        choices=("omp", "claude-code"),
        default=None,
        help="The harness to run. Omitted, the project, user, workflow and factory layers "
        "decide (the factory default is omp).",
    )
    parser.add_argument("--harness-credential-reference", default=None)
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
        "--profile",
        default=None,
        help="The packaged workflow profile to compile against. Omitted, the project or user "
        f"layer decides, else the factory default ({DEFAULT_PROFILE}).",
    )
    parser.add_argument(
        "--runner",
        default=None,
        choices=RUNNER_MODES,
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
        default=None,
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


def _digest_override(args: RequestInputs, *, flag: str, default: Digest) -> Digest:
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


def _reject_cost_budget(args: RequestInputs) -> None:
    """Refuse a hard-dollar budget instead of recording a number nothing enforces."""
    if args.implementation_cost_budget is not None:
        raise InvalidInputError(
            "no supported harness enforces a hard-dollar budget; none can be bound "
            "(the time budget, --implementation-time-budget-seconds, is enforced)",
            details={"value": args.implementation_cost_budget},
        )


def _probe_host(harness: str, executable: str) -> InstalledHost | None:
    """Observe the installed host the run will use, only when its controls are pinned."""
    if harness == "claude-code":
        return probe_installed_claude(executable)
    if harness == "omp":
        return probe_installed_omp(executable)
    raise InvalidInputError(
        "Stage 1 harness_provider is not a configured harness", details={"harness": harness}
    )


def _run_layer(args: RequestInputs) -> LayerDocument:
    """The run layer: exactly the choices the operator made on the command line."""
    values: dict[str, object] = {}
    for name, flag in (
        ("profile", args.profile),
        ("harness", args.harness_provider),
        ("runner_mode", args.runner),
        ("runner_image", args.runner_image),
        ("docker_context", args.docker_context),
        ("model_access", args.model_access),
        ("validation_commands", args.validation_commands),
        ("required_checks", args.required_checks),
        ("repair_limit", args.repair_limit),
        ("time_budget_seconds", args.implementation_time_budget_seconds),
        ("permitted_capabilities", args.permitted_capabilities),
        ("evidence_requirements", args.evidence_requirements),
        ("protected_paths", args.protected_paths),
        ("github_credential_reference", args.github_credential_reference),
        ("harness_credential_reference", args.harness_credential_reference),
        ("github_executable", args.github_executable),
        ("harness_executable", args.harness_executable),
    ):
        if flag is not None:
            values[name] = str(flag) if name.endswith("executable") else flag
    # A model or effort named on the command line is a manual pin: exact, never substituted.
    for name, flag in (("model", args.model), ("effort", args.effort)):
        if flag is not None:
            values[name] = flag
            values[f"{name}_pinned"] = True
    if args.strict_effective:
        values["strict_effective"] = True
    return run_layer(values)


@dataclass(frozen=True, slots=True)
class BuiltRequest:
    """A compiled request: the request and the configuration snapshot it is bound to."""

    request: Stage1RunRequest
    compiled: CompiledConfig

    def document(self) -> dict[str, object]:
        """The ``--request`` document: the request plus the snapshot ``engine start`` verifies."""
        return {
            **self.request.initial_state(),
            EFFECTIVE_CONFIG_KEY: self.compiled.config.to_state(),
        }


def build_request(args: argparse.Namespace) -> BuiltRequest:
    """Adapt the public CLI namespace to the shared request compiler."""
    return compile_request(
        RequestInputs(**{item.name: getattr(args, item.name) for item in fields(RequestInputs)})
    )


def compile_request(inputs: RequestInputs) -> BuiltRequest:
    """Compile a durable Stage 1 request from layered configuration and a source issue."""
    args = inputs
    _reject_cost_budget(args)
    compiled = compile_effective_config(
        project=read_project_layer(args.repository_path.resolve()),
        user=read_user_layer(),
        run=_run_layer(args),
        probe_host=_probe_host,
        behavioral_checks=checks_at_base(args.repository_path.resolve(), args.base_revision),
        admit_runner=lambda mode: require_project_runner(args.repository_path.resolve(), mode),
    )
    compiled = record_absent_sources(compiled, args.repository_path)
    config = compiled.config
    selection = compiled.selection
    repair_limit = config.integer("repair_limit")
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
    if repair_limit > 0:
        # Repair attempt ids are always derived from the run id, whatever the first attempt's
        # id is, so the last one the run may need must be valid for it to be able to repair.
        implementation_attempt_id(run_id, first_generation + repair_limit)
    head_branch = args.head_branch or f"enginery/{run_id}"
    implementation_operation_id = OperationId(
        args.implementation_operation_id or f"implement:{run_id}"
    )
    manifest = stage1_work_manifest()

    # Everything the source decides is read once the operator's own flags are valid, and is
    # bound exactly as the adapter returns it.
    github = GitHubAdapterConfig(
        repository=args.github_repository or args.repository,
        credential_reference=config.text("github_credential_reference"),
        executable=config.text("github_executable"),
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
        # The digest of the compiled snapshot is the only configuration binding: an
        # operator-supplied digest would be a bypass of the compiler.
        configuration_snapshot_digest=config.digest,
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
        validation_commands=config.commands(),
        applicable_criteria=applicable_criteria,
        required_checks=config.strings("required_checks"),
        repair_limit=repair_limit,
        implementation=Stage1ImplementationRequest(
            attempt_id=attempt_id,
            operation_id=implementation_operation_id,
            time_budget_seconds=config.integer("time_budget_seconds"),
            cost_budget=None,
            permitted_capabilities=config.strings("permitted_capabilities"),
            evidence_requirements=config.strings("evidence_requirements"),
        ),
        execution_configuration=Stage1ExecutionConfiguration(
            github_repository=github.repository,
            github_credential_reference=github.credential_reference,
            github_executable=github.executable,
            harness_provider=selection.harness,
            harness_credential_reference=config.text("harness_credential_reference"),
            harness_executable=config.text("harness_executable"),
            artifact_root=args.artifact_root.resolve(),
        ),
        execution_selection=selection,
        runner=compiled.runner,
        protected_paths=config.strings("protected_paths"),
        behavioral_checks=config.behavioral_checks,
    )
    require_explicit_isolated_validation(request)
    return BuiltRequest(request=request, compiled=compiled)


def run_build_request(args: argparse.Namespace) -> int:
    built = build_request(args)
    request = built.request
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(built.document(), indent=2), encoding="utf-8")
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


__all__ = [
    "BuiltRequest",
    "RequestInputs",
    "add_build_request_parser",
    "build_request",
    "compile_request",
    "run_build_request",
]
