from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from enginery.application.behavioral_checks import (
    BehavioralAssertion,
    BehavioralCheck,
    BehavioralChecksBinding,
    checks_from_state,
)
from enginery.application.effective_config import EffectiveConfig, compile_effective_config
from enginery.application.layered_config import ConfigError, parse_layer_document, run_layer
from enginery.cli import config_binding
from enginery.cli.config_binding import (
    behavioral_checks_path,
    bind_behavioral_checks,
    checks_at_base,
    read_behavioral_checks,
    require_bound_configuration,
)
from enginery.cli.factory_setup import load_project_configuration
from enginery.cli.main import main
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError
from enginery.domain.execution_selection import SelectionLayer
from enginery.domain.ids import RunId
from enginery.workflows.stage1 import Stage1RunRequest, stage1_request_from_state
from tests.cli.test_stage1_request_builder import _git, _minimal_argv, _repository


def _check(*, expected: str = "persisted") -> dict[str, object]:
    return {
        "name": "persistence",
        "observer": "command",
        "argv": ["example-cli", "save", "value"],
        "assertions": [
            {"kind": "exit_status", "equals": 0},
            {"kind": "stdout", "contains": expected},
            {"kind": "stderr", "equals": ""},
            {"kind": "produced_file", "path": "result.txt", "contains": "value"},
        ],
        "fault_classes": ["constant_result", "skipped_persistence", "wrong_build"],
    }


def _policy(repository: Path, *, expected: str = "persisted") -> Path:
    path = repository / ".enginery" / "project.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        'schema_version = 1\n[runner]\nmode = "trusted-local"\n'
        '[[acceptance.checks]]\nname = "persistence"\nobserver = "command"\n'
        'argv = ["example-cli", "save", "value"]\n'
        'fault_classes = ["constant_result", "skipped_persistence", "wrong_build"]\n'
        '[[acceptance.checks.assertions]]\nkind = "stdout"\n'
        f'contains = "{expected}"\n',
        encoding="utf-8",
    )
    return path


def _build(
    tmp_path: Path, repository: Path, base: str
) -> tuple[Stage1RunRequest, dict[str, object]]:
    output = tmp_path / "request.json"
    assert (
        main(
            _minimal_argv(
                tmp_path=tmp_path, repository=repository, base_revision=base, output=output
            )
        )
        == 0
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    return stage1_request_from_state(document), document


def test_doctor_uses_committed_checks_not_working_tree(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    _policy(repository, expected="base")
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "base checks", cwd=repository)
    _policy(repository, expected="uncommitted")
    configuration = load_project_configuration(repository)
    assert configuration.compiled is not None
    assert configuration.compiled.config.behavioral_checks == checks_at_base(
        repository, _git("rev-parse", "HEAD", cwd=repository)
    )


def test_refused_stage1_start_leaves_no_custody(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, base = _repository(tmp_path)
    request, _ = _build(tmp_path, repository, base)
    assert request.behavioral_checks == ()
    path = behavioral_checks_path(request)
    assert path is not None and not path.parent.exists()
    capsys.readouterr()
    assert (
        main(
            [
                "engine",
                "start",
                "--database",
                str(tmp_path / "ledger.db"),
                "--owner",
                "operator",
                "--request",
                str(tmp_path / "request.json"),
            ]
        )
        == 5
    )
    output = capsys.readouterr()
    message = output.out + output.err
    assert str(repository / ".enginery/project.toml") in message
    assert request.base_branch in message
    assert "enginery engine build-request" in message
    assert not path.parent.exists()


def test_factory_corrupt_custody_repair_names_factory_command(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    _policy(repository)
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "base checks", cwd=repository)
    request, _ = _build(tmp_path, repository, _git("rev-parse", "HEAD", cwd=repository))
    request = replace(request, run=replace(request.run, id=RunId("fr-custody")))
    bind_behavioral_checks(request)
    path = behavioral_checks_path(request)
    assert path is not None
    path.write_text("{")
    with pytest.raises(InvalidInputError, match="not valid JSON") as caught:
        bind_behavioral_checks(request)
    assert f"rm -- {shlex.quote(str(path))}" in str(caught.value)
    assert (
        f"enginery run {shlex.quote(request.work_snapshot.work_item.external_reference)} "
        f"--project {shlex.quote(str(repository))}"
    ) in str(caught.value)
    assert "engine build-request" not in str(caught.value)
    path.unlink()
    assert bind_behavioral_checks(request) == read_behavioral_checks(request)


def test_corrupt_custody_names_file_and_repair(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    _policy(repository)
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "base checks", cwd=repository)
    request, _ = _build(tmp_path, repository, _git("rev-parse", "HEAD", cwd=repository))
    bind_behavioral_checks(request)
    path = behavioral_checks_path(request)
    assert path is not None
    path.write_text("{")
    with pytest.raises(InvalidInputError, match="not valid JSON") as caught:
        bind_behavioral_checks(request)
    assert str(path) in str(caught.value)
    assert "rm --" in str(caught.value)
    assert "enginery engine build-request" in str(caught.value)


def test_custody_publication_only_exposes_complete_private_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = _repository(tmp_path)
    _policy(repository)
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "base checks", cwd=repository)
    request, _ = _build(tmp_path, repository, _git("rev-parse", "HEAD", cwd=repository))
    path = behavioral_checks_path(request)
    assert path is not None and not path.exists()
    original = config_binding.os.link

    def publish(source: Path, destination: Path) -> None:
        assert destination == path and not destination.exists()
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert json.loads(source.read_bytes())["checks"]
        original(source, destination)

    monkeypatch.setattr(config_binding.os, "link", publish)
    binding = bind_behavioral_checks(request)
    assert read_behavioral_checks(request) == binding
    assert not list(path.parent.glob(".checks-*"))


def test_killed_custody_publisher_names_repair_and_can_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = _repository(tmp_path)
    _policy(repository)
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "base checks", cwd=repository)
    request, _ = _build(tmp_path, repository, _git("rev-parse", "HEAD", cwd=repository))
    path = behavioral_checks_path(request)
    assert path is not None
    original = config_binding.os.link
    pid = os.fork()
    if pid == 0:

        def link_then_die(source: Path, destination: Path) -> None:
            original(source, destination)
            os.kill(os.getpid(), signal.SIGKILL)

        monkeypatch.setattr(config_binding.os, "link", link_then_die)
        bind_behavioral_checks(request)
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    assert path.stat().st_nlink == 2
    assert list(path.parent.glob(".checks-*"))
    for operation in (bind_behavioral_checks, read_behavioral_checks):
        with pytest.raises(InvalidInputError, match="requires a private regular file") as caught:
            operation(request)
        assert str(path) in str(caught.value)
        assert f"rm -- {shlex.quote(str(path))}" in str(caught.value)
        assert "enginery engine build-request" in str(caught.value)
    path.unlink()
    assert bind_behavioral_checks(request) == read_behavioral_checks(request)
    assert path.stat().st_nlink == 1


