"""Where Enginery's configuration lives, resolved in one place, and how it is read.

* Project policy: ``<project>/.enginery/project.toml`` (tracked, inside the protected
  ``.enginery/**`` set, so a worker cannot alter its own policy without an operator decision).
* User preferences: ``$XDG_CONFIG_HOME/enginery/user.toml`` (default
  ``~/.config/enginery/user.toml``). User-authored; setup never creates or edits it.
* Private machine state: ``$XDG_STATE_HOME/enginery/`` (default ``~/.local/state/enginery/``).

An XDG variable that is set must be an absolute path; a relative one is refused rather than
resolved against whatever directory the command happened to run in.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from enginery.application.layered_config import ConfigError, LayerDocument, parse_layer_document
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import SelectionLayer

PROJECT_POLICY_RELATIVE = Path(".enginery") / "project.toml"
USER_CONFIG_RELATIVE = Path("enginery") / "user.toml"


def _xdg_home(env: Mapping[str, str], variable: str, default: tuple[str, ...]) -> Path:
    value = env.get(variable)
    if value:
        path = Path(value)
        if not path.is_absolute():
            raise InvalidInputError(
                f"{variable} must be an absolute path; refusing the relative value",
                details={"variable": variable, "value": value},
            )
        return path
    home = env.get("HOME")
    if not home or not Path(home).is_absolute():
        raise InvalidInputError(
            f"neither {variable} nor an absolute HOME is set; cannot locate Enginery's "
            "private configuration",
            details={"variable": variable},
        )
    return Path(home, *default)


def config_home(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME`` (default ``~/.config``)."""
    return _xdg_home(os.environ if env is None else env, "XDG_CONFIG_HOME", (".config",))


def state_home(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME`` (default ``~/.local/state``)."""
    return _xdg_home(os.environ if env is None else env, "XDG_STATE_HOME", (".local", "state"))


def user_config_path(env: Mapping[str, str] | None = None) -> Path:
    return config_home(env) / USER_CONFIG_RELATIVE


def project_policy_path(project: Path) -> Path:
    return project / PROJECT_POLICY_RELATIVE


def project_identity(project: Path) -> str:
    """A stable, path-derived identity of a project for its private state directory."""
    return hashlib.sha256(str(project.resolve()).encode("utf-8")).hexdigest()[:16]


def private_state_directory(project: Path, env: Mapping[str, str] | None = None) -> Path:
    """The private machine-state directory for one project."""
    return state_home(env) / "enginery" / "projects" / project_identity(project)


def require_plain_path(project: Path, relative: Path, *, what: str) -> None:
    """Refuse a symbolic link on ``relative`` below ``project`` (each existing component)."""
    current = project
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise InvalidInputError(
                f"{what} is a symbolic link; refusing to read or write through it",
                details={"path": str(current)},
            )
        if not current.exists():
            return


def read_project_layer(project: Path) -> LayerDocument | None:
    """The project policy layer, absent when the file does not exist, strict when it does."""
    path = project_policy_path(project)
    require_plain_path(project, PROJECT_POLICY_RELATIVE, what="the project policy path")
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigError(
            f"cannot be read: {error.strerror or error}", file=str(path), key="<document>"
        ) from error
    return parse_layer_document(
        SelectionLayer.PROJECT, raw, source=str(path), reject_credentials=True
    )


def read_user_layer(env: Mapping[str, str] | None = None) -> LayerDocument | None:
    """Read user preferences, following symlinks; refuse dangling or non-regular targets."""
    path = user_config_path(env)
    if not os.path.lexists(path):
        return None
    if not path.is_file():
        raise ConfigError("must resolve to a regular file", file=str(path), key="<document>")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigError(
            f"cannot be read: {error.strerror or error}", file=str(path), key="<document>"
        ) from error
    return parse_layer_document(SelectionLayer.USER, raw, source=str(path))


__all__ = [
    "PROJECT_POLICY_RELATIVE",
    "config_home",
    "private_state_directory",
    "project_identity",
    "project_policy_path",
    "read_project_layer",
    "read_user_layer",
    "require_plain_path",
    "state_home",
    "user_config_path",
]
