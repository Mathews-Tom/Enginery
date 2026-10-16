from __future__ import annotations

import pytest

from enginery.application.effective_config import (
    MERGED,
    UNSET,
    EffectiveConfig,
    compile_effective_config,
)
from enginery.application.layered_config import (
    ConfigError,
    LayerDocument,
    parse_layer_document,
    run_layer,
)
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import SelectionLayer

_RUNNER = '[runner]\nmode = "trusted-local"\n'


def _doc(layer: SelectionLayer, body: str) -> LayerDocument:
    return parse_layer_document(
        layer, f"schema_version = 1\n{body}".encode(), source=f"{layer.value}.toml"
    )


def _compile(
    *,
    project: str | None = None,
    user: str | None = None,
    run: dict[str, object] | None = None,
):
    return compile_effective_config(
        project=None if project is None else _doc(SelectionLayer.PROJECT, project),
        user=None if user is None else _doc(SelectionLayer.USER, user),
        run=run_layer({"runner_mode": "trusted-local", **(run or {})}),
    )


def test_every_field_resolves_run_over_project_over_user_over_workflow_over_factory() -> None:
    project = "[validation]\nrepair_limit = 4\n[implementation]\ntime_budget_seconds = 400\n"
    user = "[validation]\nrepair_limit = 3\n[implementation]\ntime_budget_seconds = 300\n"
    profile = 'profile = "careful"\n'  # workflow layer: repair_limit 2, time budget 3600

    run_wins = _compile(
        project=profile + project,
        user=user,
        run={"repair_limit": 5, "time_budget_seconds": 500},
    ).config
    project_wins = _compile(project=profile + project, user=user).config
    user_wins = _compile(project=profile, user=user).config
    workflow_wins = _compile(project=profile).config
    factory_wins = _compile().config

    for config, repair, budget, winner in (
        (run_wins, 5, 500, "run"),
        (project_wins, 4, 400, "project"),
        (user_wins, 3, 300, "user"),
        (workflow_wins, 2, 3600, "workflow"),
        (factory_wins, 1, 1800, "factory"),
    ):
        assert config.value("repair_limit") == repair
        assert config.value("time_budget_seconds") == budget
        assert config.field("repair_limit").winner == winner
        assert config.field("time_budget_seconds").winner == winner

    chain = run_wins.field("repair_limit").chain
    assert [(entry.layer, entry.value) for entry in chain] == [
        ("run", 5),
        ("project", 4),
        ("user", 3),
        ("workflow", 2),
        ("factory", 1),
    ]
    assert [entry.layer for entry in run_wins.field("repair_limit").shadowed] == [
        "project",
        "user",
        "workflow",
        "factory",
    ]
    assert chain[1].source == "project.toml" and chain[1].source_digest is not None


def test_harness_model_and_effort_follow_precedence_through_the_selection_resolver() -> None:
    project = (
        '[execution]\nharness = "omp"\nmodel = "anthropic/claude-sonnet-4-5"\neffort = "low"\n'
    )
    user = '[execution]\nmodel = "openai/gpt-5"\neffort = "high"\n'

    from_project = _compile(project=project, user=user).config
    from_run = _compile(
        project=project, user=user, run={"effort": "medium", "effort_pinned": True}
    ).config

    assert from_project.value("model") == "anthropic/claude-sonnet-4-5"
    assert from_project.field("model").winner == "project"
    assert [(e.layer, e.value) for e in from_project.field("effort").chain] == [
        ("project", "low"),
        ("user", "high"),
    ]
    assert from_run.value("effort") == "medium"
    assert from_run.field("effort").winner == "run"
    assert from_run.field("effort").chain[0].pinned is True
    assert from_run.field("model").winner == "project"


def test_a_layer_naming_another_harness_contributes_nothing_to_this_harness() -> None:
    config = _compile(
        project='[execution]\nharness = "claude-code"\nmodel = "claude-opus-4-1"\n',
        run={"harness": "omp"},
    ).config

    assert config.value("harness") == "omp"
    assert config.value("model") is None
    (entry,) = config.field("model").chain
    assert entry.layer == "project" and entry.note is not None


def test_a_pin_from_a_layer_naming_another_harness_is_refused_with_file_and_key() -> None:
    with pytest.raises(ConfigError) as refused:
        _compile(
            project=(
                '[execution]\nharness = "claude-code"\nmodel = "claude-opus-4-1"\n'
                "model_pinned = true\n"
            ),
            run={"harness": "omp"},
        )

    assert refused.value.file == "project.toml"
    assert refused.value.key == "execution.model"
    assert "host-specific" in str(refused.value)


def test_protected_paths_only_add_across_layers() -> None:
    config = _compile(
        project='[protection]\npaths = ["infra/**", "docs/**"]\n',
        user='[protection]\npaths = ["docs/**", "secrets/**"]\n',
        run={"protected_paths": ["deploy/**"]},
    ).config

    assert config.plain_value("protected_paths") == [
        "deploy/**",
        "infra/**",
        "docs/**",
        "secrets/**",
    ]
    assert config.field("protected_paths").winner == MERGED


