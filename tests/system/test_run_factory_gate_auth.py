"""Tests of the ``runner-auth`` driver's own guarantees; none starts a container or a model call.

They prove the deadline really kills the process group and the container, the harness-container
count comes from the engine's events, the token scan covers every source and fails on a planted
value, a conforming custody receipt is required, and gateway refusals hold rather than fail.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

import factory_gate_auth as auth
from enginery.adapters.oci.engine import OPERATION_LABEL
from enginery.domain.custody import CustodyHost, build_custody_receipt
from factory_gate_context import GateHoldError

# --- the deadline ----------------------------------------------------------------------------


class _Engine:
    def __init__(self) -> None:
        self.present = True
        self.calls: list[str] = []

    def kill(self, name: str) -> None:
        self.calls.append(f"kill {name}")

    def remove(self, name: str, *, force: bool = True) -> None:
        self.calls.append(f"remove {name}")
        self.present = False

    def container_exists(self, name: str) -> bool:
        return self.present


def _slow_launcher(directory: Path) -> tuple[Path, Path]:
    """A launcher that spawns a SIGTERM-ignoring grandchild and then sleeps for a long time."""
    pids = directory / "pids"
    script = directory / "slow-launcher.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os, signal, subprocess, sys, time
            child = subprocess.Popen(
                [sys.executable, "-c",
                 "import signal,time; signal.signal(15, signal.SIG_IGN); time.sleep(600)"]
            )
            open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{child.pid}}")
            time.sleep(600)
            """
        ),
        encoding="utf-8",
    )
    return script, pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_deadline_kills_the_whole_process_group_and_the_container(tmp_path: Path) -> None:
    script, pids = _slow_launcher(tmp_path)
    engine = _Engine()
    outcome = auth.supervise(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        deadline_seconds=2,
        engine=engine,
        container="enginery-test",
    )
    leader, grandchild = (int(value) for value in pids.read_text().split())
    assert outcome.timed_out
    assert outcome.group_gone and outcome.container_gone and outcome.cleaned_up
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(leader)
    assert not _alive(grandchild)
    assert "kill enginery-test" in engine.calls and "remove enginery-test" in engine.calls
    verdict, reason = auth.decide_outcome(
        live=True,
        security_ok=True,
        timed_out=outcome.timed_out,
        cleaned_up=outcome.cleaned_up,
        deadline_seconds=2,
        gateway_reason=None,
    )
    assert verdict == "hold" and reason is not None and "deadline" in reason
    local, local_reason = auth.decide_outcome(
        live=False,
        security_ok=True,
        timed_out=True,
        cleaned_up=True,
        deadline_seconds=2,
        gateway_reason=None,
    )
    assert local == "fail" and local_reason is not None and "deadline" in local_reason


def test_a_deadline_that_leaves_something_alive_is_a_failure_not_a_hold() -> None:
    verdict, reason = auth.decide_outcome(
        live=True,
        security_ok=True,
        timed_out=True,
        cleaned_up=False,
        deadline_seconds=900,
        gateway_reason=None,
    )
    assert verdict == "fail" and reason is not None and "survived" in reason


def test_a_session_that_ends_in_time_leaves_no_straggler(tmp_path: Path) -> None:
    script = tmp_path / "quick.py"
    script.write_text(
        "import subprocess,sys\n"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(600)'],\n"
        "  stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "print('done')\n",
        encoding="utf-8",
    )
    engine = _Engine()
    engine.present = False
    outcome = auth.supervise(
        [sys.executable, str(script)],
        env={"PATH": os.environ.get("PATH", "")},
        deadline_seconds=30,
        engine=engine,
        container="enginery-test",
    )
    assert not outcome.timed_out and outcome.completed.returncode == 0
    assert outcome.stragglers_killed and outcome.group_gone and outcome.cleaned_up


# --- the host-side count ---------------------------------------------------------------------


def _create(name: str, *, operation: str | None = None, image: str = "sha256:aa") -> str:
    attributes: dict[str, str] = {"name": name, "image": image}
    if operation is not None:
        attributes[OPERATION_LABEL] = operation
    return json.dumps(
        {
            "Type": "container",
            "Action": "create",
            "Actor": {"ID": "f" * 64, "Attributes": attributes},
        }
    )


def _count(*lines: str) -> auth.ContainerCount:
    return auth.count_created_containers(
        "\n".join(lines), operation_id="op-1", expected_name="enginery-me", relay_name="relay"
    )


def test_exactly_one_harness_container_is_counted_from_the_events() -> None:
    counted = _count(_create("enginery-me", operation="op-1"), _create("relay"))
    assert counted.harness == ("enginery-me",) and counted.others == ()


def test_a_second_container_of_the_session_is_counted() -> None:
    counted = _count(
        _create("enginery-me", operation="op-1"), _create("enginery-other", operation="op-1")
    )
    assert len(counted.harness) == 2


