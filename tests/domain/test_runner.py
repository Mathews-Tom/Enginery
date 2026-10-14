from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.runner import (
    LEGACY_RUNNER_SELECTION,
    OUT_MOUNT,
    WORKER_HOME,
    WORKER_TMP,
    WORKSPACE_MOUNT,
    EffectiveContainer,
    EngineIdentity,
    ImageIdentity,
    LaunchSpec,
    PermissionMapping,
    ResourceLimits,
    RunnerContext,
    RunnerProfile,
    RunnerReceipt,
    RunnerSelection,
    TrustMode,
    isolated_permission_mapping,
    profile_violations,
    trusted_local_permission_mapping,
)

IMAGE = "sha256:" + "a" * 64
PROFILE = RunnerProfile()


def _effective(profile: RunnerProfile = PROFILE) -> EffectiveContainer:
    limits = profile.resources
    return EffectiveContainer(
        user=f"{profile.uid}:{profile.gid}",
        cap_drop=("ALL",),
        cap_add=(),
        security_options=("no-new-privileges",),
        privileged=False,
        read_only_rootfs=True,
        memory_bytes=limits.memory_bytes,
        memory_swap_bytes=limits.memory_bytes,
        nano_cpus=limits.nano_cpus,
        pids_limit=limits.pids,
        network_mode=profile.network,
        networks=(profile.network,),
        mounts=(
            ("volume", WORKSPACE_MOUNT, True),
            ("volume", OUT_MOUNT, True),
            ("tmpfs", WORKER_TMP, True),
            ("tmpfs", WORKER_HOME, True),
        ),
        devices=(),
        env_keys=("HOME", "LANG", "PATH"),
    )


def test_a_container_that_matches_the_profile_has_no_violations() -> None:
    assert profile_violations(PROFILE, _effective()) == ()


_BASE = _effective()
_MOUNTS = _BASE.mounts


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"user": "0:0"}, "user"),
        ({"user": "root"}, "user"),
        ({"privileged": True}, "privileged"),
        ({"cap_drop": ()}, "capabilities"),
        ({"cap_drop": ("NET_RAW",)}, "capabilities"),
        ({"cap_add": ("SYS_ADMIN",)}, "adds capabilities"),
        ({"security_options": ()}, "no-new-privileges"),
        ({"read_only_rootfs": False}, "writable"),
        ({"memory_bytes": _BASE.memory_bytes * 2}, "memory limit"),
        ({"memory_swap_bytes": _BASE.memory_bytes * 2}, "swap"),
        ({"memory_swap_bytes": -1}, "swap"),
        ({"nano_cpus": _BASE.nano_cpus * 2}, "cpu"),
        ({"nano_cpus": 0}, "cpu"),
        ({"pids_limit": -1}, "pids"),
        ({"pids_limit": _BASE.pids_limit + 1}, "pids"),
        ({"network_mode": "bridge", "networks": ("bridge",)}, "network"),
        ({"network_mode": "host", "networks": ("host",)}, "network"),
        ({"networks": (PROFILE.network, "bridge")}, "network"),
        ({"networks": ()}, "network"),
        ({"devices": ("/dev/kvm",)}, "devices"),
        (
            {"mounts": (*_MOUNTS, ("bind", "/var/run/docker.sock", True))},
            "unadmitted mount bind at /var/run/docker.sock",
        ),
        (
            {"mounts": (*_MOUNTS, ("bind", "/Users/someone", True))},
            "unadmitted mount bind at /Users/someone",
        ),
        (
            {"mounts": (*_MOUNTS, ("bind", "/home/worker/.ssh", False))},
            "unadmitted mount",
        ),
        (
            {"mounts": (*_MOUNTS, ("tmpfs", "/var/tmp", True))},
            "unadmitted mount tmpfs at /var/tmp",
        ),
        ({"mounts": _MOUNTS[1:]}, "admitted volumes"),
        ({"mounts": (_MOUNTS[0], *_MOUNTS[2:])}, "admitted volumes"),
        (
            {"mounts": (("volume", WORKSPACE_MOUNT, False), *_MOUNTS[1:])},
            "unadmitted mount",
        ),
        ({"env_keys": ("HOME", "ANTHROPIC_API_KEY")}, "ANTHROPIC_API_KEY"),
        ({"env_keys": ("HOME", "GH_TOKEN")}, "GH_TOKEN"),
        ({"env_keys": ("HOME", "SSH_AUTH_SOCK")}, "SSH_AUTH_SOCK"),
    ],
)
def test_every_weakened_property_is_reported_by_name(change: dict[str, Any], expected: str) -> None:
    violations = profile_violations(PROFILE, replace(_effective(), **change))

    assert violations
    assert any(expected in violation for violation in violations), violations


