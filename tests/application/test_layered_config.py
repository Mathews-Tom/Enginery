from __future__ import annotations

import pytest

from enginery.application.layered_config import (
    ConfigError,
    load_workflow_layer,
    parse_layer_document,
    run_layer,
)
from enginery.domain.execution_selection import SelectionLayer


def _project(text: str, *, source: str = "/repo/.enginery/project.toml"):
    return parse_layer_document(
        SelectionLayer.PROJECT, text.encode(), source=source, reject_credentials=True
    )


def test_a_valid_document_states_only_what_it_specifies() -> None:
    document = _project(
        'schema_version = 1\n[execution]\nharness = "omp"\n'
        '[validation]\ncommands = ["uv run pytest -q", "ruff check"]\nrepair_limit = 2\n'
    )

    assert document.values == {
        "harness": "omp",
        "validation_commands": [["uv", "run", "pytest", "-q"], ["ruff", "check"]],
        "repair_limit": 2,
    }


@pytest.mark.parametrize(
    ("text", "key", "allowed"),
    [
        ("schema_version = 1\nsurprise = 1\n", "surprise", "schema_version"),
        ("schema_version = 1\n[surprise]\nx = 1\n", "surprise", "execution"),
        ("schema_version = 1\n[execution]\nplanet = 1\n", "execution.planet", "harness"),
        ("schema_version = 2\n", "schema_version", "1"),
        ("[execution]\nharness = 'omp'\n", "schema_version", "1"),
        ('schema_version = 1\n[validation]\nrepair_limit = "2"\n', "validation.repair_limit", ""),
        ("schema_version = 1\n[validation]\nrepair_limit = true\n", "validation.repair_limit", ""),
        ("schema_version = 1\n[validation]\nrepair_limit = -1\n", "validation.repair_limit", ""),
        ('schema_version = 1\n[runner]\nmode = "bare-metal"\n', "runner.mode", "trusted-local"),
        ('schema_version = 1\n[validation]\ncommands = "make"\n', "validation.commands", ""),
        ("schema_version = 1\n[validation]\ncommands = []\n", "validation.commands", ""),
        ("schema_version = 1\n[execution]\nmodel_pinned = 'yes'\n", "execution.model_pinned", ""),
        ('schema_version = 1\nprofile = "nope"\nbroken = [\n', "<document>", ""),
    ],
)
def test_an_invalid_document_is_refused_naming_file_key_and_allowed_values(
    text: str, key: str, allowed: str
) -> None:
    with pytest.raises(ConfigError) as refused:
        _project(text)

    error = refused.value
    assert error.file == "/repo/.enginery/project.toml"
    assert error.key == key
    assert str(error).startswith(f"/repo/.enginery/project.toml: {key}: ")
    if allowed:
        assert allowed in error.allowed


@pytest.mark.parametrize(
    "text",
    [
        'schema_version = 1\n[credentials]\ngithub_token = "x"\n',
        'schema_version = 1\n[credentials]\ngithub = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n',
        'schema_version = 1\n[executables]\ngithub = "-----BEGIN RSA PRIVATE KEY-----"\n',
        'schema_version = 1\n[implementation]\nevidence_requirements = ["sk-abcdefghijklmnopqrstuvwx"]\n',
    ],
)
def test_project_policy_refuses_credential_shaped_keys_and_values(text: str) -> None:
    with pytest.raises(ConfigError, match="credential-shaped"):
        _project(text)


def test_a_credential_reference_stays_an_opaque_name() -> None:
    document = _project('schema_version = 1\n[credentials]\ngithub = "operator-gh-cli"\n')

    assert document.values == {"github_credential_reference": "operator-gh-cli"}
    with pytest.raises(ConfigError, match="opaque credential reference"):
        _project('schema_version = 1\n[credentials]\ngithub = "has spaces and ! punctuation"\n')


def test_user_preferences_are_not_scanned_for_credential_shaped_values() -> None:
    document = parse_layer_document(
        SelectionLayer.USER,
        b'schema_version = 1\n[executables]\nharness = "/opt/secret-tools/omp"\n',
        source="/home/u/.config/enginery/user.toml",
    )

    assert document.values == {"harness_executable": "/opt/secret-tools/omp"}


def test_an_unknown_workflow_profile_is_refused_naming_the_selecting_file() -> None:
    selector = _project('schema_version = 1\nprofile = "reckless"\n')

    with pytest.raises(ConfigError) as refused:
        load_workflow_layer("reckless", selected_by=selector)

    assert refused.value.file == selector.source
    assert refused.value.key == "profile"
    assert set(refused.value.allowed) == {"careful", "standard"}


def test_a_run_override_is_validated_like_any_layer() -> None:
    with pytest.raises(ConfigError) as refused:
        run_layer({"repair_limit": -3})
    assert refused.value.file == "command line"
    assert refused.value.key == "repair_limit"

    with pytest.raises(ConfigError, match="unknown run override"):
        run_layer({"flavour": "mint"})
