"""Keep a git invocation on the repository it was pointed at.

Git lets the environment override the repository a command works on: ``GIT_DIR`` and its
companions win over the working directory. A process started by ``git rebase --exec`` (or a
git hook) inherits them, so a fixture that runs ``git init`` or ``git config user.name`` in
its own temporary directory would silently write into the enclosing repository's shared
configuration instead. Every place that runs git against a repository chosen by path must
therefore start from an environment without these variables.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

REPOSITORY_REDIRECT_VARIABLES: frozenset[str] = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_WORK_TREE",
    }
)


def repository_isolated_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``source`` (default: the process environment) without the redirecting variables."""
    environment = os.environ if source is None else source
    return {
        key: value for key, value in environment.items() if key not in REPOSITORY_REDIRECT_VARIABLES
    }


def scrub_repository_variables() -> tuple[str, ...]:
    """Remove the redirecting variables from this process's environment; return what was removed."""
    removed = tuple(sorted(key for key in REPOSITORY_REDIRECT_VARIABLES if key in os.environ))
    for key in removed:
        del os.environ[key]
    return removed


__all__ = [
    "REPOSITORY_REDIRECT_VARIABLES",
    "repository_isolated_environment",
    "scrub_repository_variables",
]