def test_other_runs_probes_and_garbage_are_not_counted_but_listed() -> None:
    counted = _count(
        _create("enginery-me", operation="op-1"),
        _create("enginery-probe-x"),
        _create("enginery-concurrent", operation="op-2"),
        "not json",
        json.dumps({"Type": "network", "Action": "create"}),
    )
    assert counted.harness == ("enginery-me",)
    assert set(counted.others) == {"enginery-probe-x", "enginery-concurrent"}


def test_no_event_for_the_session_is_zero_containers() -> None:
    assert _count().harness == ()


def test_engine_time_arithmetic_keeps_the_fraction_and_zone() -> None:
    assert auth.later("2026-10-13T22:25:39.317236424Z", 1) == "2026-10-13T22:25:40.317236424Z"
    assert auth.later("2026-10-13T23:59:59+02:00", 1) == "2026-10-14T00:00:00+02:00"
    assert auth.later("yesterday", 1) is None


# --- the token scan --------------------------------------------------------------------------

TOKEN = "gate-fake-gateway-" + "ab12" * 8


def _clean_sources(tmp_path: Path) -> tuple[dict[str, str], dict[str, Path]]:
    texts = {
        label: "nothing secret"
        for label in auth.EXPECTED_SCAN_SOURCES
        if not label.startswith("directory:")
    }
    directories = {}
    for label in auth.EXPECTED_SCAN_SOURCES:
        if label.startswith("directory:"):
            directory = tmp_path / label.removeprefix("directory:")
            directory.mkdir()
            (directory / "file.txt").write_text("nothing secret", encoding="utf-8")
            directories[label] = directory
    return texts, directories


def test_a_clean_run_finds_nothing_and_scans_every_expected_source(tmp_path: Path) -> None:
    texts, directories = _clean_sources(tmp_path)
    hits, scanned = auth.scan_for_token(TOKEN, texts, directories)
    assert hits == {}
    assert set(scanned) == set(auth.EXPECTED_SCAN_SOURCES)


@pytest.mark.parametrize(
    "label", [name for name in auth.EXPECTED_SCAN_SOURCES if not name.startswith("directory:")]
)
def test_a_planted_token_in_each_text_source_is_found(tmp_path: Path, label: str) -> None:
    texts, directories = _clean_sources(tmp_path)
    texts[label] = f"before {TOKEN} after"
    hits, _ = auth.scan_for_token(TOKEN, texts, directories)
    assert list(hits) == [label]
    assert TOKEN not in json.dumps(hits)


@pytest.mark.parametrize(
    "label", [name for name in auth.EXPECTED_SCAN_SOURCES if name.startswith("directory:")]
)
def test_a_planted_token_in_each_directory_source_is_found(tmp_path: Path, label: str) -> None:
    texts, directories = _clean_sources(tmp_path)
    nested = directories[label] / "deep" / "er"
    nested.mkdir(parents=True)
    (nested / "leak.bin").write_bytes(b"\x00" + TOKEN.encode() + b"\x00")
    hits, _ = auth.scan_for_token(TOKEN, texts, directories)
    assert hits == {label: ["deep/er/leak.bin"]}
    assert TOKEN not in json.dumps(hits)


