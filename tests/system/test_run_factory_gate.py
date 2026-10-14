"""Tests of the factory gate driver: arguments, evidence, exit codes, scrubbing and stubs.

These tests never start a container. They prove the driver refuses what it must refuse, that the
evidence builder cannot be talked into a pass by an assertion that did not run, and that the
loopback stubs and observers the real run relies on behave as the evidence assumes. The engine-
dependent ``runner-boundary`` scenario is run by the operator against a real engine.
"""

from __future__ import annotations

import ast
import errno
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from enginery.adapters.oci.engine import OciEngine
from enginery.domain.errors import MissingPrerequisiteError
from factory_gate_boundary import REQUIRED as BOUNDARY_REQUIRED
from factory_gate_boundary import relay_cases, required_assertions
from factory_gate_context import GateHoldError, prepare_gate
from factory_gate_evidence import (
    EXIT_FAIL,
    EXIT_HOLD,
    EXIT_PASS,
    SCHEMA_VERSION,
    EvidenceBuilder,
    EvidenceError,
    exit_code_for,
    validate_evidence,
)
from factory_gate_support import (
    RecordingListener,
    Scrubber,
    UpstreamStub,
    diff_snapshots,
    snapshot_tree,
)
from run_factory_gate import (
    CONFIRMATION_ENVIRONMENT,
    ScenarioSpec,
    default_scenarios,
    execute,
    main,
    parse_options,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
IMAGE = "sha256:" + "a" * 64
EVIDENCE = "evidence.json"


def _argv(tmp_path: Path, *extra: str, scenario: str = "runner-boundary") -> list[str]:
    return ["--scenario", scenario, "--evidence-out", str(tmp_path / EVIDENCE), *extra]


# --- argument handling -----------------------------------------------------------------------


def test_scenario_and_evidence_out_are_required(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as no_scenario:
        parse_options(["--evidence-out", str(tmp_path / EVIDENCE)])
    with pytest.raises(SystemExit) as no_evidence:
        parse_options(["--scenario", "runner-boundary"])
    with pytest.raises(SystemExit) as unknown:
        parse_options(_argv(tmp_path, scenario="runner-everything"))
    assert (no_scenario.value.code, no_evidence.value.code, unknown.value.code) == (2, 2, 2)


def test_runner_auth_requires_a_harness_and_boundary_refuses_one(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as missing:
        parse_options(_argv(tmp_path, scenario="runner-auth"))
    with pytest.raises(SystemExit) as stray:
        parse_options(_argv(tmp_path, "--harness", "omp"))
    assert missing.value.code == 2
    assert stray.value.code == 2
    options = parse_options(_argv(tmp_path, "--harness", "claude", scenario="runner-auth"))
    assert (options.scenario, options.harness, options.mode) == ("runner-auth", "claude", "local")


@pytest.mark.parametrize(
    "extra",
    [
        ["--live"],
        ["--live", "--gateway-url", "http://127.0.0.1:4000"],
        ["--live", "--gateway-token-file", "/tmp/token"],
        ["--confirm-single-session"],
        ["--gateway-url", "http://127.0.0.1:4000"],
        ["--gateway-token-file", "/tmp/token"],
        ["--gateway-url", "ftp://127.0.0.1", "--gateway-token-file", "/tmp/token"],
        ["--runner-image", "sha256:short"],
        ["--relay-image", "enginery-relay:dev"],
        ["--session-deadline-seconds", "901"],
        ["--session-deadline-seconds", "0"],
    ],
)
def test_unsafe_or_incomplete_arguments_are_usage_errors(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_options(_argv(tmp_path, "--harness", "omp", *extra, scenario="runner-auth"), {})
    assert refused.value.code == 2


def test_live_without_a_custody_receipt_is_a_usage_error(tmp_path: Path) -> None:
    argv = _argv(
        tmp_path,
        "--harness",
        "omp",
        "--live",
        "--confirm-single-session",
        "--gateway-url",
        "http://host.docker.internal:4000",
        "--gateway-token-file",
        str(tmp_path / "gateway.token"),
        scenario="runner-auth",
    )
    with pytest.raises(SystemExit) as refused:
        parse_options(argv, {})
    assert refused.value.code == 2


def test_live_needs_gateway_arguments_and_an_explicit_confirmation(tmp_path: Path) -> None:
    live = [
        "--harness",
        "omp",
        "--live",
        "--custody-receipt",
        str(tmp_path / "custody.json"),
        "--gateway-url",
        "http://host.docker.internal:4000",
        "--gateway-token-file",
        str(tmp_path / "gateway.token"),
    ]
    with pytest.raises(SystemExit) as unconfirmed:
        parse_options(_argv(tmp_path, *live, scenario="runner-auth"), {})
    assert unconfirmed.value.code == 2
    confirmed = parse_options(
        _argv(tmp_path, *live, "--confirm-single-session", scenario="runner-auth"), {}
    )
    from_environment = parse_options(
        _argv(tmp_path, *live, scenario="runner-auth"), {CONFIRMATION_ENVIRONMENT: "1"}
    )
    assert confirmed.live and confirmed.mode == "live" and confirmed.confirm_single_session
    assert from_environment.live and from_environment.confirm_single_session
    assert confirmed.gateway_token_file == tmp_path / "gateway.token"


def test_a_live_run_never_starts_without_the_confirmation(tmp_path: Path) -> None:
    started: list[object] = []

    def prepare(*args: Any, **kwargs: Any) -> Any:
        started.append((args, kwargs))
        raise AssertionError("a refused live run must not reach the engine")

    with pytest.raises(SystemExit):
        main(
            _argv(
                tmp_path,
                "--harness",
                "omp",
                "--live",
                "--custody-receipt",
                str(tmp_path / "custody.json"),
                "--gateway-url",
                "http://127.0.0.1:4000",
                "--gateway-token-file",
                str(tmp_path / "gateway.token"),
                scenario="runner-auth",
            ),
            prepare=prepare,
            environ={},
        )
    assert started == []
    assert not (tmp_path / EVIDENCE).exists()


# --- evidence builder and exit codes ---------------------------------------------------------

REQUIRED = {"one": "first thing", "two": "second thing"}


def _builder(**kwargs: Any) -> EvidenceBuilder:
    return EvidenceBuilder(scenario="runner-boundary", mode="local", required=REQUIRED, **kwargs)


def _identities() -> dict[str, Any]:
    return {
        key: {}
        for key in ("subject", "build", "config", "host", "engine", "image", "profile", "evaluator")
    }


def _record(builder: EvidenceBuilder, assertion_id: str, *, passed: bool = True) -> None:
    builder.assertion(
        assertion_id,
        passed=passed,
        commands=["a command"],
        observed={"result": "something"},
        expected="something else",
        observer={"host": "observation"},
        raw_artifacts=["x.raw/one.json"],
    )


def _build(builder: EvidenceBuilder) -> dict[str, Any]:
    return builder.build(
        identities=_identities(),
        effects={"hosted_inference_calls": 0},
        raw_artifacts_dir="x.raw",
    )


def test_every_required_assertion_passing_is_the_only_way_to_exit_zero() -> None:
    builder = _builder()
    _record(builder, "one")
    _record(builder, "two")
    document = _build(builder)
    assert document["schema_version"] == SCHEMA_VERSION == 1
    assert document["verdict"] == "pass"
    assert validate_evidence(document) == []
    assert exit_code_for(document) == EXIT_PASS


def test_an_assertion_that_did_not_run_fails_the_scenario() -> None:
    builder = _builder()
    _record(builder, "one")
    document = _build(builder)
    missing = next(entry for entry in document["assertions"] if entry["id"] == "two")
    assert (missing["ran"], missing["status"]) == (False, "fail")
    assert document["verdict"] == "fail"
    assert "two" in document["reason"]
    assert exit_code_for(document) == EXIT_FAIL


def test_a_failed_assertion_or_control_fails_the_scenario() -> None:
    failed = _builder()
    _record(failed, "one")
    _record(failed, "two", passed=False)
    assert exit_code_for(_build(failed)) == EXIT_FAIL
    control = _builder()
    _record(control, "one")
    _record(control, "two")
    control.control(
        "positive",
        description="the attempt code ran",
        passed=False,
        observed={},
        expected="it ran",
    )
    document = _build(control)
    assert document["verdict"] == "fail" and "positive" in document["reason"]
    assert exit_code_for(document) == EXIT_FAIL


def test_a_step_error_is_a_failed_assertion_never_a_skip() -> None:
    builder = _builder()
    _record(builder, "one")
    builder.fail_assertion("two", "step raised OSError")
    document = _build(builder)
    entry = next(item for item in document["assertions"] if item["id"] == "two")
    assert entry["status"] == "fail" and entry["ran"] is False
    assert exit_code_for(document) == EXIT_FAIL


def test_a_hold_has_its_own_verdict_and_exit_code() -> None:
    builder = _builder()
    builder.hold("no qualified container engine is available")
    document = _build(builder)
    assert document["verdict"] == "hold"
    assert "engine" in document["reason"]
    assert all(
        entry["status"] == "fail" and entry["ran"] is False for entry in document["assertions"]
    )
    assert exit_code_for(document) == EXIT_HOLD == 3


def test_an_assertion_needs_a_host_side_observer_and_gets_a_raw_artifact() -> None:
    builder = _builder()
    with pytest.raises(EvidenceError, match="independent host-side observation"):
        builder.assertion("one", passed=True, commands=["c"], observed={}, expected="e")
    written: list[tuple[str, Any]] = []

    def writer(name: str, content: Any) -> str:
        written.append((name, content))
        return f"x.raw/{name}"

    builder.raw_writer = writer
    builder.assertion(
        "one", passed=True, commands=["c"], observed={"a": 1}, expected="e", observer={"b": 2}
    )
    document = _build(builder)
    entry = next(item for item in document["assertions"] if item["id"] == "one")
    assert entry["raw_artifacts"] == ["x.raw/assertions/one.json"]
    assert written[0][1]["observer"] == {"b": 2}


def test_unknown_and_repeated_assertions_are_refused() -> None:
    builder = _builder()
    with pytest.raises(EvidenceError):
        _record(builder, "three")
    _record(builder, "one")
    with pytest.raises(EvidenceError):
        _record(builder, "one")


def test_a_tampered_pass_cannot_exit_zero() -> None:
    builder = _builder()
    _record(builder, "one")
    _record(builder, "two")
    document = _build(builder)
    document["assertions"][1]["ran"] = False
    assert exit_code_for(document) == EXIT_FAIL
    document = _build(_complete_builder())
    document["assertions"][0]["status"] = "fail"
    assert exit_code_for(document) == EXIT_FAIL
    document = _build(_complete_builder())
    document["assertions"][0]["commands"] = []
    assert validate_evidence(document)
    assert exit_code_for(document) == EXIT_FAIL
    document = _build(_complete_builder())
    document["assertions"][0]["observer"] = None
    assert any("host-side observation" in item for item in validate_evidence(document))
    assert exit_code_for(document) == EXIT_FAIL


def _complete_builder() -> EvidenceBuilder:
    builder = _builder()
    _record(builder, "one")
    _record(builder, "two")
    return builder


def test_schema_validation_names_every_missing_part() -> None:
    assert validate_evidence({}) != []
    document = _build(_complete_builder())
    for key in ("identities", "assertions", "external_effect_counts", "verdict"):
        broken = {name: value for name, value in document.items() if name != key}
        assert validate_evidence(broken), key
    broken = json.loads(json.dumps(document))
    del broken["identities"]["evaluator"]
    del broken["assertions"][0]["observer"]
    del broken["external_effect_counts"]["hosted_inference_calls"]
    problems = validate_evidence(broken)
    assert any("evaluator" in item for item in problems)
    assert any("observer" in item for item in problems)
    assert any("hosted_inference_calls" in item for item in problems)


def test_token_values_are_scrubbed_from_the_evidence() -> None:
    token = "gateway-token-value-1234567890"
    scrubber = Scrubber()
    scrubber.register("gateway-token", token)
    builder = _builder(scrub=scrubber.data)
    builder.assertion(
        "one",
        passed=True,
        commands=[f"curl -H 'Authorization: Bearer {token}'"],
        observed={"nested": [{"header": f"Bearer {token}"}], token: "key too"},
        expected=f"never {token}",
        observer={"seen": token},
    )
    _record(builder, "two")
    document = _build(builder)
    text = json.dumps(document)
    assert token not in text
    assert "<redacted:gateway-token>" in text
    assert scrubber.contains(text) == []
    assert scrubber.contains(f"x {token} y") == ["gateway-token"]


# --- execute() with a fake engine seam -------------------------------------------------------


class FakeGate:
    """Just enough of ``Gate`` for ``execute``: no engine, no containers."""

    def __init__(self, evidence: EvidenceBuilder) -> None:
        self.evidence = evidence
        self.effects: dict[str, Any] = {}
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True

    def identities(self, required: dict[str, str]) -> dict[str, Any]:
        return _identities()


def _fake_prepare(holder: list[FakeGate]) -> Any:
    def prepare(options: Any, evidence: EvidenceBuilder, *args: Any, **kwargs: Any) -> FakeGate:
        gate = FakeGate(evidence)
        holder.append(gate)
        return gate

    return prepare


def _spec(run: Any, required: dict[str, str] | None = None) -> dict[str, ScenarioSpec]:
    wanted = required or REQUIRED
    return {
        "runner-boundary": ScenarioSpec(
            required=lambda options: dict(wanted),
            need_relay=False,
            run=run,
            finalize=lambda gate: None,
        )
    }


def _record_all(gate: Any) -> None:
    for assertion_id in REQUIRED:
        gate.evidence.assertion(
            assertion_id,
            passed=True,
            commands=["c"],
            observed={},
            expected="e",
            observer={"host": "observed"},
        )


def test_execute_passes_only_when_the_scenario_records_everything(tmp_path: Path) -> None:
    holder: list[FakeGate] = []
    options = parse_options(_argv(tmp_path))
    document, code = execute(options, scenarios=_spec(_record_all), prepare=_fake_prepare(holder))
    assert (document["verdict"], code) == ("pass", 0)
    assert holder[0].cleaned is True


def test_execute_fails_when_the_scenario_skips_an_assertion(tmp_path: Path) -> None:
    def partial(gate: Any) -> None:
        gate.evidence.assertion(
            "one", passed=True, commands=["c"], observed={}, expected="e", observer={"h": 1}
        )

    holder: list[FakeGate] = []
    document, code = execute(
        parse_options(_argv(tmp_path)), scenarios=_spec(partial), prepare=_fake_prepare(holder)
    )
    assert (document["verdict"], code) == ("fail", 1)
    assert holder[0].cleaned is True
    assert [e["id"] for e in document["assertions"] if not e["ran"]] == ["two"]


def test_execute_turns_a_scenario_crash_into_a_failure_after_cleaning_up(tmp_path: Path) -> None:
    def crash(gate: Any) -> None:
        raise RuntimeError("boom")

    holder: list[FakeGate] = []
    document, code = execute(
        parse_options(_argv(tmp_path)), scenarios=_spec(crash), prepare=_fake_prepare(holder)
    )
    assert code == 1 and document["verdict"] == "fail"
    assert "RuntimeError" in document["reason"]
    assert holder[0].cleaned is True


def test_a_hold_raised_by_the_scenario_is_a_hold(tmp_path: Path) -> None:
    def hold(gate: Any) -> None:
        raise GateHoldError("the relay image is missing")

    document, code = execute(
        parse_options(_argv(tmp_path)), scenarios=_spec(hold), prepare=_fake_prepare([])
    )
    assert (document["verdict"], code) == ("hold", 3)
    assert "relay image" in document["reason"]


def _engine_that_cannot_run(stderr: str) -> Any:
    def runner(argv: Any, *, timeout: float | None = None, input: str | None = None) -> Any:  # noqa: A002
        return subprocess.CompletedProcess(argv, 1, "", stderr)

    return lambda context: OciEngine(context=context, runner=runner)


def test_a_missing_engine_holds_the_real_preparation(tmp_path: Path) -> None:
    document, code = execute(
        parse_options(_argv(tmp_path)),
        engine_factory=_engine_that_cannot_run("Cannot connect to the Docker daemon"),
    )
    assert code == 3
    assert document["verdict"] == "hold"
    assert "no qualified container engine" in document["reason"]
    assert document["assertions"]
    assert all(not entry["ran"] and entry["status"] == "fail" for entry in document["assertions"])
    assert document["external_effect_counts"]["hosted_inference_calls"] == 0
    assert validate_evidence(document) == []


def test_a_bogus_docker_context_is_a_hold_not_a_pass(tmp_path: Path) -> None:
    options = parse_options(_argv(tmp_path, "--docker-context", "no-such-context-for-the-gate"))
    with pytest.raises(GateHoldError):
        prepare_gate(
            options,
            _builder(),
            Scrubber(),
            _engine_that_cannot_run("context not found"),
            need_relay=False,
        )


def test_a_missing_gateway_token_file_holds_before_any_container(tmp_path: Path) -> None:
    options = parse_options(
        _argv(
            tmp_path,
            "--gateway-url",
            "http://127.0.0.1:4000",
            "--gateway-token-file",
            str(tmp_path / "absent.token"),
        )
    )
    prepared: list[object] = []
    document, code = execute(
        options, scenarios=_spec(_record_all), prepare=lambda *a, **k: prepared.append(1)
    )
    assert code == 3 and prepared == []
    assert "gateway token file" in document["reason"]


def test_a_secret_that_reaches_a_raw_artifact_fails_the_run(tmp_path: Path) -> None:
    token_file = tmp_path / "gateway.token"
    token_file.write_text("super-secret-gateway-token-value\n", encoding="utf-8")
    token_file.chmod(0o600)
    options = parse_options(
        _argv(
            tmp_path,
            "--gateway-url",
            "http://127.0.0.1:4000",
            "--gateway-token-file",
            str(token_file),
        )
    )

    def leak(gate: Any) -> None:
        _record_all(gate)
        options.raw_dir.mkdir(parents=True, exist_ok=True)
        (options.raw_dir / "leak.txt").write_text("super-secret-gateway-token-value")

    document, code = execute(options, scenarios=_spec(leak), prepare=_fake_prepare([]))
    assert code == 1
    assert document["verdict"] == "fail"
    assert "leak.txt" in document["reason"]
    assert "super-secret-gateway-token-value" not in json.dumps(document)


def test_main_writes_the_evidence_and_returns_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        _argv(tmp_path),
        scenarios=_spec(_record_all),
        prepare=_fake_prepare([]),
    )
    printed = json.loads(capsys.readouterr().out)
    written = json.loads((tmp_path / EVIDENCE).read_text(encoding="utf-8"))
    assert code == 0
    assert printed == written
    assert written["scenario"] == "runner-boundary" and written["mode"] == "local"
    code = main(
        _argv(tmp_path),
        engine_factory=_engine_that_cannot_run("daemon is not running"),
    )
    assert code == 3
    assert json.loads((tmp_path / EVIDENCE).read_text(encoding="utf-8"))["verdict"] == "hold"


def test_scenario_requirements_follow_the_arguments(tmp_path: Path) -> None:
    plain = parse_options(_argv(tmp_path))
    with_gateway = parse_options(
        _argv(
            tmp_path,
            "--gateway-url",
            "http://127.0.0.1:4000",
            "--gateway-token-file",
            str(tmp_path / "t"),
        )
    )
    auth = parse_options(_argv(tmp_path, "--harness", "omp", scenario="runner-auth"))
    scenarios = default_scenarios()
    assert "f-relay-real-gateway-models-and-denials" not in scenarios["runner-boundary"].required(
        plain
    )
    assert "f-relay-real-gateway-models-and-denials" in required_assertions(with_gateway)
    auth_required = scenarios["runner-auth"].required(auth)
    assert {"auth-custody-receipt", "auth-no-token-in-artifacts", "a-read-protected-paths"} <= set(
        auth_required
    )
    assert all(not key.startswith(("b-", "g-", "i-")) for key in auth_required)


def test_every_required_boundary_assertion_has_a_recording_site() -> None:
    source = (SCRIPTS / "factory_gate_boundary.py").read_text(encoding="utf-8")
    orphaned = [key for key in BOUNDARY_REQUIRED if source.count(f'"{key}"') < 2]
    assert orphaned == []
    auth_source = (SCRIPTS / "factory_gate_auth.py").read_text(encoding="utf-8")
    auth = default_scenarios()["runner-auth"].required(
        parse_options(_argv(Path("/nonexistent"), "--harness", "omp", scenario="runner-auth"))
    )
    unrecorded = [
        key for key in auth if key.startswith("auth-") and auth_source.count(f'"{key}"') < 2
    ]
    assert unrecorded == []


# --- stubs and observers the real run relies on ----------------------------------------------


def test_recording_listener_counts_every_connection() -> None:
    listener = RecordingListener("gateway-test", "127.0.0.1")
    port = listener.start()
    try:
        assert listener.count() == 0
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(b"GET / HTTP/1.0\r\n\r\n")
            assert b"gate-stub-reply:gateway-test" in connection.recv(256)
        assert listener.count() == 1
        assert listener.connections()[0]["peer"] == "127.0.0.1"
    finally:
        listener.stop()


def _http(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_upstream_stub_records_requests_and_answers_valid_streams() -> None:
    stub = UpstreamStub(expected_bearer="gateway-bearer-for-the-test", reply_text="OK")
    port = stub.start("127.0.0.1")
    base = f"http://127.0.0.1:{port}"
    try:
        bearer = {"Authorization": "Bearer gateway-bearer-for-the-test"}
        status, body = _http(f"{base}/v1/models?case=1", headers=bearer)
        assert status == 200 and "anthropic/claude-gate-1" in body
        status, body = _http(
            f"{base}/v1/messages",
            data=json.dumps({"model": "anthropic/claude-gate-1", "stream": True}).encode(),
            headers={**bearer, "Content-Type": "application/json"},
        )
        assert status == 200
        assert "event: message_start" in body and '"text": "OK"' in body
        status, body = _http(
            f"{base}/v1/pi/stream",
            data=json.dumps({"modelId": "anthropic/claude-gate-1"}).encode(),
            headers={"Authorization": "Bearer something-else"},
        )
        assert status == 200 and '"type": "done"' in body
        assert _http(f"{base}/__gate_probe")[0] == 200
        assert _http(f"{base}/v1/usage")[0] == 404
        requests = stub.snapshot()
        assert [r.path for r in requests] == [
            "/v1/models?case=1",
            "/v1/messages",
            "/v1/pi/stream",
            "/v1/usage",
        ]
        assert [r.authorization_matches_gateway for r in requests] == [True, True, False, False]
        assert len(stub.snapshot(include_probes=True)) == 5
        assert "gateway-bearer-for-the-test" not in json.dumps([r.to_state() for r in requests])
    finally:
        stub.stop()


def test_tree_snapshots_detect_content_mode_and_membership_changes(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "nested").mkdir(parents=True)
    (root / "a.txt").write_text("one")
    (root / "nested" / "b.txt").write_text("two")
    before = snapshot_tree(root)
    assert diff_snapshots(before, snapshot_tree(root)) == []
    (root / "a.txt").write_text("changed")
    (root / "nested" / "c.txt").write_text("new")
    (root / "nested" / "b.txt").chmod(0o600)
    changes = diff_snapshots(before, snapshot_tree(root))
    assert "changed: a.txt" in changes
    assert "added: nested/c.txt" in changes
    assert "changed: nested/b.txt" in changes
    assert snapshot_tree(tmp_path / "absent") == {".": "absent"}


def test_relay_case_matrix_has_unique_markers_and_no_denied_forward() -> None:
    cases = relay_cases(SimpleNamespace(rid="test0001"))  # type: ignore[arg-type]
    assert len({case["id"] for case in cases}) == len(cases)
    assert len({case["marker"] for case in cases}) == len(cases)
    forwarded = [case for case in cases if case["forward"]]
    assert {case["id"] for case in forwarded} == {
        "A2-models",
        "A3-messages",
        "A4-messages-bare-claude",
        "A5-pi-stream-anthropic",
        "A6-pi-stream-codex",
    }
    for case in cases:
        if not case["forward"]:
            assert case["statuses"], case["id"]
            assert 200 not in case["statuses"] or case["id"] == "A1-healthz"
    covered = " ".join(f"{case['path']} {case['body']}" for case in cases)
    for needle in (
        "google-antigravity/",
        "openai/gpt",
        "/v1/credentials/check",
        "/v1/usage",
        "/v1/chat/completions",
        "/v1/embeddings",
        "claude-gate-bare",
    ):
        assert needle in covered, needle


# --- the scripts stay self-contained ---------------------------------------------------------


def test_probe_is_standard_library_only_and_scripts_never_import_tests() -> None:
    probe = ast.parse((SCRIPTS / "factory_gate_probe.py").read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0] if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(probe)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in (node.names if isinstance(node, ast.Import) else [None])  # type: ignore[list-item]
    }
    imported = {name.split(".")[0] for name in imported if name}
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported - set(
        sys.stdlib_module_names
    )
    for path in SCRIPTS.glob("*gate*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(name == "tests" or name.startswith("tests.") for name in names), path


def test_probe_attempt_recorder_judges_refusals_and_performances() -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import factory_gate_probe as probe
    finally:
        sys.path.remove(str(SCRIPTS))
    recorder = probe.Recorder()

    def refused() -> None:
        raise PermissionError(1, "denied")

    refused_entry = recorder.attempt("denied", "open('/x')", refused)
    performed_entry = recorder.attempt("control", "noop", lambda: "done", expected="performed")
    surprise = recorder.attempt("surprise", "noop", lambda: "done")
    assert refused_entry["ok"] and refused_entry["error"] == "EPERM"
    assert performed_entry["ok"] and performed_entry["outcome"] == "performed"
    assert not surprise["ok"] and surprise["outcome"] == "performed"
    assert set(probe.MODES) >= {
        "read",
        "write",
        "escape",
        "network",
        "git",
        "plant",
        "relay",
        "memory",
        "pids",
        "cpu",
        "sleep",
    }


def test_the_driver_documents_its_exit_codes_and_modes() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_factory_gate.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0
    for flag in (
        "--scenario",
        "--evidence-out",
        "--harness",
        "--live",
        "--docker-context",
        "--runner-image",
        "--relay-image",
        "--gateway-url",
        "--gateway-token-file",
        "--confirm-single-session",
        "--custody-receipt",
    ):
        assert flag in completed.stdout, flag


def test_missing_engine_cli_is_a_prerequisite_error() -> None:
    engine = OciEngine(executable="enginery-gate-no-such-docker-binary")
    with pytest.raises(MissingPrerequisiteError):
        engine.qualify()


# --- review assertions, probe classification, subject commit, observed inference ----------------


def test_every_review_assertion_is_required_and_has_a_recording_site() -> None:
    import factory_gate_review as review

    options = parse_options(_argv(Path("/nonexistent")))
    required = required_assertions(options)
    assert set(review.REQUIRED) <= set(required)
    assert all(key.startswith("r-") for key in review.REQUIRED)
    source = (SCRIPTS / "factory_gate_review.py").read_text(encoding="utf-8")
    assert [key for key in review.REQUIRED if source.count(f'"{key}"') < 2] == []
    # A review assertion that never ran is a failure, not a skip.
    builder = EvidenceBuilder(
        scenario="runner-boundary",
        mode="local",
        required={"r-m1-no-host-objects-in-workspace": "x"},
    )
    document = _build(builder)
    assert exit_code_for(document) == EXIT_FAIL
    assert document["assertions"][0]["ran"] is False


def test_probe_counts_only_explicit_refusals_as_refused() -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import factory_gate_probe as probe
    finally:
        sys.path.remove(str(SCRIPTS))
    recorder = probe.Recorder()

    def raising(error: BaseException) -> Any:
        def action() -> None:
            raise error

        return action

    cases = {
        "erofs": (OSError(errno.EROFS, "ro"), "refused"),
        "refused": (ConnectionRefusedError(errno.ECONNREFUSED, "no"), "refused"),
        "unreachable": (OSError(errno.ENETUNREACH, "net"), "refused"),
        "lookup": (socket.gaierror(socket.EAI_NONAME, "unknown"), "refused"),
        "timeout": (TimeoutError("timed out"), "inconclusive"),
        "unexpected": (ValueError("boom"), "inconclusive"),
        "odd-errno": (OSError(errno.EIO, "io"), "inconclusive"),
    }
    for name, (error, outcome) in cases.items():
        entry = recorder.attempt(name, name, raising(error))
        assert entry["outcome"] == outcome, name
        assert entry["ok"] is (outcome == "refused"), name


def test_git_failure_is_refused_only_with_a_specific_signature() -> None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import factory_gate_probe as probe
    finally:
        sys.path.remove(str(SCRIPTS))
    refused = probe.git_failure_outcome(
        128, "fatal: unable to access 'https://x/': Could not resolve host: x\n"
    )
    assert refused[0] == "refused"
    assert (
        probe.git_failure_outcome(128, "fatal: 'x' does not appear to be a git repository")[0]
        == "refused"
    )
    # A failure that says nothing about the boundary is inconclusive, never a refusal.
    assert probe.git_failure_outcome(1, "error: pathspec 'x' did not match")[0] == "inconclusive"
    assert probe.git_failure_outcome(128, "")[0] == "inconclusive"
    assert probe.git_failure_outcome(0, "") == ("performed", "")


def test_subject_commit_is_validated_and_recorded(tmp_path: Path) -> None:
    sha = "a" * 40
    options = parse_options(_argv(tmp_path, "--subject-commit", sha))
    assert options.subject_commit == sha
    with pytest.raises(SystemExit) as bad:
        parse_options(_argv(tmp_path, "--subject-commit", "not-a-sha"))
    assert bad.value.code == 2
    env = parse_options(_argv(tmp_path), {"ENGINERY_FACTORY_GATE_SUBJECT_COMMIT": sha})
    assert env.subject_commit == sha
    from factory_gate_context import subject_identity

    subject = subject_identity(sha)
    assert subject["source"]["kind"] in ("git", "installed-wheel", "unknown")
    if subject["source"]["kind"] == "git":
        assert subject["commit_sha"] == subject["source"]["git_sha"]
    assert subject["declared_commit_sha"] == sha


def test_wheel_subject_declares_but_does_not_verify_its_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import enginery
    from factory_gate_context import subject_identity

    installed = tmp_path / "site" / "enginery"
    installed.mkdir(parents=True)
    monkeypatch.setattr(enginery, "__file__", str(installed / "__init__.py"))
    sha = "b" * 40

    subject = subject_identity(sha)

    assert subject["source"]["kind"] != "git"
    assert subject["commit_sha"] is None
    assert subject["declared_commit_sha"] == sha
    assert subject["declared_commit_verified"] is False
    assert subject_identity(None).get("declared_commit_sha") is None


def test_custody_receipt_is_for_runner_auth_only(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stray:
        parse_options(_argv(tmp_path, "--custody-receipt", str(tmp_path / "c.json")))
    assert stray.value.code == 2
    ok = parse_options(
        _argv(
            tmp_path,
            "--harness",
            "omp",
            "--custody-receipt",
            str(tmp_path / "c.json"),
            scenario="runner-auth",
        )
    )
    assert ok.custody_receipt == tmp_path / "c.json"


def test_hosted_inference_is_derived_from_observed_connections() -> None:
    from factory_gate_boundary import _observed_hosted_inference

    def gate(ops: list[dict[str, Any]], effects: dict[str, Any]) -> Any:
        attempt = SimpleNamespace(name="network", operations=ops)
        return SimpleNamespace(attempts=[attempt], effects=effects)

    clean = _observed_hosted_inference(
        gate(
            [{"id": "tcp:internet-1.1.1.1:443", "outcome": "refused"}],
            {"relay_forward_log_entries": 5, "relay_upstream_requests_forwarded": 5},
        ),
        {"gateway-loopback": 0, "broker-any": 0},
    )
    assert clean["hosted_inference_calls"] == 0 and clean["relay_forward_log_matches_stub"]
    connected = _observed_hosted_inference(
        gate([{"id": "tcp:internet-1.1.1.1:443", "outcome": "connected"}], {}), {}
    )
    assert connected["hosted_inference_calls"] == 1
    leaked = _observed_hosted_inference(
        gate([], {"relay_forward_log_entries": 4, "relay_upstream_requests_forwarded": 3}),
        {"gateway-any": 2},
    )
    assert leaked["hosted_inference_calls"] == 1
    assert not leaked["relay_forward_log_matches_stub"]
    assert leaked["gateway_and_broker_listener_connections"] == 2


def test_git_bomb_must_be_refused_for_the_tree_cap_reason() -> None:
    import inspect

    import factory_gate_review as review
    from enginery.engine import task_repository

    # The gate keys the verdict on the product's own refusal text, so a rewording of it must
    # break this test rather than silently turn the assertion into "refused for any reason".
    assert review._TREE_CAP_REFUSAL in inspect.getsource(task_repository)
    step = inspect.getsource(review.step_m3_bomb)
    assert "_TREE_CAP_REFUSAL" in step


def test_relay_description_is_egress_only_not_dual_homed() -> None:
    import factory_gate_boundary as boundary

    text = boundary.REQUIRED["f-relay-hardened-and-reachable"]
    assert "dual-homed" not in text
    assert "egress-only" in text


def test_probe_observation_prefers_the_workspace_copy_over_a_forgeable_result(
    tmp_path: Path,
) -> None:
    from factory_gate_support import (
        OBS_FILE_IN_WORKSPACE,
        PARAMS_IN_CONTAINER,
        PROBE_IN_CONTAINER,
        probe_seed_files,
        read_probe_observation,
    )

    forged = {"output": 'noise\nGATE_OBS {"kind": "replayed-from-another-attempt"}\n'}
    assert read_probe_observation(forged, tmp_path) == {"kind": "replayed-from-another-attempt"}
    assert read_probe_observation({"output": "no observation here"}, tmp_path) is None
    copy = tmp_path / OBS_FILE_IN_WORKSPACE
    copy.parent.mkdir(parents=True)
    copy.write_text('{"kind": "own"}', encoding="utf-8")
    assert read_probe_observation(forged, tmp_path) == {"kind": "own"}

    seeds = dict(probe_seed_files({"mode": "x"}))
    assert set(seeds) == {PROBE_IN_CONTAINER, PARAMS_IN_CONTAINER}
    assert json.loads(seeds[PARAMS_IN_CONTAINER]) == {"mode": "x"}
    assert "def mode_disk" in seeds[PROBE_IN_CONTAINER]


def test_disk_assertions_are_the_kernel_cap_scenario() -> None:
    import factory_gate_review as review

    assert "r-m3-disk-bound-kill-hold" not in review.REQUIRED
    for key in (
        "r-m3-disk-enospc-bytes",
        "r-m3-disk-enospc-inodes",
        "r-m3-disk-parallel-fallocate-capped",
        "r-m3-disk-receipt-and-readback",
        "r-m3-memory-fill-exit-137",
    ):
        assert key in review.REQUIRED
