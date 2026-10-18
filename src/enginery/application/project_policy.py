"""Render a project policy file from the explicit choices of guided setup.

Pure and deterministic: the same choices always render the same bytes, which is what makes
``enginery init`` idempotent. The text is user-owned once written; setup never rewrites it.
Every rendered document is a valid layer document (the caller parses it back through
:func:`enginery.application.layered_config.parse_layer_document` before using it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

BUILTIN_CAPABILITIES: frozenset[str] = frozenset({"git"})
"""Capability names the factory provides itself; any other permitted name is a package."""

_HEADER = (
    "# Enginery project policy (tracked). Written once by `enginery init`; from then on\n"
    "# it is yours: setup never edits it again and `enginery uninstall` keeps it if you\n"
    "# change it. Secrets never belong here; name opaque credential references only.\n"
)


@dataclass(frozen=True, slots=True)
class SetupChoices:
    """What the operator chose; nothing here is defaulted by setup except the empty."""

    runner_mode: str
    profile: str | None = None
    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    runner_image: str | None = None
    model_access: str | None = None
    validation_commands: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()


def _string(value: str) -> str:
    return json.dumps(value)


def _array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_string(value) for value in values) + "]"


def render_project_policy(choices: SetupChoices) -> bytes:
    """The project policy text for ``choices``."""
    lines = [_HEADER, "schema_version = 1"]
    if choices.profile is not None:
        lines.append(f"profile = {_string(choices.profile)}")
    execution = [
        f"{key} = {_string(value)}"
        for key, value in (
            ("harness", choices.harness),
            ("model", choices.model),
            ("effort", choices.effort),
        )
        if value is not None
    ]
    if execution:
        lines += ["", "[execution]", *execution]
    runner = [f"mode = {_string(choices.runner_mode)}"]
    for key, value in (
        ("image", choices.runner_image),
        ("model_access", choices.model_access),
    ):
        if value is not None:
            runner.append(f"{key} = {_string(value)}")
    lines += ["", "[runner]", *runner]
    validation = []
    if choices.validation_commands:
        validation.append(f"commands = {_array(choices.validation_commands)}")
    if choices.required_checks:
        validation.append(f"required_checks = {_array(choices.required_checks)}")
    if validation:
        lines += ["", "[validation]", *validation]
    if choices.capabilities:
        names = tuple(dict.fromkeys(("git", *choices.capabilities)))
        lines += ["", "[implementation]", f"permitted_capabilities = {_array(names)}"]
    if choices.protected_paths:
        lines += ["", "[protection]", f"paths = {_array(choices.protected_paths)}"]
    return ("\n".join(lines) + "\n").encode("utf-8")


__all__ = ["BUILTIN_CAPABILITIES", "SetupChoices", "render_project_policy"]