def test_image_injected_environment_keys_are_not_violations() -> None:
    effective = replace(_effective(), env_keys=("HOME", "HOSTNAME", "NODE_VERSION"))

    assert profile_violations(PROFILE, effective) == ()


def test_a_profile_with_non_default_limits_is_checked_against_its_own_limits() -> None:
    profile = RunnerProfile(resources=ResourceLimits(memory_mib=1024, cpus=1.0, pids=64))

    assert profile_violations(profile, _effective(profile)) == ()
    assert profile_violations(profile, _effective(PROFILE))


@pytest.mark.parametrize(
    "change",
    [
        {"uid": 0},
        {"gid": 0},
        {"uid": 999},
        {"cap_drop": ()},
        {"cap_drop": ("NET_RAW",)},
        {"security_options": ()},
        {"security_options": ("seccomp=unconfined",)},
        {"read_only_rootfs": False},
    ],
)
def test_a_profile_refuses_to_be_weaker_than_the_isolation_floor(change: dict[str, Any]) -> None:
    with pytest.raises(InvalidInputError):
        RunnerProfile(**change)


def test_a_profile_digest_is_order_stable_and_sensitive_to_every_property() -> None:
    reordered = replace(PROFILE, env_allowlist=tuple(reversed(PROFILE.env_allowlist)))
    assert reordered.digest == PROFILE.digest

    variants = [
        replace(PROFILE, uid=65533),
        replace(PROFILE, gid=65533),
        replace(PROFILE, network="other-net"),
        replace(PROFILE, name="other-profile"),
        replace(PROFILE, security_options=("no-new-privileges", "seccomp=default")),
        replace(PROFILE, env_allowlist=(*PROFILE.env_allowlist, "EXTRA_KEY")),
        replace(PROFILE, env_allowlist=PROFILE.env_allowlist[1:]),
        replace(PROFILE, resources=replace(PROFILE.resources, memory_mib=8192)),
        replace(PROFILE, resources=replace(PROFILE.resources, cpus=4.0)),
        replace(PROFILE, resources=replace(PROFILE.resources, pids=1024)),
        replace(PROFILE, resources=replace(PROFILE.resources, tmp_mib=1024)),
        replace(PROFILE, resources=replace(PROFILE.resources, home_mib=1024)),
        replace(PROFILE, resources=replace(PROFILE.resources, stop_grace_seconds=30)),
    ]
    digests = {variant.digest for variant in variants}
    assert PROFILE.digest not in digests
    assert len(digests) == len(variants)


def test_a_profile_round_trips_through_its_state() -> None:
    profile = replace(PROFILE, network="other-net", env_allowlist=("HOME", "PATH"))

    decoded = RunnerProfile.from_state(json.loads(json.dumps(profile.to_state())))

    assert decoded.network == "other-net"
    assert set(decoded.env_allowlist) == {"HOME", "PATH"}
    assert decoded.digest == profile.digest


def test_trusted_local_selection_carries_no_isolation_pins() -> None:
    selection = RunnerSelection.trusted_local()

    assert selection.trust_mode is TrustMode.TRUSTED_LOCAL
    assert (selection.image, selection.profile_digest, selection.docker_context) == (
        None,
        None,
        None,
    )
    assert not selection.isolated_mode
    assert not selection.legacy
    assert RunnerSelection.from_state(selection.to_state()) == selection


def test_the_legacy_selection_is_flagged_legacy() -> None:
    assert LEGACY_RUNNER_SELECTION.legacy
    assert LEGACY_RUNNER_SELECTION.trust_mode is TrustMode.TRUSTED_LOCAL
    assert not RunnerSelection.trusted_local().legacy


