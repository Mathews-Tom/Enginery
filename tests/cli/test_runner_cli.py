"""``enginery runner`` and the isolated flags of ``engine build-request`` / ``engine start``.

The container engine is the only seam replaced: the CLI's own engine/admission constructors are
swapped for ones backed by the in-memory fake docker, so everything else (argument parsing, the
admission logic, hold reporting, exit codes, the request builder, the ledger) is the real code.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from enginery.adapters.oci import image as oci_image
from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.cli.main import main
from enginery.domain.execution_catalog import InstalledHost
from enginery.domain.runner import ImageIdentity, ResourceLimits, RunnerProfile
from enginery.ledger.service import LedgerService
from enginery.workflows.run_lifecycle import stage1_request_from_state
from tests.adapters.oci.support import IMAGE_ID, FakeDocker, image_identity
from tests.support.github import StandInGitHub, write_stand_in_gh

HOLD_EXIT = 5
INVALID_EXIT = 2


def _runner_image_fake() -> FakeDocker:
    fake = FakeDocker()
    fake.images[IMAGE_ID]["Config"]["Labels"] = {
        oci_image.RECIPE_DIGEST_LABEL: oci_image.recipe_digest(),
        oci_image.RUNNER_IMAGE_LABEL: "1",
        oci_image.CLAUDE_CODE_LABEL: "2.1.296",
        oci_image.OMP_LABEL: "18.10.3",
        oci_image.BUN_LABEL: "1.3.14",
    }
    return fake


def _admission_factory(fake: FakeDocker) -> Callable[..., OciRunnerAdmission]:
    def make(*, profile: RunnerProfile) -> OciRunnerAdmission:
        return OciRunnerAdmission(
            engine_factory=lambda context: fake.engine(context=context), profile=profile
        )

    return make


def _engine_factory(fake: FakeDocker) -> Callable[..., OciEngine]:
    def make(*, context: str | None = None) -> OciEngine:
        return fake.engine(context=context)

    return make


# --- runner status ---------------------------------------------------------------------


def test_status_reports_an_admitted_engine_and_image_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _runner_image_fake()
    monkeypatch.setattr("enginery.cli.runner.OciRunnerAdmission", _admission_factory(fake))

    exit_code = main(["runner", "status", "--image", IMAGE_ID])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["admitted"] is True
    assert report["hold"] is None
    assert report["profile_digest"] == str(RunnerProfile().digest)
    assert report["image"]["image_id"] == IMAGE_ID
    assert report["image"]["recipe_digest"] == oci_image.recipe_digest()
    assert report["engine"]["cgroup_version"] == "2"


def test_status_passes_the_docker_context_through_to_the_engine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _runner_image_fake()
    monkeypatch.setattr("enginery.cli.runner.OciRunnerAdmission", _admission_factory(fake))

    exit_code = main(["runner", "status", "--image", IMAGE_ID, "--docker-context", "colima"])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert fake.context == "colima"
    assert report["engine"]["context"] == "colima"


def test_status_reports_the_hold_of_an_unqualified_engine_and_exits_with_the_hold_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _runner_image_fake()
    fake.info["CgroupVersion"] = "1"
    monkeypatch.setattr("enginery.cli.runner.OciRunnerAdmission", _admission_factory(fake))

    exit_code = main(["runner", "status", "--image", IMAGE_ID])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == HOLD_EXIT
    assert report["admitted"] is False
    assert report["hold"]["reason"] == "runner_unqualified"
    assert "cgroup v2" in report["hold"]["message"]
    assert report["engine"] is None and report["image"] is None


def test_status_reports_a_stale_image_as_fingerprint_drift(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _runner_image_fake()
    stale = "sha256:" + "0" * 64
    fake.images[IMAGE_ID]["Config"]["Labels"][oci_image.RECIPE_DIGEST_LABEL] = stale
    monkeypatch.setattr("enginery.cli.runner.OciRunnerAdmission", _admission_factory(fake))

    exit_code = main(["runner", "status", "--image", IMAGE_ID])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == HOLD_EXIT
    assert report["hold"]["reason"] == "runner_fingerprint_drift"
    assert report["hold"]["details"]["recorded"] == stale


def test_status_reports_a_missing_image_as_unqualified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeDocker()
    other = "sha256:" + "c" * 64
    monkeypatch.setattr("enginery.cli.runner.OciRunnerAdmission", _admission_factory(fake))

    exit_code = main(["runner", "status", "--image", other])

    report = json.loads(capsys.readouterr().out)
    assert exit_code == HOLD_EXIT
    assert report["hold"]["reason"] == "runner_unqualified"
    assert other in report["hold"]["message"]


@pytest.mark.parametrize(
    "bad_image",
    ["latest", "sha256:abc", "sha256:" + "A" * 64, "sha256:" + "a" * 63, "sha512:" + "a" * 64],
)
def test_status_refuses_an_image_that_is_not_an_exact_sha256_id(
    bad_image: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _runner_image_fake()
    monkeypatch.setattr("enginery.cli.runner.OciRunnerAdmission", _admission_factory(fake))

    exit_code = main(["runner", "status", "--image", bad_image])

    captured = capsys.readouterr()
    assert exit_code == INVALID_EXIT
    assert "sha256" in captured.err
    assert captured.out == ""
    assert fake.calls == [], "an invalid image id never reaches the engine"


def test_status_requires_an_image(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["runner", "status"])

    assert raised.value.code == INVALID_EXIT
    assert "--image" in capsys.readouterr().err


# --- runner build-image ----------------------------------------------------------------


def test_build_image_builds_on_a_qualified_engine_and_prints_the_exact_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeDocker()
    built: list[dict[str, object]] = []

    def fake_build(engine: OciEngine, *, tag: str | None, no_cache: bool) -> ImageIdentity:
        built.append({"engine": engine, "tag": tag, "no_cache": no_cache})
        return image_identity()

    monkeypatch.setattr("enginery.cli.runner.OciEngine", _engine_factory(fake))
    monkeypatch.setattr("enginery.cli.runner.build_image", fake_build)

    exit_code = main(
        [
            "runner",
            "build-image",
            "--docker-context",
            "colima",
            "--tag",
            "enginery:dev",
            "--no-cache",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["image"]["image_id"] == IMAGE_ID
    assert report["engine"]["cgroup_version"] == "2"
    assert report["engine"]["context"] == "colima"
    assert len(built) == 1
    assert built[0]["tag"] == "enginery:dev" and built[0]["no_cache"] is True
    assert fake.context == "colima"


def test_build_image_does_not_build_on_an_engine_that_cannot_enforce_the_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeDocker()
    fake.info["CgroupVersion"] = "1"
    built: list[object] = []

    def fake_build(engine: OciEngine, *, tag: str | None, no_cache: bool) -> ImageIdentity:
        built.append(engine)
        return image_identity()

    monkeypatch.setattr("enginery.cli.runner.OciEngine", _engine_factory(fake))
    monkeypatch.setattr("enginery.cli.runner.build_image", fake_build)

    exit_code = main(["runner", "build-image"])

    captured = capsys.readouterr()
    assert exit_code == 3  # missing prerequisite
    assert "cgroup v2" in captured.err
    assert captured.out == ""
    assert built == []
    assert not any(call[0] == "build" for call in fake.calls)


def test_runner_without_a_subcommand_asks_for_one(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["runner"])

    assert exit_code == HOLD_EXIT
    error = capsys.readouterr().err
    assert "runner requires a subcommand" in error
    assert "custody-receipt" in error


# --- engine build-request --------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", "-q", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    _git("add", "README", cwd=repository)
    _git("commit", "-q", "-m", "fixture", cwd=repository)
    from tests.support.behavioral import declare_behavioral_policy

    return repository, declare_behavioral_policy(repository)


def _build_argv(
    tmp_path: Path, *runner: str, github: StandInGitHub | None = None
) -> tuple[list[str], Path]:
    repository, base = _repository(tmp_path)
    github = github or write_stand_in_gh(tmp_path / "github")
    output = tmp_path / "request.json"
    return (
        [
            "engine",
            "build-request",
            *runner,
            "--output",
            str(output),
            "--run-id",
            "run-1",
            "--repository",
            "owner/repo",
            "--external-reference",
            "owner/repo#1",
            "--github-executable",
            str(github.executable),
            "--base-revision",
            base,
            "--repository-path",
            str(repository),
            "--workspace-path",
            str(tmp_path / "workspace"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--validation-command",
            "python3 -m unittest",
        ],
        output,
    )


def test_an_isolated_request_must_name_the_image_it_pins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, output = _build_argv(tmp_path, "--runner", "isolated-oci")

    exit_code = main(argv)

    assert exit_code == INVALID_EXIT
    assert "--runner-image" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize(
    "extra",
    [["--runner-image", IMAGE_ID], ["--docker-context", "colima"]],
    ids=["image", "context"],
)
def test_a_trusted_local_request_rejects_isolated_only_flags(
    tmp_path: Path, extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    argv, output = _build_argv(tmp_path, "--runner", "trusted-local", *extra)

    exit_code = main(argv)

    assert exit_code == INVALID_EXIT
    assert "applies only to the isolated-oci trust mode" in capsys.readouterr().err
    assert not output.exists()


def test_a_request_must_bind_a_trust_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, output = _build_argv(tmp_path)

    assert main(argv) == INVALID_EXIT

    error = capsys.readouterr().err
    assert "runner.mode" in error and "--runner" in error
    assert not output.exists()


def test_an_isolated_request_rejects_a_malformed_image_id_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, output = _build_argv(
        tmp_path, "--runner", "isolated-oci", "--runner-image", "enginery-runner:latest"
    )

    exit_code = main(argv)

    assert exit_code == INVALID_EXIT
    assert "sha256" in capsys.readouterr().err
    assert not output.exists()


def test_an_isolated_request_pins_the_image_and_the_profile_digest(tmp_path: Path) -> None:
    argv, output = _build_argv(
        tmp_path,
        "--runner",
        "isolated-oci",
        "--runner-image",
        IMAGE_ID,
        "--docker-context",
        "colima",
    )

    exit_code = main(argv)

    assert exit_code == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["runner"]["trust_mode"] == "isolated-oci"
    assert raw["runner"]["image"] == IMAGE_ID
    assert raw["runner"]["profile_digest"] == str(RunnerProfile().digest)
    assert raw["runner"]["docker_context"] == "colima"
    request = stage1_request_from_state(raw)
    assert request.runner.isolated_mode
    assert request.runner.image == IMAGE_ID
    assert request.runner.profile_digest == RunnerProfile().digest
    assert str(request.digest) == raw["request_digest"]


def test_a_trusted_local_request_records_an_explicit_trust_mode_without_an_image(
    tmp_path: Path,
) -> None:
    argv, output = _build_argv(tmp_path, "--runner", "trusted-local")

    exit_code = main(argv)

    assert exit_code == 0
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["runner"]["trust_mode"] == "trusted-local"
    assert raw["runner"].get("image") is None
    assert stage1_request_from_state(raw).runner.isolated_mode is False


# --- engine start of an isolated request -----------------------------------------------


def _start_argv(tmp_path: Path, request: Path) -> list[str]:
    return [
        "engine",
        "start",
        "--database",
        str(tmp_path / "ledger.db"),
        "--owner",
        "operator",
        "--request",
        str(request),
    ]


def _isolated_request(tmp_path: Path) -> Path:
    argv, output = _build_argv(tmp_path, "--runner", "isolated-oci", "--runner-image", IMAGE_ID)
    assert main(argv) == 0
    return output


def _run_exists(tmp_path: Path) -> bool:
    with LedgerService.open(tmp_path / "ledger.db") as ledger:
        return ledger.read_projection(aggregate_type="run", aggregate_id="run-1") is not None


def test_starting_an_isolated_request_on_an_unqualified_engine_is_held_and_records_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _isolated_request(tmp_path)
    fake = _runner_image_fake()
    fake.unreachable = True
    monkeypatch.setattr("enginery.cli.stage1_runner.OciRunnerAdmission", _admission_factory(fake))
    capsys.readouterr()

    exit_code = main(_start_argv(tmp_path, request))

    assert exit_code == HOLD_EXIT
    assert "unreachable" in capsys.readouterr().err
    assert not _run_exists(tmp_path)


def test_starting_an_isolated_request_on_a_qualified_engine_records_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _isolated_request(tmp_path)
    fake = _runner_image_fake()
    monkeypatch.setattr("enginery.cli.stage1_runner.OciRunnerAdmission", _admission_factory(fake))

    exit_code = main(_start_argv(tmp_path, request))

    assert exit_code == 0
    assert _run_exists(tmp_path)
    assert any(call[:2] == ("image", "inspect") for call in fake.calls)


# --- engine next of an isolated run -----------------------------------------------------


def _watch_argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "engine",
        "next",
        "--database",
        str(tmp_path / "ledger.db"),
        "--owner",
        "operator",
        "--run-id",
        "run-1",
        *extra,
    ]


def _started_isolated_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _isolated_request(tmp_path)
    fake = _runner_image_fake()
    monkeypatch.setattr("enginery.cli.stage1_runner.OciRunnerAdmission", _admission_factory(fake))
    assert main(_start_argv(tmp_path, request)) == 0


def _drift_the_current_profile(monkeypatch: pytest.MonkeyPatch) -> RunnerProfile:
    stricter = RunnerProfile(resources=ResourceLimits(memory_mib=2048))
    assert stricter.digest != RunnerProfile().digest
    monkeypatch.setattr("enginery.cli.stage1_runner.RunnerProfile", lambda: stricter)
    return stricter


@pytest.mark.parametrize("extra", [(), ("--advance",)])
def test_watch_reports_the_hold_of_a_run_bound_to_a_profile_that_is_no_longer_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: tuple[str, ...],
) -> None:
    _started_isolated_run(tmp_path, monkeypatch)
    bound = RunnerProfile().digest
    stricter = _drift_the_current_profile(monkeypatch)
    capsys.readouterr()

    exit_code = main(_watch_argv(tmp_path, *extra))

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["next_action"] == "hold"
    assert report["hold"]["reason"] == "runner_fingerprint_drift"
    assert report["hold"]["details"] == {"bound": str(bound), "current": str(stricter.digest)}
    assert report["execution"]["runner"]["trust_mode"] == "isolated-oci"
    assert report["nodes"] == []  # the hold launched nothing, even when asked to advance


def test_watch_of_a_run_whose_profile_still_matches_reports_no_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _started_isolated_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "enginery.cli.stage1_composition.probe_installed_omp",
        lambda executable, runner=None: InstalledHost("18.10.3", "json-protocol=3"),
    )
    capsys.readouterr()

    assert main(_watch_argv(tmp_path)) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["hold"] is None
    assert report["next_action"] == "qualify"