def test_a_project_hard_constraint_refuses_a_run_layer_override_before_any_request() -> None:
    project = '[constraints]\nallowed_efforts = ["low", "medium"]\nmax_repair_limit = 2\n'
    model = '[execution]\nharness = "omp"\neffort = "low"\n'

    with pytest.raises(InvalidInputError, match="violates the project layer constraint"):
        _compile(project=model + project, run={"effort": "high", "effort_pinned": True})
    with pytest.raises(ConfigError) as refused:
        _compile(project=model + project, run={"repair_limit": 9})

    assert refused.value.file == "project.toml"
    assert refused.value.key == "constraints.max_repair_limit"
    assert "run layer" in str(refused.value)
    assert (
        _compile(project=model + project, run={"repair_limit": 2}).config.value("repair_limit") == 2
    )


def test_a_lower_layer_cannot_loosen_a_hard_constraint() -> None:
    project = '[constraints]\nallowed_runner_modes = ["isolated-oci"]\n'
    user = '[constraints]\nallowed_runner_modes = ["trusted-local", "isolated-oci"]\n'

    with pytest.raises(ConfigError) as refused:
        _compile(project=project, user=user)

    assert refused.value.key == "constraints.allowed_runner_modes"
    assert refused.value.file == "project.toml"


def test_permitted_capabilities_must_stay_inside_the_hard_constraint() -> None:
    with pytest.raises(ConfigError, match="permitted capabilities must be among git"):
        _compile(
            project='[constraints]\nallowed_capabilities = ["git"]\n',
            run={"permitted_capabilities": ["git", "browser"]},
        )


def test_no_layer_may_leave_the_trust_mode_to_a_default() -> None:
    with pytest.raises(ConfigError) as refused:
        compile_effective_config(project=None, user=None, run=run_layer({}))

    assert refused.value.key == "runner.mode"
    assert set(refused.value.allowed) == {"trusted-local", "isolated-oci"}


def test_the_project_may_declare_the_trust_mode_and_the_run_may_not_be_shadowed_by_it() -> None:
    config = compile_effective_config(
        project=_doc(SelectionLayer.PROJECT, _RUNNER), user=None, run=None
    ).config

    assert config.value("runner_mode") == "trusted-local"
    assert config.field("runner_mode").winner == "project"
    assert config.field("runner_image").winner == UNSET


def test_isolated_runs_need_validation_commands_from_a_layer_above_the_factory() -> None:
    image = "sha256:" + "a" * 64
    with pytest.raises(InvalidInputError, match="requires --validation-command"):
        _compile(run={"runner_mode": "isolated-oci", "runner_image": image})

    config = _compile(
        run={
            "runner_mode": "isolated-oci",
            "runner_image": image,
            "validation_commands": ["python3 -m unittest"],
        }
    ).config
    assert config.value("runner_mode") == "isolated-oci"


def test_isolation_only_settings_are_refused_under_trusted_local() -> None:
    with pytest.raises(ConfigError, match="applies only to the isolated-oci"):
        _compile(run={"model_access": "relay"})


def test_trusted_local_run_shadows_lower_isolated_configuration() -> None:
    isolated = (
        '[runner]\nmode = "isolated-oci"\nimage = "sha256:' + "a" * 64 + '"\n'
        'docker_context = "private"\nmodel_access = "relay"\n'
    )
    compiled = _compile(
        project=isolated.replace('docker_context = "private"\n', ""),
        user=isolated,
    )
    assert not compiled.runner.isolated_mode
    for name in ("runner_image", "docker_context", "model_access"):
        field = compiled.config.field(name)
        assert field.winner == UNSET
        assert all(entry.note is not None for entry in field.chain)


def test_another_harness_executable_is_ineligible() -> None:
    config = _compile(
        user='[execution]\nharness = "claude-code"\n[executables]\nharness = "/private/claude"\n',
        run={"harness": "omp"},
    ).config
    assert config.value("harness_executable") == "omp"
    assert any(
        entry.layer == "user" and entry.note for entry in config.field("harness_executable").chain
    )


def test_project_executables_are_refused_with_source_and_key() -> None:
    with pytest.raises(ConfigError) as refused:
        _doc(SelectionLayer.PROJECT, '[executables]\nharness = "/tmp/hostile"\n')
    assert refused.value.file == "project.toml"
    assert refused.value.key == "executables"


def test_the_snapshot_round_trips_and_its_digest_names_every_source() -> None:
    first = _compile(project="[validation]\nrepair_limit = 4\n").config
    again = _compile(project="[validation]\nrepair_limit = 4\n").config
    changed = _compile(project="[validation]\nrepair_limit = 3\n").config

    assert EffectiveConfig.from_state(first.to_state()) == first
    assert first.digest == again.digest
    assert first.digest != changed.digest
    assert {record.layer for record in first.sources} == {"run", "project", "workflow", "factory"}


def test_a_malformed_snapshot_is_refused() -> None:
    state = _compile().config.to_state()
    del state["fields"]["repair_limit"]  # type: ignore[attr-defined]

    with pytest.raises(InvalidInputError, match="exactly the compiled fields"):
        EffectiveConfig.from_state(state)
