"""Bind a Stage 1 request to the configuration snapshot it was compiled from.

``engine build-request`` writes the compiled snapshot into the request document beside the
request. ``engine start`` calls :func:`require_bound_configuration` before it runs anything:
the snapshot's sha256 must be the request's ``configuration_snapshot_digest`` and every
compiled field must equal the value the request actually carries. Inconsistent,
policy-violating or drifted requests are refused with an instruction to rebuild.
Runs that were already started are decoded from the ledger unchanged and never
pass through this check.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from enginery.adapters.git_reader import git_bytes as _git_bytes
from enginery.application.behavioral_checks import (
    BehavioralCheck,
    BehavioralChecksBinding,
    require_base_revision,
)
from enginery.application.effective_config import EffectiveConfig, enforce_snapshot_constraints
from enginery.application.layered_config import (
    LayerDocument,
    load_factory_layer,
    load_workflow_layer,
    parse_layer_document,
)
from enginery.capabilities.atomic import fsync_directory, write_temporary
from enginery.cli.config_locations import (
    MAX_CONFIG_BYTES,
    PROJECT_POLICY_RELATIVE,
    private_state_directory,
    project_policy_path,
    read_project_layer,
    read_user_layer,
    require_project_runner,
    state_home,
    user_config_path,
)
from enginery.cli.setup_files import refuse_links, require_private_path
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import SelectionLayer
from enginery.workflows.run_lifecycle import Stage1RunRequest

EFFECTIVE_CONFIG_KEY = "effective_config"
_REBUILD = "rebuild it with `enginery engine build-request`"

_CUSTODY_WHAT = "behavioral evaluator custody"


def read_base_project_layer(repository: Path, base_revision: str) -> LayerDocument | None:
    """Read the tracked policy blob at the immutable base, never the workspace or worker head."""
    require_base_revision(base_revision)
    if _git_bytes(repository, "cat-file", "-t", base_revision) != b"commit\n":
        raise InvalidInputError("behavioral checks base revision must identify a commit")
    listing = _git_bytes(
        repository, "ls-tree", "-z", base_revision, "--", PROJECT_POLICY_RELATIVE.as_posix()
    )
    if not listing:
        return None
    match = re.fullmatch(
        rb"100(?:644|755) blob ([0-9a-f]{40}|[0-9a-f]{64})\t"
        + re.escape(PROJECT_POLICY_RELATIVE.as_posix().encode())
        + rb"\x00",
        listing,
    )
    if match is None:
        raise InvalidInputError("base project policy must be a tracked regular file, not a link")
    raw = _git_bytes(repository, "cat-file", "blob", match[1].decode("ascii"))
    return parse_layer_document(
        SelectionLayer.PROJECT,
        raw,
        source=f"{base_revision}:{PROJECT_POLICY_RELATIVE.as_posix()}",
        reject_credentials=True,
    )


def checks_at_base(repository: Path, base_revision: str) -> tuple[BehavioralCheck, ...]:
    layer = read_base_project_layer(repository, base_revision)
    return () if layer is None else layer.behavioral_checks or ()


def _expected_binding(request: Stage1RunRequest) -> BehavioralChecksBinding | None:
    if request.behavioral_checks is None:
        return None
    return BehavioralChecksBinding(
        base_revision=request.run.base_revision,
        configuration_snapshot_digest=str(request.run.configuration_snapshot_digest),
        checks=request.behavioral_checks,
    )


def behavioral_checks_path(request: Stage1RunRequest) -> Path | None:
    """The content-addressed private evaluator document, outside every worker source mount."""
    binding = _expected_binding(request)
    if binding is None:
        return None
    path = (
        private_state_directory(request.repository_path)
        / "evaluator"
        / "checks"
        / f"{binding.digest.hex_value}.json"
    )
    for worker_root in (request.repository_path, request.workspace_path):
        if path.is_relative_to(worker_root.resolve()):
            raise InvalidInputError("behavioral evaluator custody must be outside worker mounts")
    root = state_home()
    refuse_links(root.parent, Path(root.name) / path.relative_to(root), what=_CUSTODY_WHAT)
    require_private_path(path, what=_CUSTODY_WHAT)
    return path


def _custody_repair(request: Stage1RunRequest, path: Path) -> str:
    if str(request.run.id).startswith("fr-"):
        command = (
            f"enginery run "
            f"{shlex.quote(request.work_snapshot.work_item.external_reference)} "
            f"--project {shlex.quote(str(request.repository_path))}"
        )
    else:
        command = f"{_REBUILD} before starting the unbound run again"
    return f"repair: rm -- {shlex.quote(str(path))}; then {command}"


@contextmanager
def _custody_lock(path: Path) -> Iterator[None]:
    """Serialize publication and reads, including the temporary hard-link window."""
    lock_path = path.parent / ".lock"
    require_private_path(lock_path, what=_CUSTODY_WHAT)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_mode & 0o077:
            raise InvalidInputError(
                f"behavioral evaluator custody lock is not private: {lock_path}"
            )
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_binding(path: Path, *, repair: str) -> BehavioralChecksBinding:
    require_private_path(path, what=_CUSTODY_WHAT)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o077
            ):
                raise InvalidInputError(
                    f"behavioral evaluator custody requires a private regular file: {path}; "
                    f"{repair}"
                )
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise InvalidInputError(
            f"behavioral evaluator custody is unavailable; {_REBUILD}"
        ) from error
    if len(raw) > MAX_CONFIG_BYTES:
        raise InvalidInputError("behavioral evaluator custody exceeds the 1 MiB limit")
    try:
        return BehavioralChecksBinding.from_state(json.loads(raw))
    except (ValueError, UnicodeError) as error:
        raise InvalidInputError(
            f"behavioral evaluator custody is not valid JSON: {path}; {repair}"
        ) from error


def read_behavioral_checks(request: Stage1RunRequest) -> BehavioralChecksBinding | None:
    """Load the evaluator's copy and verify all identity fields against the bound request."""
    expected = _expected_binding(request)
    path = behavioral_checks_path(request)
    if expected is None or path is None:
        return None
    try:
        with _custody_lock(path):
            actual = _read_binding(path, repair=_custody_repair(request, path))
    except OSError as error:
        raise InvalidInputError(
            f"behavioral evaluator custody is unavailable: {path}; {_REBUILD}"
        ) from error
    if actual != expected:
        raise InvalidInputError("behavioral evaluator custody does not match the bound request")
    return actual