@pytest.mark.parametrize("operation", [bind_behavioral_checks, read_behavioral_checks])
def test_custody_readers_wait_for_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: object
) -> None:
    repository, _ = _repository(tmp_path)
    _policy(repository)
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "base checks", cwd=repository)
    request, _ = _build(tmp_path, repository, _git("rev-parse", "HEAD", cwd=repository))
    path = behavioral_checks_path(request)
    assert path is not None
    linked = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    original_link = config_binding.os.link
    original_flock = config_binding.fcntl.flock

    def publish(source: Path, destination: Path) -> None:
        original_link(source, destination)
        linked.set()
        assert release.wait(timeout=10)

    def flock(descriptor: int, mode: int) -> None:
        if mode == config_binding.fcntl.LOCK_EX and linked.is_set():
            waiting.set()
        original_flock(descriptor, mode)

    monkeypatch.setattr(config_binding.os, "link", publish)
    monkeypatch.setattr(config_binding.fcntl, "flock", flock)
    assert callable(operation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publisher = pool.submit(bind_behavioral_checks, request)
        try:
            assert linked.wait(timeout=10)
            assert path.stat().st_nlink == 2
            reader = pool.submit(operation, request)
            assert waiting.wait(timeout=10)
            assert not reader.done()
        finally:
            release.set()
        assert reader.result(timeout=10) == publisher.result(timeout=10)
    assert path.stat().st_nlink == 1


def test_typed_observer_inputs_and_assertions_round_trip() -> None:
    command = _check()
    service = {
        "name": "service",
        "observer": "service",
        "argv": ["example-service", "--port", "8123"],
        "url": "http://127.0.0.1:8123/value",
        "assertions": [{"kind": "body", "equals": "value"}],
        "fault_classes": ["constant_result"],
    }
    browser = {
        "name": "browser",
        "observer": "browser",
        "url": "http://[::1]:8123/",
        "assertions": [{"kind": "dom", "selector": "#result", "contains": "value"}],
        "fault_classes": ["wrong_build"],
    }
    checks = checks_from_state([command, service, browser])

    assert [check.to_state() for check in checks] == [command, service, browser]
    assert checks[0].assertions[0].expected == 0
    assert checks[1].argv == ("example-service", "--port", "8123")
    assert checks[2].assertions[0].selector == "#result"


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": True},
        {"observer": "mock"},
        {"argv": "example-cli save value"},
        {"argv": ["sh", "-c", "echo value"]},
        {"url": "http://127.0.0.1:8123/"},
        {"fault_classes": ["other"]},
        {"fault_classes": ["constant_result", "constant_result"]},
        {"assertions": [{"kind": "stdout", "contains": "value", "optional": True}]},
        {"assertions": [{"kind": "stdout", "contains": ""}]},
        {"assertions": [{"kind": "exit_status", "equals": True}]},
        {"assertions": [{"kind": "stdout", "equals": "value", "contains": "value"}]},
        {"assertions": [{"kind": "produced_file", "path": "../secret", "exists": True}]},
        {"assertions": [{"kind": "dom", "selector": "#result", "exists": True}]},
    ],
)
def test_invalid_check_documents_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(InvalidInputError):
        BehavioralCheck.from_state({**_check(), **changes})


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/",
        "file:///tmp/result",
        "http://127.0.0.1.example.invalid/",
        "http://user:password@127.0.0.1/",
        "http://127.0.0.1:0/",
        "http://[::1",
    ],
)
def test_service_observer_cannot_choose_a_non_loopback_or_credentialed_url(url: str) -> None:
    with pytest.raises(InvalidInputError):
        BehavioralCheck.from_state(
            {
                **_check(),
                "observer": "service",
                "url": url,
                "assertions": [{"kind": "body", "equals": "value"}],
            }
        )