def test_an_empty_token_cannot_be_scanned_for(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        auth.scan_for_token("", {}, {})


# --- verdict mapping -------------------------------------------------------------------------


def _entry(
    decision: str, status: str, method: str = "POST", path: str = "/v1/messages"
) -> dict[str, str]:
    return {"method": method, "path": path, "decision": decision, "status": status}


@pytest.mark.parametrize("status", ["401", "403", "429"])
def test_gateway_refusals_hold_with_a_reason(status: str) -> None:
    reason = auth.gateway_hold_reason([_entry("forward", status)], live=False, usage={"x": 1})
    assert reason is not None and status in reason
    verdict, stated = auth.decide_outcome(
        live=True,
        security_ok=True,
        timed_out=False,
        cleaned_up=True,
        deadline_seconds=900,
        gateway_reason=reason,
    )
    assert verdict == "hold" and stated == reason


def test_the_relays_own_refusals_are_not_gateway_holds() -> None:
    assert auth.gateway_hold_reason([_entry("deny", "403")], live=False, usage=None) is None


def test_a_live_session_without_usage_has_no_billing_proof() -> None:
    reason = auth.gateway_hold_reason([_entry("forward", "200")], live=True, usage=None)
    assert reason is not None and "billing proof" in reason
    assert auth.gateway_hold_reason([_entry("forward", "200")], live=True, usage={"x": 1}) is None
    assert auth.gateway_hold_reason([_entry("forward", "200")], live=False, usage=None) is None


def test_a_security_failure_is_never_downgraded_to_a_hold() -> None:
    verdict, reason = auth.decide_outcome(
        live=True,
        security_ok=False,
        timed_out=True,
        cleaned_up=False,
        deadline_seconds=900,
        gateway_reason="the gateway answered 429",
    )
    assert verdict is None and reason is None


def test_only_omps_two_discovery_gets_are_tolerated() -> None:
    tolerated = _entry("deny", "404", "GET", "/models")
    assert auth._is_refused_discovery("omp", tolerated)
    assert auth._is_refused_discovery("omp", _entry("deny", "404", "GET", "/codex/models"))
    assert not auth._is_refused_discovery("claude", tolerated)
    assert not auth._is_refused_discovery("omp", _entry("deny", "404", "POST", "/models"))
    assert not auth._is_refused_discovery("omp", _entry("deny", "404", "GET", "/v1/other"))


# --- custody ---------------------------------------------------------------------------------


def _token_file(directory: Path, mode: int = 0o600) -> Path:
    path = directory / "gateway.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _write_receipt(directory: Path, token_file: Path, gateway: str) -> Path:
    from datetime import UTC, datetime

    from enginery.cli.runner import _inspect_token_file

    receipt = build_custody_receipt(
        host=CustodyHost.MACOS,
        gateway_url=gateway,
        broker_url="http://127.0.0.1:1",
        token_file=_inspect_token_file(token_file),
        created_at=datetime.now(UTC),
    )
    path = directory / "custody.json"
    path.write_text(json.dumps(receipt.to_state()), encoding="utf-8")
    return path


def test_a_conforming_receipt_is_accepted_and_has_a_digest(tmp_path: Path) -> None:
    token_file = _token_file(tmp_path)
    path = _write_receipt(tmp_path, token_file, "http://gateway.local:8080")
    receipt = auth.load_live_custody(
        path, gateway_url="http://gateway.local:8080", token_file=token_file
    )
    assert receipt.conforms and str(receipt.digest).startswith("sha256:")


def test_a_live_run_without_a_receipt_is_held(tmp_path: Path) -> None:
    with pytest.raises(GateHoldError, match="--custody-receipt"):
        auth.load_live_custody(None, gateway_url="http://g:1", token_file=_token_file(tmp_path))


def test_a_non_conforming_receipt_is_held(tmp_path: Path) -> None:
    token_file = _token_file(tmp_path, mode=0o644)
    path = _write_receipt(tmp_path, token_file, "http://g:1")
    with pytest.raises(GateHoldError, match="does not conform"):
        auth.load_live_custody(path, gateway_url="http://g:1", token_file=token_file)


def test_a_receipt_for_another_gateway_or_a_now_loosened_token_file_is_held(tmp_path: Path) -> None:
    token_file = _token_file(tmp_path)
    path = _write_receipt(tmp_path, token_file, "http://g:1")
    with pytest.raises(GateHoldError, match="different gateway"):
        auth.load_live_custody(path, gateway_url="http://other:1", token_file=token_file)
    token_file.chmod(0o644)
    with pytest.raises(GateHoldError, match="no longer conforms"):
        auth.load_live_custody(path, gateway_url="http://g:1", token_file=token_file)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o644


def test_an_unreadable_or_malformed_receipt_is_held(tmp_path: Path) -> None:
    token_file = _token_file(tmp_path)
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    for path in (garbage, tmp_path / "absent.json"):
        with pytest.raises(GateHoldError, match="cannot be used"):
            auth.load_live_custody(path, gateway_url="http://g:1", token_file=token_file)


def test_the_local_receipt_is_built_over_the_stub_token_file_and_conforms(tmp_path: Path) -> None:
    receipt = auth.build_local_custody(
        gateway_url="http://host.docker.internal:9", token_file=_token_file(tmp_path)
    )
    assert receipt.conforms
    assert TOKEN not in json.dumps(receipt.to_state())


# --- wiring ----------------------------------------------------------------------------------


def test_the_spec_is_pointed_at_the_gates_own_relay_container() -> None:
    argv = [
        "python",
        "-m",
        "launcher",
        "--spec",
        json.dumps({"relay_container": "enginery-relay"}),
        "--",
        "x",
    ]
    rewritten = auth.with_relay_container(argv, "enginery-gate-1-relay")
    assert json.loads(rewritten[4])["relay_container"] == "enginery-gate-1-relay"
    assert json.loads(argv[4])["relay_container"] == "enginery-relay"


def test_every_new_assertion_is_registered() -> None:
    required = auth.required_assertions(None)
    for key in (
        "auth-custody-receipt",
        "auth-harness-container-count",
        "auth-no-token-in-artifacts",
    ):
        assert key in required


def test_a_second_session_is_refused() -> None:
    counter = auth.SessionCounter()
    counter.begin()
    with pytest.raises(RuntimeError):
        counter.begin()
