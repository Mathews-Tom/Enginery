"""Bind a Stage 1 request to the configuration snapshot it was compiled from.

``stage1 build-request`` writes the compiled snapshot into the request document beside the
request. ``stage1 start`` calls :func:`require_bound_configuration` before it runs anything:
the snapshot's sha256 must be the request's ``configuration_snapshot_digest`` and every
compiled field must equal the value the request actually carries. Inconsistent,
policy-violating or drifted requests are refused with an instruction to rebuild.
Runs that were already started are decoded from the ledger unchanged and never
pass through this check.
"""

from __future__ import annotations

from enginery.application.effective_config import EffectiveConfig, enforce_snapshot_constraints
from enginery.application.layered_config import load_factory_layer, load_workflow_layer
from enginery.cli.config_locations import (
    project_policy_path,
    read_project_layer,
    read_user_layer,
    user_config_path,
)
from enginery.domain.errors import InvalidInputError
from enginery.workflows.stage1 import Stage1RunRequest

EFFECTIVE_CONFIG_KEY = "effective_config"
_REBUILD = "rebuild the request with the current `enginery stage1 build-request`"


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
                f"configuration changed since the request was built: {source.source}; "
                "rebuild with enginery stage1 build-request",
                details={"file": source.source},
            )
    return config


__all__ = ["EFFECTIVE_CONFIG_KEY", "request_values", "require_bound_configuration"]