def test_unknown_acceptance_keys_and_duplicate_check_names_are_refused() -> None:
    with pytest.raises(ConfigError, match="acceptance"):
        parse_layer_document(
            SelectionLayer.PROJECT,
            b"schema_version = 1\n[acceptance]\nchecks = []\nignore = true\n",
            source="project.toml",
        )
    with pytest.raises(InvalidInputError, match="unique"):
        checks_from_state([_check(), _check()])
    with pytest.raises(InvalidInputError):
        BehavioralAssertion.from_state({"kind": "dom", "selector": "#result", "equals": 1})


@pytest.mark.parametrize("name", ["browser", "browser_digest", "lavish", "gh_axi"])
def test_repository_policy_cannot_select_observer_or_presenter_executables(name: str) -> None:
    with pytest.raises(ConfigError, match="must not choose host programs"):
        parse_layer_document(
            SelectionLayer.PROJECT,
            f'schema_version = 1\n[executables]\n{name} = "untrusted"\n'.encode(),
            source="project.toml",
        )


def test_operator_executables_are_strict_and_reach_the_compiled_snapshot() -> None:
    digest = str(Digest.of_bytes(b"approved browser tree"))
    user = parse_layer_document(
        SelectionLayer.USER,
        (
            'schema_version = 1\n[executables]\nbrowser = "/opt/browser"\n'
            f'browser_digest = "{digest}"\nlavish = "/opt/lavish-axi"\n'
            'gh_axi = "/opt/gh-axi"\n'
        ).encode(),
        source="user.toml",
    )
    compiled = compile_effective_config(
        project=None, user=user, run=run_layer({"runner_mode": "trusted-local"})
    ).config
    restored = EffectiveConfig.from_state(compiled.to_state())

    assert restored.operator_executables == user.operator_executables
    assert restored.operator_executables is not None
    assert restored.operator_executables.browser_digest == digest
    assert restored.operator_executables.lavish == "/opt/lavish-axi"
    assert restored.operator_executables.gh_axi == "/opt/gh-axi"
    with pytest.raises(ConfigError, match="executables"):
        parse_layer_document(
            SelectionLayer.USER,
            b'schema_version = 1\n[executables]\nbrowser = "/opt/browser"\n',
            source="user.toml",
        )
    with pytest.raises(ConfigError, match="unknown key"):
        parse_layer_document(
            SelectionLayer.USER,
            b'schema_version = 1\n[executables]\ngh_axi_shell = "sh -c x"\n',
            source="user.toml",
        )


def test_request_checks_come_from_base_and_custody_survives_worker_head_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repository, _ = _repository(tmp_path)
    _policy(repository, expected="base-owned")
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "operator acceptance", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)
    _policy(repository, expected="worker-owned")

    request, document = _build(tmp_path, repository, base)
    config = require_bound_configuration(document, request)
    binding = bind_behavioral_checks(request)
    path = behavioral_checks_path(request)

    assert config.behavioral_checks == request.behavioral_checks
    assert request.behavioral_checks is not None
    assert request.behavioral_checks[0].assertions[0].expected == "base-owned"
    assert binding is not None and path is not None
    assert binding.assertion_set_digest == Digest.of_json(
        [check.to_state() for check in request.behavioral_checks]
    )
    assert not path.is_relative_to(repository)
    assert not path.is_relative_to(request.workspace_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    _policy(repository, expected="changed-after-binding")
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "worker policy change", cwd=repository)

    assert read_behavioral_checks(request) == binding
    assert bind_behavioral_checks(request) == binding


