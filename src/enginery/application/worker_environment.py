"""Explicit environment allowlists for every process that launches or runs a worker.

A worker, and the host process that launches it, never inherits the operator's ambient
environment: that environment carries SCM, factory, broker, cloud and package-publishing
credentials. A process receives exactly the keys a caller names, taken from a source mapping,
and a key that is credential-shaped can never be named at all. Granting a credential to a worker
is therefore a decision made in code that fails loudly, not an omission that happens to work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from enginery.domain.errors import InvalidInputError

TRUSTED_LOCAL_WORKER_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
    "CLAUDE_CONFIG_DIR",
    "PI_CODING_AGENT_DIR",
    "PI_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
"""What a trusted-local worker inherits from its operator.

Trusted-local runs the harness as the operator, with the operator's home and so with whatever
the harness keeps there: this list is what makes that explicit and bounded, and it is declared
weaker than the isolated runner, not containment.
"""

LAUNCHER_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "XDG_RUNTIME_DIR",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)
"""What the host process that launches an isolated worker inherits.

The launcher only needs to find and reach the container engine. The container's own environment
is a separate, explicit list that never derives from the host's.
"""

_CREDENTIAL_NAME_FRAGMENTS: tuple[str, ...] = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "API_KEY",
    "APIKEY",
    "CREDENTIAL",
    "PRIVATE_KEY",
)
_CREDENTIAL_NAME_PREFIXES: tuple[str, ...] = (
    "GH_",
    "GITHUB_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "UV_PUBLISH_",
    "PYPI_",
)
_CREDENTIAL_NAMES: frozenset[str] = frozenset({"SSH_AUTH_SOCK", "SSH_AGENT_PID"})


def is_credential_shaped(name: str) -> bool:
    """Whether an environment variable name looks like it carries a credential.

    The check is on the name, case-insensitively, because the value of a credential is not
    something a policy can recognise reliably: a name that says token, secret, password, key
    or credential, a source-host or cloud-provider prefix, a package-publishing prefix, or the
    ssh agent socket is treated as one.
    """
    upper = name.upper()
    return (
        upper in _CREDENTIAL_NAMES
        or upper.startswith(_CREDENTIAL_NAME_PREFIXES)
        or any(fragment in upper for fragment in _CREDENTIAL_NAME_FRAGMENTS)
    )


def build_worker_environment(
    source: Mapping[str, str], permitted_keys: Iterable[str]
) -> dict[str, str]:
    """Return exactly the permitted keys that ``source`` defines, and nothing else.

    A permitted key that is blank or credential-shaped is refused even when ``source`` does not
    define it: permission is judged on the allowlist, never on what happens to be present, so a
    list that names a credential fails the same way on every machine.
    """
    permitted = tuple(permitted_keys)
    for key in permitted:
        if not key.strip() or key != key.strip() or "=" in key or "\x00" in key:
            raise InvalidInputError(
                "a permitted environment key must be a non-blank variable name",
                details={"key": key},
            )
        if is_credential_shaped(key):
            raise InvalidInputError(
                "a credential-shaped environment variable can never be permitted for a worker",
                details={"key": key},
            )
    return {key: source[key] for key in permitted if key in source}


__all__ = [
    "LAUNCHER_KEYS",
    "TRUSTED_LOCAL_WORKER_KEYS",
    "build_worker_environment",
    "is_credential_shaped",
]