def test_an_isolated_selection_pins_the_image_and_profile_digest() -> None:
    selection = RunnerSelection.isolated(image=IMAGE, profile=PROFILE, docker_context="colima")

    assert selection.isolated_mode
    assert selection.image == IMAGE
    assert selection.profile_digest == PROFILE.digest
    assert selection.profile_name == PROFILE.name
    assert selection.docker_context == "colima"
    decoded = RunnerSelection.from_state(json.loads(json.dumps(selection.to_state())))
    assert decoded == selection


@pytest.mark.parametrize(
    "image",
    [
        "latest",
        "enginery-worker:latest",
        "sha256:" + "a" * 12,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "A" * 64,
        "a" * 64,
        "sha512:" + "a" * 64,
        "",
    ],
)
def test_an_isolated_selection_rejects_anything_but_an_exact_image_id(image: str) -> None:
    with pytest.raises(InvalidInputError):
        RunnerSelection.isolated(image=image, profile=PROFILE)


def test_selection_state_with_an_unknown_mode_or_schema_is_refused() -> None:
    state = RunnerSelection.isolated(image=IMAGE, profile=PROFILE).to_state()

    with pytest.raises(InvalidInputError):
        RunnerSelection.from_state({**state, "trust_mode": "container-ish"})
    with pytest.raises(InvalidInputError):
        RunnerSelection.from_state({**state, "schema_version": 2})
    with pytest.raises(InvalidInputError):
        RunnerSelection.from_state({k: v for k, v in state.items() if k != "schema_version"})
    with pytest.raises(InvalidInputError):
        RunnerSelection.from_state({**state, "image": "latest"})
    with pytest.raises(InvalidInputError):
        RunnerSelection.from_state("not a mapping")


def test_selection_invariants_between_mode_and_pins() -> None:
    with pytest.raises(InvalidInputError):
        RunnerSelection(
            trust_mode=TrustMode.ISOLATED_OCI,
            profile_name=PROFILE.name,
            profile_digest=PROFILE.digest,
            image=IMAGE,
            legacy=True,
        )
    with pytest.raises(InvalidInputError):
        RunnerSelection(trust_mode=TrustMode.ISOLATED_OCI, profile_name=PROFILE.name, image=IMAGE)
    for extra in (
        {"image": IMAGE},
        {"profile_digest": PROFILE.digest},
        {"docker_context": "colima"},
    ):
        with pytest.raises(InvalidInputError):
            RunnerSelection(
                trust_mode=TrustMode.TRUSTED_LOCAL,
                profile_name="enginery-trusted-local-v1",
                **extra,
            )
    with pytest.raises(InvalidInputError):
        RunnerSelection.isolated(image=IMAGE, profile=PROFILE, docker_context="  ")


def _permissions() -> PermissionMapping:
    return isolated_permission_mapping("claude-code")


def _spec(**overrides: Any) -> LaunchSpec:
    fields: dict[str, Any] = {
        "operation_id": "op-1",
        "container_name": "enginery-op-1",
        "image": IMAGE,
        "profile": PROFILE,
        "harness": "claude-code",
        "workspace_path": "/tmp/ws",
        "out_path": "/tmp/out",
        "receipt_path": "/tmp/receipts/op-1.json",
        "result_name": "result.json",
        "time_budget_seconds": 600,
        "permissions": _permissions(),
        "env": (("HOME", "/home/worker"), ("LANG", "C.UTF-8")),
        "seed_files": (("a/b.txt", "line1\nline2 \u00e9\n"), ("c.md", "")),
        "bundle_branch": "work",
        "bundle_base": "abc123",
        "docker_context": "colima",
    }
    fields.update(overrides)
    return LaunchSpec(**fields)


def test_a_launch_spec_round_trips_through_json() -> None:
    spec = _spec()

    decoded = LaunchSpec.from_state(json.loads(json.dumps(spec.to_state())))

    assert decoded == spec
    assert decoded.seed_files == spec.seed_files
    assert decoded.env == spec.env