def test_custody_rejects_tampering_even_with_a_recomputed_assertion_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repository, base = _repository(tmp_path)
    request, _ = _build(tmp_path, repository, base)
    binding = bind_behavioral_checks(request)
    path = behavioral_checks_path(request)
    assert binding is not None and path is not None
    altered = replace(binding, checks=checks_from_state([_check()]))
    path.write_text(json.dumps(altered.to_state()), encoding="utf-8")

    with pytest.raises(InvalidInputError, match="does not match"):
        read_behavioral_checks(request)
    with pytest.raises(InvalidInputError, match="does not match"):
        bind_behavioral_checks(request)


def test_a_hand_edited_request_cannot_bind_worker_selected_assertions(tmp_path: Path) -> None:
    repository, base = _repository(tmp_path)
    request, _ = _build(tmp_path, repository, base)
    forged = replace(request, behavioral_checks=checks_from_state([_check()]))

    with pytest.raises(InvalidInputError, match="base revision"):
        bind_behavioral_checks(forged)


def test_custody_refuses_symbolic_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repository, base = _repository(tmp_path)
    request, _ = _build(tmp_path, repository, base)
    bind_behavioral_checks(request)
    path = behavioral_checks_path(request)
    assert path is not None
    raw = path.read_bytes()
    path.unlink()
    target = tmp_path / "untrusted.json"
    target.write_bytes(raw)
    path.symlink_to(target)

    with pytest.raises(InvalidInputError, match="symbolic link"):
        read_behavioral_checks(request)


def test_base_policy_must_be_a_regular_git_blob(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    directory = repository / ".enginery"
    directory.mkdir()
    (directory / "project.toml").symlink_to("policy.toml")
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "linked policy", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)

    with pytest.raises(InvalidInputError, match="tracked regular file"):
        checks_at_base(repository, base)


def test_historical_configuration_and_request_keep_their_original_digest(tmp_path: Path) -> None:
    repository, base = _repository(tmp_path)
    request, _ = _build(tmp_path, repository, base)
    new = compile_effective_config(
        project=None, user=None, run=run_layer({"runner_mode": "trusted-local"})
    ).config
    old_state = new.to_state()
    del old_state["acceptance"]
    old_config = EffectiveConfig.from_state(old_state)
    historical = replace(
        request,
        run=replace(request.run, configuration_snapshot_digest=old_config.digest),
        behavioral_checks=None,
    )
    state = historical.initial_state()
    decoded = stage1_request_from_state(state)

    assert old_config.to_state() == old_state
    assert old_config.digest == Digest.of_json(old_state)
    assert old_config.behavioral_checks is None
    assert "behavioral_checks" not in state
    assert decoded.behavioral_checks is None
    assert decoded.digest == historical.digest
    assert bind_behavioral_checks(decoded) is None
    assert read_behavioral_checks(decoded) is None


def test_custody_document_rejects_unknown_fields_and_digest_changes() -> None:
    binding = BehavioralChecksBinding(
        base_revision="a" * 40,
        configuration_snapshot_digest=str(Digest.of_bytes(b"config")),
        checks=checks_from_state([_check()]),
    )
    assert BehavioralChecksBinding.from_state(binding.to_state()) == binding
    with pytest.raises(InvalidInputError):
        BehavioralChecksBinding.from_state({**binding.to_state(), "worker_override": True})
    assert replace(binding, evaluator_version="next-evaluator").digest != binding.digest
    assert replace(binding, base_revision="b" * 40).digest != binding.digest


def test_rehashing_a_forged_snapshot_cannot_replace_the_base_assertions(tmp_path: Path) -> None:
    repository, base = _repository(tmp_path)
    request, document = _build(tmp_path, repository, base)
    config = EffectiveConfig.from_state(document["effective_config"])
    forged_config = replace(config, behavioral_checks=checks_from_state([_check()]))
    forged = replace(
        request,
        run=replace(request.run, configuration_snapshot_digest=forged_config.digest),
        behavioral_checks=forged_config.behavioral_checks,
    )
    forged_document = {**document, "effective_config": forged_config.to_state()}

    with pytest.raises(InvalidInputError, match="base revision"):
        require_bound_configuration(forged_document, forged)


def test_base_policy_reads_are_bounded(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    path = _policy(repository)
    path.write_bytes(path.read_bytes() + b"#" * (1024 * 1024))
    _git("add", ".enginery/project.toml", cwd=repository)
    _git("commit", "-m", "oversized policy", cwd=repository)
    base = _git("rev-parse", "HEAD", cwd=repository)

    with pytest.raises(InvalidInputError, match="1 MiB"):
        checks_at_base(repository, base)
