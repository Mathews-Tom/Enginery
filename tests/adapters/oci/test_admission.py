"""Isolated-runner admission against a fake engine: every refusal is a named hold."""

from __future__ import annotations

from dataclasses import replace

import pytest

from enginery.adapters.oci import image
from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launch import engine_memory_floor_mib
from enginery.domain.execution_selection import HoldReason
from enginery.domain.runner import ImageIdentity, RunnerProfile, RunnerSelection

from .support import DOCKER_DESKTOP_INFO, IMAGE_ID, FakeDocker


def _labels(recipe: str | None = None) -> dict[str, str]:
    return {
        image.RECIPE_DIGEST_LABEL: image.recipe_digest() if recipe is None else recipe,
        image.RUNNER_IMAGE_LABEL: "1",
        image.CLAUDE_CODE_LABEL: "2.1.296",
        image.OMP_LABEL: "18.10.3",
        image.BUN_LABEL: "1.3.14",
    }


def _fake_with_runner_image(recipe: str | None = None) -> FakeDocker:
    fake = FakeDocker()
    fake.images[IMAGE_ID]["Config"]["Labels"] = _labels(recipe)
    return fake


def _admission(fake: FakeDocker, *, profile: RunnerProfile | None = None) -> OciRunnerAdmission:
    def factory(context: str | None) -> OciEngine:
        return fake.engine(context=context)

    return OciRunnerAdmission(engine_factory=factory, profile=profile or RunnerProfile())


def _selection(profile: RunnerProfile | None = None, **kwargs: str) -> RunnerSelection:
    return RunnerSelection.isolated(image=IMAGE_ID, profile=profile or RunnerProfile(), **kwargs)


def test_admits_a_qualified_engine_and_a_verified_image() -> None:
    fake = _fake_with_runner_image()

    decision = _admission(fake).admit(_selection())

    assert decision.hold is None
    assert decision.engine is not None
    assert decision.engine.cgroup_version == "2"
    assert decision.engine.server_version == DOCKER_DESKTOP_INFO["ServerVersion"]
    assert isinstance(decision.image, ImageIdentity)
    assert decision.image.image_id == IMAGE_ID
    assert decision.image.recipe_digest == image.recipe_digest()


def test_the_selected_docker_context_reaches_the_engine() -> None:
    fake = _fake_with_runner_image()

    decision = _admission(fake).admit(_selection(docker_context="desktop-linux"))

    assert decision.hold is None
    assert fake.context == "desktop-linux"
    assert decision.engine is not None
    assert decision.engine.context == "desktop-linux"


def test_trusted_local_selection_is_never_held_and_never_touches_the_engine() -> None:
    fake = FakeDocker()
    fake.unreachable = True

    decision = _admission(fake).admit(RunnerSelection.trusted_local())

    assert decision.hold is None
    assert decision.engine is None
    assert decision.image is None
    assert fake.calls == []


def test_a_cgroup_v1_engine_is_unqualified_and_names_the_missing_requirement() -> None:
    fake = _fake_with_runner_image()
    fake.info["CgroupVersion"] = "1"

    decision = _admission(fake).admit(_selection())

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "cgroup v2" in decision.hold.message
    assert "not run in trusted-local mode" in decision.hold.message
    assert decision.engine is None
    # The image is never inspected on an engine that cannot enforce the profile.
    assert not any(call[:2] == ("image", "inspect") for call in fake.calls)


def test_an_engine_with_too_little_memory_is_a_named_hold_before_the_image_is_inspected() -> None:
    profile = RunnerProfile()
    floor = engine_memory_floor_mib(profile)
    fake = _fake_with_runner_image()
    fake.info["MemTotal"] = (floor - 1) * (1 << 20)

    decision = _admission(fake).admit(_selection())

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert f"one worker needs at least {floor} MiB" in decision.hold.message
    assert "virtual machine" in decision.hold.message
    assert "not run in trusted-local mode" in decision.hold.message
    assert decision.engine is None
    assert not any(call[:2] == ("image", "inspect") for call in fake.calls)


def test_an_engine_with_exactly_the_floor_memory_is_admitted() -> None:
    fake = _fake_with_runner_image()
    fake.info["MemTotal"] = engine_memory_floor_mib(RunnerProfile()) * (1 << 20)

    assert _admission(fake).admit(_selection()).hold is None


def test_an_engine_that_reports_no_memory_is_a_named_hold() -> None:
    fake = _fake_with_runner_image()
    del fake.info["MemTotal"]

    decision = _admission(fake).admit(_selection())

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "memory cannot be checked" in decision.hold.message


def test_an_unreachable_daemon_is_unqualified() -> None:
    fake = _fake_with_runner_image()
    fake.unreachable = True

    decision = _admission(fake).admit(_selection())

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "unreachable" in decision.hold.message
    assert decision.image is None


def test_a_missing_image_is_unqualified() -> None:
    fake = _fake_with_runner_image()
    other = "sha256:" + "c" * 64

    decision = _admission(fake).admit(
        RunnerSelection.isolated(image=other, profile=RunnerProfile())
    )

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert other in decision.hold.message
    assert decision.engine is None


def test_an_image_that_is_not_a_runner_image_is_unqualified() -> None:
    fake = FakeDocker()  # the default fake image carries no runner labels

    decision = _admission(fake).admit(_selection())

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "not an Enginery runner image" in decision.hold.message


def test_an_image_built_from_another_recipe_is_fingerprint_drift() -> None:
    stale = "sha256:" + "0" * 64
    fake = _fake_with_runner_image(stale)

    decision = _admission(fake).admit(_selection())

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT
    assert decision.hold.details["recorded"] == stale
    assert decision.hold.details["expected"] == image.recipe_digest()
    assert decision.hold.details["image_id"] == IMAGE_ID
    assert decision.image is None


def test_a_profile_the_run_was_not_bound_to_is_drift_without_touching_the_engine() -> None:
    fake = _fake_with_runner_image()
    bound = RunnerProfile(network="a-different-runner-network")
    selection = _selection(bound)
    current = RunnerProfile()
    assert selection.profile_digest != current.digest

    decision = _admission(fake, profile=current).admit(selection)

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT
    assert decision.hold.details["bound"] == str(bound.digest)
    assert decision.hold.details["current"] == str(current.digest)
    assert fake.calls == []


@pytest.mark.parametrize("field_name", ["memory_mib", "pids"])
def test_a_loosened_resource_limit_changes_the_profile_digest_and_is_drift(
    field_name: str,
) -> None:
    fake = _fake_with_runner_image()
    default = RunnerProfile()
    changed = replace(
        default,
        resources=replace(
            default.resources, **{field_name: getattr(default.resources, field_name) * 2}
        ),
    )

    decision = _admission(fake, profile=changed).admit(_selection(default))

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT
    assert fake.calls == []