def test_a_launch_spec_without_optionals_round_trips() -> None:
    spec = _spec(env=(), seed_files=(), bundle_branch=None, bundle_base=None, docker_context=None)

    assert LaunchSpec.from_state(json.loads(json.dumps(spec.to_state()))) == spec


@pytest.mark.parametrize(
    "overrides",
    [
        {"env": (("ANTHROPIC_API_KEY", "x"),)},
        {"env": (("HOME", "/h"), ("GH_TOKEN", "x"))},
        {"env": (("SSH_AUTH_SOCK", "/s"),)},
        {"workspace_path": "ws"},
        {"out_path": "out"},
        {"receipt_path": "receipts/op.json"},
        {"result_name": "a/b.json"},
        {"result_name": ".hidden"},
        {"result_name": ".."},
        {"bundle_base": None},
        {"bundle_branch": None},
        {"image": "enginery-worker:latest"},
        {"image": "sha256:abc"},
        {"time_budget_seconds": 0},
        {"time_budget_seconds": -5},
        {"operation_id": " "},
    ],
)
def test_a_launch_spec_refuses_unsafe_or_malformed_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(InvalidInputError):
        _spec(**overrides)


def test_a_launch_spec_allows_env_keys_a_wider_profile_allowlists() -> None:
    profile = replace(PROFILE, env_allowlist=(*PROFILE.env_allowlist, "EXTRA_KEY"))

    spec = _spec(profile=profile, env=(("EXTRA_KEY", "1"),))

    assert LaunchSpec.from_state(spec.to_state()) == spec


def test_a_launch_spec_state_with_malformed_pairs_is_refused() -> None:
    state = _spec().to_state()

    with pytest.raises(InvalidInputError):
        LaunchSpec.from_state({**state, "env": [["HOME"]]})
    with pytest.raises(InvalidInputError):
        LaunchSpec.from_state({**state, "seed_files": "nope"})


def test_a_runner_context_requires_a_matching_isolated_selection_and_absolute_root() -> None:
    selection = RunnerSelection.isolated(image=IMAGE, profile=PROFILE)

    context = RunnerContext(selection=selection, profile=PROFILE, receipt_root="/var/receipts")
    assert context.receipt_root == "/var/receipts"

    with pytest.raises(InvalidInputError):
        RunnerContext(
            selection=RunnerSelection.trusted_local(), profile=PROFILE, receipt_root="/var/r"
        )
    with pytest.raises(InvalidInputError):
        RunnerContext(
            selection=selection, profile=replace(PROFILE, network="other"), receipt_root="/var/r"
        )
    with pytest.raises(InvalidInputError):
        RunnerContext(selection=selection, profile=PROFILE, receipt_root="receipts")


def _receipt(effective: EffectiveContainer | None = None) -> RunnerReceipt:
    return RunnerReceipt(
        operation_id="op-1",
        trust_mode=TrustMode.ISOLATED_OCI,
        profile_name=PROFILE.name,
        profile_digest=PROFILE.digest,
        engine=EngineIdentity(
            kind="docker",
            server_version="29.8.2",
            operating_system="linux",
            kernel_version="6.1",
            cgroup_version="2",
            security_options=("name=seccomp,profile=builtin",),
            rootless=False,
            context="colima",
        ),
        image=ImageIdentity(
            image_id=IMAGE, recipe_digest="sha256:" + "b" * 64, harness_versions=(("omp", "1.0"),)
        ),
        container_name="enginery-op-1",
        host={"os": "darwin", "arch": "arm64"},
        effective=effective or _effective(),
        permissions=_permissions(),
        harness_version="2.1",
        harness_protocol="json-3",
        started_at="2026-10-13T00:00:00Z",
    )


def test_a_runner_receipt_round_trips_and_its_digest_tracks_the_effective_container() -> None:
    receipt = _receipt()

    decoded = RunnerReceipt.from_state(json.loads(json.dumps(receipt.to_state())))

    assert decoded == receipt
    assert decoded.digest == receipt.digest
    assert _receipt().digest == receipt.digest
    weaker = _receipt(replace(_effective(), privileged=True))
    assert weaker.digest != receipt.digest
    assert _receipt(replace(_effective(), env_keys=("HOME",))).digest != receipt.digest


