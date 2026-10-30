"""``runner build-relay-image``, ``runner relay`` and ``--model-access`` on ``build-request``."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from enginery.adapters.oci import image as oci_image
from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.relay import RelayStatus
from enginery.cli.main import main
from enginery.domain.errors import MissingPrerequisiteError
from enginery.domain.runner import ImageIdentity, RunnerProfile
from enginery.workflows.run_intent import stage1_request_from_state
from tests.adapters.oci.support import IMAGE_ID, FakeDocker
from tests.cli.test_runner_cli import (
    HOLD_EXIT,
    INVALID_EXIT,
    _build_argv,
    _engine_factory,
    _run_exists,
    _runner_image_fake,
    _start_argv,
)

RELAY_IMAGE = "sha256:" + "c" * 64
HEALTHY = RelayStatus(True, True, True, RELAY_IMAGE, ("egress", "internal"), "http://u:1", "ok")


def _isolated(tmp_path: Path, *extra: str) -> tuple[list[str], Path]:
    return _build_argv(
        tmp_path,
        "--runner",
        "isolated-oci",
        "--runner-image",
        IMAGE_ID,
        "--validation-command",
        "python3 -m compileall -q .",
        *extra,
    )


# --- build-request --model-access ---------------------------------------------------------


def test_a_request_without_model_access_keeps_the_persisted_selection_shape(
    tmp_path: Path,
) -> None:
    argv, output = _isolated(tmp_path)

    assert main(argv) == 0

    raw = json.loads(output.read_text(encoding="utf-8"))
    assert "model_access" not in raw["runner"]
    assert stage1_request_from_state(raw).runner.model_access == "none"


def test_a_request_can_bind_model_access_through_the_relay(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "relay").mkdir()
    plain_argv, plain_output = _isolated(tmp_path / "plain")
    relay_argv, relay_output = _isolated(tmp_path / "relay", "--model-access", "relay")

    assert main(plain_argv) == 0
    assert main(relay_argv) == 0

    raw = json.loads(relay_output.read_text(encoding="utf-8"))
    assert raw["runner"]["model_access"] == "relay"
    request = stage1_request_from_state(raw)
    assert request.runner.model_access == "relay"
    assert request.runner.isolated_mode
    plain = stage1_request_from_state(json.loads(plain_output.read_text(encoding="utf-8")))
    assert request.runner != plain.runner


def test_model_access_through_the_relay_needs_an_isolated_runner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, output = _build_argv(tmp_path, "--runner", "trusted-local", "--model-access", "relay")

    assert main(argv) == INVALID_EXIT

    assert "runner.model_access" in capsys.readouterr().err
    assert not output.exists()


def test_an_unknown_model_access_is_rejected_by_the_parser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, _ = _isolated(tmp_path, "--model-access", "direct")

    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == INVALID_EXIT
    assert "--model-access" in capsys.readouterr().err


# --- starting a run bound to the relay ------------------------------------------------------


def _relay_admission_factory(
    fake: FakeDocker, status: RelayStatus | Exception
) -> Callable[..., OciRunnerAdmission]:
    def relay_status(_engine: OciEngine, _profile: RunnerProfile) -> RelayStatus:
        if isinstance(status, Exception):
            raise status
        return status

    def verified(_engine: OciEngine, image_id: str) -> ImageIdentity:
        return ImageIdentity(image_id=image_id, recipe_digest=oci_image.relay_recipe_digest())

    def make(*, profile: RunnerProfile) -> OciRunnerAdmission:
        return OciRunnerAdmission(
            engine_factory=lambda context: fake.engine(context=context),
            profile=profile,
            relay_status=relay_status,
            verify_relay=verified,
        )

    return make


def _relay_request(tmp_path: Path) -> Path:
    argv, output = _isolated(tmp_path, "--model-access", "relay")
    assert main(argv) == 0
    return output


def test_a_run_bound_to_the_relay_is_held_when_the_relay_is_not_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _relay_request(tmp_path)
    unhealthy = RelayStatus(False, False, False, None, (), None, "the relay does not exist")
    monkeypatch.setattr(
        "enginery.cli.runner_wiring.OciRunnerAdmission",
        _relay_admission_factory(_runner_image_fake(), unhealthy),
    )
    capsys.readouterr()

    exit_code = main(_start_argv(tmp_path, request))

    assert exit_code == HOLD_EXIT
    error = capsys.readouterr().err
    assert "model access relay" in error and "not run in trusted-local mode" in error
    assert not _run_exists(tmp_path)


def test_a_run_bound_to_the_relay_is_held_when_the_engine_fails_the_relay_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _relay_request(tmp_path)
    monkeypatch.setattr(
        "enginery.cli.runner_wiring.OciRunnerAdmission",
        _relay_admission_factory(_runner_image_fake(), MissingPrerequisiteError("engine gone")),
    )

    assert main(_start_argv(tmp_path, request)) == HOLD_EXIT
    assert not _run_exists(tmp_path)


def test_a_run_bound_to_the_relay_starts_when_the_relay_is_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _relay_request(tmp_path)
    monkeypatch.setattr(
        "enginery.cli.runner_wiring.OciRunnerAdmission",
        _relay_admission_factory(_runner_image_fake(), HEALTHY),
    )

    assert main(_start_argv(tmp_path, request)) == 0
    assert _run_exists(tmp_path)


def test_a_run_without_model_access_never_consults_the_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv, output = _isolated(tmp_path)
    assert main(argv) == 0
    monkeypatch.setattr(
        "enginery.cli.runner_wiring.OciRunnerAdmission",
        _relay_admission_factory(_runner_image_fake(), RuntimeError("must not be consulted")),
    )

    assert main(_start_argv(tmp_path, output)) == 0


# --- runner build-relay-image --------------------------------------------------------------


def test_build_relay_image_builds_on_a_qualified_engine_and_prints_the_exact_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[dict[str, object]] = []

    def fake_build(engine: OciEngine, *, tag: str | None, no_cache: bool) -> ImageIdentity:
        built.append({"tag": tag, "no_cache": no_cache})
        return ImageIdentity(image_id=RELAY_IMAGE, recipe_digest=oci_image.relay_recipe_digest())

    monkeypatch.setattr("enginery.cli.runner_relay.OciEngine", _engine_factory(FakeDocker()))
    monkeypatch.setattr("enginery.cli.runner_relay.build_relay_image", fake_build)

    exit_code = main(
        [
            "runner",
            "build-relay-image",
            "--docker-context",
            "colima",
            "--tag",
            "relay:dev",
            "--no-cache",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["image"]["image_id"] == RELAY_IMAGE
    assert report["image"]["recipe_digest"] == oci_image.relay_recipe_digest()
    assert report["engine"]["context"] == "colima"
    assert report["build_seconds"] >= 0
    assert built == [{"tag": "relay:dev", "no_cache": True}]


def test_build_relay_image_refuses_an_engine_that_cannot_enforce_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDocker()
    fake.info["CgroupVersion"] = "1"
    monkeypatch.setattr("enginery.cli.runner_relay.OciEngine", _engine_factory(fake))
    monkeypatch.setattr(
        "enginery.cli.runner_relay.build_relay_image",
        lambda *a, **k: pytest.fail("built on an unqualified engine"),
    )

    exit_code = main(["runner", "build-relay-image"])

    assert exit_code != 0


# --- runner relay up / down / status -------------------------------------------------------


class RecordingManager:
    instances: ClassVar[list[RecordingManager]] = []

    def __init__(self, engine: OciEngine, **_: Any) -> None:
        self.engine = engine
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status_to_report = HEALTHY
        RecordingManager.instances.append(self)

    def up(self, **kwargs: Any) -> RelayStatus:
        self.calls.append(("up", kwargs))
        return self.status_to_report

    def down(self) -> RelayStatus:
        self.calls.append(("down", {}))
        return RelayStatus(False, False, False, None, (), None, "gone")

    def status(self) -> RelayStatus:
        self.calls.append(("status", {}))
        return self.status_to_report


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> type[RecordingManager]:
    RecordingManager.instances = []
    monkeypatch.setattr("enginery.cli.runner_relay.OciEngine", _engine_factory(FakeDocker()))
    monkeypatch.setattr("enginery.cli.runner_relay.RelayManager", RecordingManager)
    return RecordingManager


def test_relay_up_passes_the_pinned_image_upstream_and_token_file(
    manager: type[RecordingManager], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = tmp_path / "token"

    exit_code = main(
        [
            "runner",
            "relay",
            "up",
            "--relay-image",
            RELAY_IMAGE,
            "--upstream",
            "http://host.docker.internal:47400",
            "--token-file",
            str(token),
            "--docker-context",
            "colima",
            "--recreate",
            "--wait-seconds",
            "5",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["healthy"] is True
    ((verb, kwargs),) = manager.instances[0].calls
    assert verb == "up"
    assert kwargs == {
        "relay_image": RELAY_IMAGE,
        "upstream": "http://host.docker.internal:47400",
        "token_file": token,
        "recreate": True,
        "wait_seconds": 5.0,
    }
    assert manager.instances[0].engine.context == "colima"
    assert str(token) not in json.dumps(report)


def test_relay_up_requires_its_inputs(
    manager: type[RecordingManager], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["runner", "relay", "up", "--relay-image", RELAY_IMAGE])

    assert raised.value.code == INVALID_EXIT
    assert "--upstream" in capsys.readouterr().err


def test_relay_status_exits_with_the_hold_code_when_unhealthy(
    manager: type[RecordingManager], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["runner", "relay", "status"]) == 0
    capsys.readouterr()

    unhealthy = RelayStatus(True, True, False, RELAY_IMAGE, (), None, "no health answer")
    original = RecordingManager.status

    def status(self: RecordingManager) -> RelayStatus:
        original(self)
        return unhealthy

    RecordingManager.status = status  # type: ignore[method-assign]
    try:
        exit_code = main(["runner", "relay", "status"])
    finally:
        RecordingManager.status = original  # type: ignore[method-assign]

    assert exit_code == HOLD_EXIT
    assert json.loads(capsys.readouterr().out)["detail"] == "no health answer"


def test_relay_down_reports_the_removed_relay_and_exits_zero(
    manager: type[RecordingManager], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["runner", "relay", "down"])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["present"] is False
    assert manager.instances[0].calls == [("down", {})]


def test_relay_requires_a_subcommand(
    manager: type[RecordingManager], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["runner", "relay"])

    assert exit_code != 0
    assert "up, down or status" in capsys.readouterr().err