def bind_behavioral_checks(request: Stage1RunRequest) -> BehavioralChecksBinding | None:
    """Freeze base-owned assertions before a worker starts; never overwrite an existing copy."""
    expected = _expected_binding(request)
    path = behavioral_checks_path(request)
    if expected is None or path is None:
        return None
    if checks_at_base(request.repository_path, request.run.base_revision) != expected.checks:
        raise InvalidInputError(
            f"behavioral checks do not match project policy at the base revision; {_REBUILD}"
        )
    raw = (json.dumps(expected.to_state(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_CONFIG_BYTES:
        raise InvalidInputError("behavioral evaluator custody exceeds the 1 MiB limit")
    # Deferred to keep factory composition's request-compiler import acyclic.
    from enginery.cli.factory_collect import ensure_private_directory

    ensure_private_directory(path.parent, what=_CUSTODY_WHAT)
    for directory in (path.parent.parent, path.parent):
        if directory.stat().st_mode & 0o077:
            raise InvalidInputError("behavioral evaluator custody directories must have mode 0700")
    require_private_path(path, what=_CUSTODY_WHAT)
    temporary = write_temporary(path.parent, raw, prefix=".checks-")
    try:
        with _custody_lock(path):
            temporary.chmod(0o600)
            try:
                os.link(temporary, path)
            except FileExistsError:
                actual = _read_binding(path, repair=_custody_repair(request, path))
                if actual != expected:
                    raise InvalidInputError(
                        "behavioral evaluator custody does not match the bound request"
                    ) from None
                return actual
            temporary.unlink()
            fsync_directory(path.parent)
    except OSError as error:
        raise InvalidInputError(f"cannot persist behavioral evaluator custody: {path}") from error
    finally:
        temporary.unlink(missing_ok=True)
    return expected


def request_values(request: Stage1RunRequest) -> dict[str, object]:
    """The compiled fields as the request actually carries them, under their snapshot names."""
    selection = request.execution_selection
    runner = request.runner
    execution = request.execution_configuration
    implementation = request.implementation
    return {
        "harness": execution.harness_provider,
        "model": selection.selected_model,
        "effort": selection.selected_effort,
        "strict_effective": selection.strict_effective,
        "runner_mode": "isolated-oci" if runner.isolated_mode else "trusted-local",
        "runner_image": runner.image,
        "docker_context": runner.docker_context,
        "model_access": runner.model_access,
        "validation_commands": [list(command) for command in request.validation_commands],
        "required_checks": list(request.required_checks),
        "repair_limit": request.repair_limit,
        "time_budget_seconds": implementation.time_budget_seconds,
        "permitted_capabilities": list(implementation.permitted_capabilities),
        "evidence_requirements": list(implementation.evidence_requirements),
        "protected_paths": list(request.protected_paths),
        "github_credential_reference": execution.github_credential_reference,
        "harness_credential_reference": execution.harness_credential_reference,
        "github_executable": execution.github_executable,
        "harness_executable": execution.harness_executable,
    }


def require_bound_configuration(document: object, request: Stage1RunRequest) -> EffectiveConfig:
    """Refuse unless ``request`` is exactly what its bound configuration snapshot compiles to."""
    require_project_runner(
        request.repository_path.resolve(),
        "isolated-oci" if request.runner.isolated_mode else "trusted-local",
    )
    if not isinstance(document, dict) or EFFECTIVE_CONFIG_KEY not in document:
        raise InvalidInputError(
            "this request carries no compiled configuration snapshot (it was built before "
            f"layered configuration, or by hand); {_REBUILD}",
            details={"run_id": str(request.run.id)},
        )
    config = EffectiveConfig.from_state(document[EFFECTIVE_CONFIG_KEY])
    if config.digest != request.run.configuration_snapshot_digest:
        raise InvalidInputError(
            "the request's configuration snapshot does not match its bound "
            f"configuration_snapshot_digest; {_REBUILD}",
            details={
                "run_id": str(request.run.id),
                "bound": str(request.run.configuration_snapshot_digest),
                "snapshot": str(config.digest),
            },
        )
    if config.selection_digest != str(request.execution_selection.digest):
        raise InvalidInputError(
            f"the request's execution selection disagrees with its configuration snapshot; "
            f"{_REBUILD}",
            details={"run_id": str(request.run.id), "field": "execution_selection"},
        )
    for name, carried in request_values(request).items():
        compiled = config.plain_value(name)
        if compiled != carried:
            raise InvalidInputError(
                f"request field {name} disagrees with its configuration snapshot "
                f"(request {carried!r}, snapshot {compiled!r}); {_REBUILD}",
                details={"run_id": str(request.run.id), "field": name},
            )
    if config.behavioral_checks != request.behavioral_checks:
        raise InvalidInputError(
            f"request behavioral checks disagree with its configuration snapshot; {_REBUILD}"
        )
    if (
        request.behavioral_checks is not None
        and checks_at_base(request.repository_path, request.run.base_revision)
        != request.behavioral_checks
    ):
        raise InvalidInputError(
            f"behavioral checks do not match project policy at the base revision; {_REBUILD}"
        )
    enforce_snapshot_constraints(config)
    project_path = project_policy_path(request.repository_path.resolve())
    user_path = user_config_path()
    for source in config.sources:
        if source.layer in {"factory", "workflow"}:
            try:
                packaged = (
                    load_factory_layer()
                    if source.layer == "factory"
                    else load_workflow_layer(str(config.plain_value("profile")), selected_by=None)
                )
                current = packaged.digest
            except (OSError, InvalidInputError):
                current = None
        elif source.layer in {"project", "user"}:
            expected_path = project_path if source.layer == "project" else user_path
            try:
                layer = (
                    read_project_layer(request.repository_path.resolve())
                    if source.layer == "project"
                    else read_user_layer()
                )
                current = None if layer is None else layer.digest
            except (OSError, InvalidInputError):
                raise InvalidInputError(
                    f"configuration changed since the request was built: {expected_path}; "
                    f"{_REBUILD}",
                    details={"file": str(expected_path)},
                ) from None
            if source.source != str(expected_path):
                raise InvalidInputError(
                    f"configuration changed since the request was built: {expected_path} "
                    f"(previously {source.source}); {_REBUILD}",
                    details={"file": str(expected_path)},
                )
        else:
            continue
        if (None if current is None else str(current)) != source.digest:
            raise InvalidInputError(
                f"configuration changed since the request was built: {source.source}; {_REBUILD}",
                details={"file": source.source},
            )
    return config


__all__ = [
    "EFFECTIVE_CONFIG_KEY",
    "behavioral_checks_path",
    "bind_behavioral_checks",
    "checks_at_base",
    "read_base_project_layer",
    "read_behavioral_checks",
    "request_values",
    "require_bound_configuration",
]