def test_a_runner_receipt_records_environment_names_only() -> None:
    effective = replace(_effective(), env_keys=("HOME", "PATH"))
    state = _receipt(effective).to_state()
    recorded = state["effective"]
    assert isinstance(recorded, dict)

    assert recorded["env_keys"] == ["HOME", "PATH"]
    assert not {"env", "environment", "env_values"} & set(recorded)
    assert not {"env", "environment", "env_values"} & set(state)
    assert all(isinstance(key, str) and "=" not in key for key in recorded["env_keys"])


def test_runner_receipt_state_that_is_unsupported_or_incomplete_is_refused() -> None:
    state = _receipt().to_state()

    with pytest.raises(InvalidInputError):
        RunnerReceipt.from_state({**state, "schema_version": 2})
    with pytest.raises(InvalidInputError):
        RunnerReceipt.from_state({**state, "trust_mode": "mystery"})
    for missing in ("operation_id", "profile_digest", "engine", "image", "effective", "started_at"):
        with pytest.raises(InvalidInputError):
            RunnerReceipt.from_state({k: v for k, v in state.items() if k != missing})
    with pytest.raises(InvalidInputError):
        RunnerReceipt.from_state([])


def test_permission_mapping_round_trips() -> None:
    for mapping in (
        isolated_permission_mapping("omp"),
        isolated_permission_mapping("claude-code"),
        trusted_local_permission_mapping("claude-code"),
        trusted_local_permission_mapping("omp"),
    ):
        assert PermissionMapping.from_state(json.loads(json.dumps(mapping.to_state()))) == mapping


def test_trusted_local_mappings_declare_their_bypass() -> None:
    claude = trusted_local_permission_mapping("claude-code")
    assert claude.unconditional_bypass
    assert claude.arguments[:2] == ("--permission-mode", "bypassPermissions") or (
        "bypassPermissions" in claude.arguments and "--permission-mode" in claude.arguments
    )

    omp = trusted_local_permission_mapping("omp")
    assert omp.unconditional_bypass
    assert omp.arguments == ()


@pytest.mark.parametrize("harness", ["claude-code", "omp"])
def test_isolated_mappings_are_never_a_bypass(harness: str) -> None:
    mapping = isolated_permission_mapping(harness)

    assert mapping.harness == harness
    assert not mapping.unconditional_bypass
    assert mapping.arguments
    forbidden = ("bypasspermissions", "dangerously-skip-permissions", "yolo", "auto-approve")
    for argument in mapping.arguments:
        assert not any(token in argument.lower() for token in forbidden), argument


def test_unknown_harness_has_no_permission_mapping() -> None:
    with pytest.raises(InvalidInputError):
        isolated_permission_mapping("mystery")
    with pytest.raises(InvalidInputError):
        trusted_local_permission_mapping("mystery")


def test_a_permission_mapping_state_with_a_non_boolean_bypass_is_refused() -> None:
    state = isolated_permission_mapping("omp").to_state()

    with pytest.raises(InvalidInputError):
        PermissionMapping.from_state({**state, "unconditional_bypass": "no"})


def test_digest_type_is_used_for_profile_digest() -> None:
    assert isinstance(PROFILE.digest, Digest)


def test_a_launch_spec_records_the_probed_harness_identity_only_when_it_has_one() -> None:
    bare = _spec().to_state()
    assert "harness_version" not in bare and "harness_protocol" not in bare  # legacy shape

    spec = _spec(harness_version="2.1.296", harness_protocol="stream-json=1")
    state = spec.to_state()

    assert state["harness_version"] == "2.1.296"
    assert state["harness_protocol"] == "stream-json=1"
    assert LaunchSpec.from_state(json.loads(json.dumps(state))) == spec
    assert LaunchSpec.from_state(bare).harness_version is None


@pytest.mark.parametrize("missing", ["operating_system", "kernel_version", "cgroup_version"])
@pytest.mark.parametrize("bad", ["absent", "", "   ", 3, None])
def test_an_engine_identity_with_a_missing_or_blank_value_is_refused_not_defaulted(
    missing: str, bad: object
) -> None:
    state = _receipt().to_state()["engine"]
    assert isinstance(state, dict)
    state = {k: v for k, v in state.items() if k != missing}
    if bad != "absent":
        state[missing] = bad

    with pytest.raises(InvalidInputError, match=missing):
        EngineIdentity.from_state(state)


