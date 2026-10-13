"""The worker environment allowlist: only named keys, and never a credential-shaped one."""

from __future__ import annotations

import pytest

from enginery.application.worker_environment import (
    LAUNCHER_KEYS,
    TRUSTED_LOCAL_WORKER_KEYS,
    build_worker_environment,
    is_credential_shaped,
)
from enginery.domain.errors import InvalidInputError

AMBIENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/operator",
    "GH_TOKEN": "gh-secret",
    "GITHUB_TOKEN": "github-secret",
    "GITHUB_PAT": "pat-secret",
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "AWS_PROFILE": "prod",
    "AZURE_CLIENT_SECRET": "azure-secret",
    "GOOGLE_APPLICATION_CREDENTIALS": "/keys/gcp.json",
    "UV_PUBLISH_TOKEN": "pypi-secret",
    "NPM_TOKEN": "npm-secret",
    "PYPI_API_TOKEN": "pypi-secret-2",
    "ANTHROPIC_API_KEY": "anthropic-secret",
    "DB_PASSWORD": "db-secret",
    "ENGINERY_AMBIENT_MARKER": "marker",
}


def test_only_permitted_keys_the_source_defines_are_present() -> None:
    environment = build_worker_environment(AMBIENT, ("PATH", "HOME", "LANG"))

    assert environment == {"PATH": "/usr/bin:/bin", "HOME": "/home/operator"}


def test_every_ambient_credential_and_unlisted_variable_is_absent_by_omission() -> None:
    environment = build_worker_environment(AMBIENT, TRUSTED_LOCAL_WORKER_KEYS)

    assert set(environment) <= set(TRUSTED_LOCAL_WORKER_KEYS)
    assert set(environment) == {"PATH", "HOME"}, "only the ambient keys on the allowlist survive"
    for name in AMBIENT:
        if name not in {"PATH", "HOME"}:
            assert name not in environment


def test_the_result_is_a_new_mapping_and_the_source_is_not_modified() -> None:
    source = dict(AMBIENT)

    environment = build_worker_environment(source, ("PATH",))
    environment["PATH"] = "changed"

    assert source == AMBIENT


@pytest.mark.parametrize(
    "key",
    [
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_HOST",
        "GITHUB_REPOSITORY",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "AWS_PROFILE",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "UV_PUBLISH_TOKEN",
        "UV_PUBLISH_USERNAME",
        "NPM_TOKEN",
        "PYPI_API_TOKEN",
        "PYPI_USER",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "DB_PASSWORD",
        "SERVICE_SECRET",
        "KEY_PASSPHRASE",
        "MY_CREDENTIALS",
        "SIGNING_PRIVATE_KEY",
        "gh_token",
        "Github_Token",
        "ssh_auth_sock",
    ],
)
def test_permitting_a_credential_shaped_key_is_refused_even_when_the_source_lacks_it(
    key: str,
) -> None:
    with pytest.raises(InvalidInputError, match="credential-shaped"):
        build_worker_environment({}, ("PATH", key))
    with pytest.raises(InvalidInputError, match="credential-shaped"):
        build_worker_environment({key: "value"}, (key,))


@pytest.mark.parametrize("key", ["", "  ", " PATH", "PATH ", "A=B", "NUL\x00NAME"])
def test_a_blank_or_malformed_key_name_is_refused(key: str) -> None:
    with pytest.raises(InvalidInputError, match="non-blank variable name"):
        build_worker_environment(AMBIENT, (key,))


@pytest.mark.parametrize(
    "key",
    [
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "CLAUDE_CONFIG_DIR",
        "PI_CODING_AGENT_DIR",
        "XDG_CONFIG_HOME",
        "SSL_CERT_FILE",
        "DOCKER_HOST",
        "DOCKER_CERT_PATH",
        "DOCKER_TLS_VERIFY",
        "XDG_RUNTIME_DIR",
    ],
)
def test_ordinary_names_are_not_credential_shaped(key: str) -> None:
    assert not is_credential_shaped(key)


def test_the_shipped_allowlists_are_exactly_the_documented_ones_and_credential_free() -> None:
    assert TRUSTED_LOCAL_WORKER_KEYS == (
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
    assert LAUNCHER_KEYS == (
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
    for key in (*TRUSTED_LOCAL_WORKER_KEYS, *LAUNCHER_KEYS):
        assert not is_credential_shaped(key)
    # Building from both lists must not raise: a list that names a credential would.
    build_worker_environment(AMBIENT, TRUSTED_LOCAL_WORKER_KEYS)
    build_worker_environment(AMBIENT, LAUNCHER_KEYS)