def test_a_complete_engine_identity_still_decodes() -> None:
    state = _receipt().to_state()["engine"]

    decoded = EngineIdentity.from_state(state)

    assert decoded.operating_system == "linux"
    assert decoded.to_state() == state


@pytest.mark.parametrize("versions", [{"omp": 1}, {1: "omp"}, {"omp": None}, [("omp", "1")]])
def test_an_image_identity_with_non_text_harness_versions_is_refused(versions: object) -> None:
    state = {"image_id": IMAGE, "recipe_digest": "sha256:" + "b" * 64, "harness_versions": versions}

    with pytest.raises(InvalidInputError):
        ImageIdentity.from_state(state)


def test_an_isolated_runner_receipt_cannot_record_a_permission_bypass() -> None:
    with pytest.raises(InvalidInputError, match="bypass"):
        replace(_receipt(), permissions=trusted_local_permission_mapping("claude-code"))
    state = _receipt().to_state()
    state["permissions"] = trusted_local_permission_mapping("claude-code").to_state()
    with pytest.raises(InvalidInputError, match="bypass"):
        RunnerReceipt.from_state(state)
    # trusted-local keeps declaring its bypass in its own mapping: only isolated is refused
    assert trusted_local_permission_mapping("omp").unconditional_bypass is True


def test_the_worker_environment_allowlist_names_the_git_hook_override_keys() -> None:
    from enginery.domain.runner import DEFAULT_ENV_ALLOWLIST

    assert {"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"} <= set(
        DEFAULT_ENV_ALLOWLIST
    )


@pytest.mark.parametrize("harness", ["claude-code", "omp"])
def test_no_isolated_permission_entry_runs_code_from_the_workspace(harness: str) -> None:
    arguments = isolated_permission_mapping(harness).arguments
    runners = ("pytest", "python", "uv ", "mypy", "ruff")

    assert not [
        argument for argument in arguments if any(name in argument.lower() for name in runners)
    ]
    rationale = isolated_permission_mapping(harness).rationale
    assert "not the boundary" in rationale and "Containment is the container" in rationale


@pytest.mark.parametrize("blank", ["operating_system", "kernel_version", "cgroup_version"])
@pytest.mark.parametrize("value", ["", "   "])
def test_an_engine_identity_cannot_be_constructed_with_a_value_it_could_not_decode(
    blank: str, value: str
) -> None:
    fields: dict[str, object] = {
        "kind": "docker",
        "server_version": "29.8.2",
        "operating_system": "linux",
        "kernel_version": "6.1",
        "cgroup_version": "2",
        "security_options": (),
        "rootless": False,
    }
    fields[blank] = value

    with pytest.raises(InvalidInputError):
        EngineIdentity(**fields)  # type: ignore[arg-type]


def test_resource_bounds_omit_the_poll_interval_for_a_kernel_enforced_cap() -> None:
    from enginery.domain.runner import (
        DISK_BOUND_KERNEL_VOLUME,
        ResourceBounds,
    )

    fields: dict[str, Any] = {
        "workspace_bytes": 1,
        "out_bytes": 1,
        "workspace_inodes": 1,
        "out_inodes": 1,
        "max_file_bytes": 1,
        "log_driver": "json-file",
        "log_max_size": "1m",
        "log_max_file": 2,
    }
    kernel = ResourceBounds(disk_mechanism=DISK_BOUND_KERNEL_VOLUME, **fields)
    monitored = ResourceBounds(disk_mechanism="host-monitor-kill-hold", poll_seconds=2.0, **fields)

    assert "poll_seconds" not in kernel.to_state()
    assert monitored.to_state()["poll_seconds"] == 2.0
    # A receipt recorded by the earlier monitor still decodes, with the same recorded state.
    assert ResourceBounds.from_state(monitored.to_state()) == monitored
    assert ResourceBounds.from_state(kernel.to_state()) == kernel
