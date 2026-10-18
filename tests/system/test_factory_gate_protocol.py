"""Real CLI stand-in campaigns exercise both reference harness lifecycle paths."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from factory_gate_campaign import Campaign, sha256
from factory_gate_context import GateOptions
from factory_gate_protocol import REQUIRED, unsupported_probes
from run_factory_gate import main, parse_options


def argv(root: Path, harness: str, *extra: str) -> list[str]:
    return [
        "--scenario",
        "protocol",
        "--harness",
        harness,
        "--campaign-id",
        "protocol-test",
        "--evidence-out",
        str(root / f"{harness}.json"),
        *extra,
    ]


@pytest.fixture(scope="module")
def protocol_campaign(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    root = tmp_path_factory.mktemp("protocol-campaign")
    documents = {}
    for harness in ("omp", "claude"):
        code = main(argv(root, harness))
        document = json.loads((root / f"{harness}.json").read_text())
        assert code == 0, document.get("reason")
        documents[harness] = document
    return root, documents


def test_both_reference_harnesses_complete_without_hosted_calls_or_oci(
    protocol_campaign: tuple[Path, dict[str, Any]],
) -> None:
    root, documents = protocol_campaign
    for harness, document in documents.items():
        assert document["verdict"] == "pass"
        assert document["details"]["qualification_target"] == "synthetic"
        assert document["identities"]["qualification_target"] == "synthetic"
        assertions = {item["id"]: item for item in document["assertions"]}
        assert set(assertions) == set(REQUIRED)
        assert all(row["status"] == "pass" and row["ran"] for row in assertions.values())
        assert document["external_effect_counts"]["hosted_inference_calls"] == 0
        assert document["external_effect_counts"]["isolated_containers"] == 0
        assert document["external_effect_counts"]["worker_sessions"] == 2
        receipts = assertions["execution-controls"]["observed"]
        assert receipts
        assert receipts[0]["effective_effort"]["state"] == "unknown"
        assert receipts[0]["effective_model"]["state"] == "unknown"
        assert receipts[0]["submitted_arguments"] == [
            "--model",
            "fixture/model-1" if harness == "omp" else "claude-fixture-1",
            "--thinking" if harness == "omp" else "--effort",
            "medium",
        ]
        assert receipts[0]["host"]["tested_executable_version"] == (
            "18.10.3" if harness == "omp" else "2.1.296"
        )
        assert receipts[0]["host"]["observed_executable_version_at_launch"] == (
            "18.10.3" if harness == "omp" else "2.1.296"
        )
        cancellation = assertions["cancellation-reconcile"]["observed"]
        assert cancellation["terminal_status"] == "cancelled"
        assert cancellation["event_count"] > 0
        assert cancellation["group_gone"]
        assert cancellation["container_gone"] is None
        assert cancellation["cancel_status"] == cancellation["reconcile_status"] == "cancelled"
        assert cancellation["pushes"] == cancellation["pull_requests"] == 0
        assert (root / f"{harness}.raw").is_dir()


def test_protocol_accepts_symlinked_evidence_directory(tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    for harness in ("omp", "claude"):
        assert main(argv(linked, harness)) == 0
        document = json.loads((linked / f"{harness}.json").read_text())
        assert document["verdict"] == "pass"
    campaign = Campaign(parse_options(argv(linked, harness)))
    assert campaign.path == campaign.path.resolve()
    assert campaign.verify_evidence() == []


@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_canonical_cancellation_after_first_event(tmp_path: Path, harness: str) -> None:
    from factory_gate_evidence import EvidenceBuilder
    from factory_gate_protocol import ProtocolGate, _local_cancellation
    from factory_gate_support import Scrubber

    options = parse_options(argv(tmp_path, harness))
    gate = ProtocolGate(
        options,
        EvidenceBuilder(scenario="protocol", mode="local", required=REQUIRED),
        Scrubber(),
        None,
    )
    key = gate.campaign.reserve(harness, "cancellation")
    gate.campaign.transition(key, "running")
    result = _local_cancellation(gate, f"cancel-{harness}")
    assert result["terminal_status"] == "cancelled"
    assert result["group_gone"] and result["container_gone"] is None
    assert gate.campaign.read()["slots"][key]["cancel_issued"] is True


def test_completed_campaign_raw_digests_recover_exactly(
    protocol_campaign: tuple[Path, dict[str, Any]],
) -> None:
    root, _ = protocol_campaign
    manifest = Campaign(
        GateOptions(
            scenario="protocol", evidence_out=root / "omp.json", campaign_id="protocol-test"
        )
    )
    assert manifest.verify_evidence() == []
    document = manifest.read()
    assert len(document["slots"]) == 4
    for key, slot in document["slots"].items():
        assert slot["state"] == ("cancelled" if key.endswith("cancellation") else "completed")
        record = document["evidence"][key]
        assert sha256(Path(record["path"])) == record["sha256"]
        assert record["raw_originals"]
        for name, digest in record["raw_originals"].items():
            assert sha256(Path(name)) == digest


@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_unsupported_controls_hold_without_reserving_a_slot(harness: str, tmp_path: Path) -> None:
    holds = unsupported_probes(harness)
    assert {row["control"] for row in holds} == {"model", "effort"}
    assert all(row["reason"] for row in holds)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("extra", [[], ["--harness", "omp"], ["--campaign-id", "valid-id"]])
def test_protocol_requires_harness_and_campaign(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        parse_options(
            ["--scenario", "protocol", "--evidence-out", str(tmp_path / "e.json"), *extra],
            environ={},
        )
    assert stopped.value.code == 2


@pytest.mark.parametrize(
    "scenario",
    [
        "setup",
        "manual-path",
        "behavioral-acceptance",
        "review-authority",
        "runner-boundary",
        "runner-auth",
    ],
)
def test_other_scenarios_reject_campaign_id(tmp_path: Path, scenario: str) -> None:
    with pytest.raises(SystemExit):
        parse_options(
            [
                "--scenario",
                scenario,
                "--campaign-id",
                "valid-id",
                "--evidence-out",
                str(tmp_path / "e.json"),
                *(["--harness", "omp"] if scenario == "runner-auth" else []),
            ],
            environ={},
        )


@pytest.mark.parametrize(
    "missing",
    [
        "--runner-image",
        "--relay-image",
        "--docker-context",
        "--subject-commit",
        "--campaign-config",
    ],
)
def test_live_protocol_requires_each_bound_input_before_execution(
    tmp_path: Path, missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    bound = {
        "--runner-image": "sha256:" + "a" * 64,
        "--relay-image": "sha256:" + "b" * 64,
        "--docker-context": "desktop-linux",
        "--subject-commit": "c" * 40,
        "--campaign-config": str(tmp_path / "campaign.json"),
    }
    arguments = argv(
        tmp_path,
        "omp",
        "--live",
        "--confirm-single-session",
        "--gateway-url",
        "http://127.0.0.1:47400",
        "--gateway-token-file",
        str(tmp_path / "token"),
        "--custody-receipt",
        str(tmp_path / "custody.json"),
    )
    for flag, value in bound.items():
        if flag != missing:
            arguments.extend((flag, value))
    with pytest.raises(SystemExit):
        parse_options(arguments, environ={})
    assert f"live protocol requires {missing}" in capsys.readouterr().err


def test_gateway_token_in_retained_evidence_is_scrubbed_and_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import factory_gate_protocol as protocol
    from factory_gate_context import GateHoldError

    secret = "local-stand-in-gateway-token"
    token = tmp_path / "gateway.token"
    token.write_text(secret)
    token.chmod(0o600)

    def leak(gate: protocol.ProtocolGate) -> dict[str, Any]:
        (gate.options.raw_dir / "leaked.bin").write_bytes(b"\xff" + secret.encode())
        raise GateHoldError("injected retained-artifact credential exposure")

    monkeypatch.setattr(protocol, "_local_workflow", leak)
    code = main(
        argv(
            tmp_path,
            "omp",
            "--gateway-url",
            "http://127.0.0.1:47400",
            "--gateway-token-file",
            str(token),
        )
    )
    document = json.loads((tmp_path / "omp.json").read_text())
    assert code == 3 and document["verdict"] == "hold"
    assert document["details"]["secret_hold"]["scrubbed"]
    assert secret.encode() not in (tmp_path / "omp.raw" / "leaked.bin").read_bytes()


@pytest.mark.parametrize("harness", ["omp", "claude"])
def test_unrefused_control_probe_stops_before_any_reservation(
    tmp_path: Path, harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import factory_gate_protocol as protocol

    monkeypatch.setattr(protocol, "unsupported_probes", lambda _: [])

    def reserve(*_: object) -> str:
        pytest.fail("a worker was reserved after an unsupported control was accepted")

    monkeypatch.setattr(Campaign, "reserve", reserve)
    code = main(argv(tmp_path, harness))
    document = json.loads((tmp_path / f"{harness}.json").read_text())
    assert code == 3 and document["verdict"] == "hold"
    manifest = Campaign(parse_options(argv(tmp_path, harness))).read()
    assert manifest["slots"] == {}


def test_unsupported_probe_slot_delta_is_measured_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import factory_gate_protocol as protocol

    options = parse_options(argv(tmp_path, "omp"))
    probes = protocol.unsupported_probes

    def reserve_during_probe(harness: str) -> list[dict[str, str]]:
        Campaign(options).reserve(harness, "cancellation")
        return probes(harness)

    monkeypatch.setattr(protocol, "unsupported_probes", reserve_during_probe)
    assert main(argv(tmp_path, "omp")) == 3
    document = json.loads(options.evidence_out.read_text())
    assertion = next(row for row in document["assertions"] if row["id"] == "unsupported-controls")
    assert assertion["observer"]["new_slots"] == 1
    assert assertion["observer"]["container_creates"] is None
    assert assertion["status"] == "fail"


@pytest.mark.parametrize("guard", ["already-spent", "stale-raw", "missing-record", "metadata"])
def test_rerun_refuses_spent_or_stale_slot_before_reserving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guard: str
) -> None:
    from factory_gate_evidence import EvidenceBuilder
    from factory_gate_protocol import ProtocolGate, _slot_record
    from factory_gate_support import Scrubber

    gate = ProtocolGate(
        parse_options(argv(tmp_path, "omp")),
        EvidenceBuilder(scenario="protocol", mode="local", required=REQUIRED),
        Scrubber(),
        None,
    )
    key = gate.campaign.reserve("omp", "completed-workflow")
    gate.campaign.transition(key, "running")
    raw = tmp_path / "original.raw"
    raw.write_bytes(b"original retained output")
    result = {"raw_originals": {str(raw): sha256(raw)}}
    record = _slot_record(gate.options.raw_dir / (key.replace(":", "-") + ".json"), result)
    if guard == "already-spent":
        gate.campaign.transition(key, "interrupted")
        gate.campaign.finalize(
            key, "reconciled", record, reconciliation={"terminal_status": "failed"}
        )
    else:
        gate.campaign.finalize(key, "completed", record)
    if guard == "stale-raw":
        raw.write_bytes(b"tampered retained output")
    elif guard in {"missing-record", "metadata"}:
        with gate.campaign._locked() as manifest:
            if guard == "missing-record":
                del manifest["evidence"][key]
            else:
                manifest["evidence"][key]["workflow"]["edited"] = True

    def reserve(*_: object) -> str:
        pytest.fail("a spent/stale campaign slot was reserved")

    monkeypatch.setattr(Campaign, "reserve", reserve)
    assert main(argv(tmp_path, "omp")) == 3
    document = json.loads((tmp_path / "omp.json").read_text())
    expected = {
        "already-spent": "already spent",
        "missing-record": "already spent",
        "stale-raw": "stale",
        "metadata": "differs from its digest-bound receipt",
    }
    assert expected[guard] in document["reason"]


@pytest.mark.parametrize("recovered", [False, True], ids=["fresh", "reconciled"])
@pytest.mark.parametrize(
    "broken",
    [
        None,
        "cancel_status",
        "reconcile_status",
        "first_event",
        "cancel_issued",
        "container-count",
        "terminal-slot",
        "bound-evidence",
    ],
)
def test_run_checks_live_shaped_fresh_and_reconciled_results(
    tmp_path: Path,
    protocol_campaign: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    recovered: bool,
    broken: str | None,
) -> None:
    from dataclasses import replace

    import factory_gate_protocol as protocol
    import factory_gate_protocol_live as live
    from factory_gate_evidence import EvidenceBuilder
    from factory_gate_support import Scrubber

    root, _ = protocol_campaign
    original = Campaign(parse_options(argv(root, "omp"))).read()
    workflow = dict(original["evidence"]["omp:completed-workflow"]["workflow"])
    cancellation = dict(original["evidence"]["omp:cancellation"]["workflow"])
    cancellation.update(
        cancel_status="cancelled",
        reconcile_status="cancelled",
        cancel={"run_id": "live-run", "node_id": "implement", "status": "cancelled"},
        reconcile={"status": "cancelled", "product": {"next_action": "await_human_review"}},
        container_gone=True,
        first_event={"container": "observed-worker"},
        cancel_issued=True,
    )
    if broken in {"cancel_status", "reconcile_status"}:
        cancellation[broken] = "failed"
    gate = protocol.ProtocolGate(
        parse_options(argv(tmp_path, "omp")),
        EvidenceBuilder(scenario="protocol", mode="live", required=REQUIRED),
        Scrubber(),
        None,
    )
    gate.options = replace(gate.options, live=True)

    class Prepared:
        def completed_workflow(self) -> dict[str, Any]:
            gate.effects["isolated_containers"] += 1
            return workflow

        def cancellation(self, operation_id: str) -> dict[str, Any]:
            gate.effects["isolated_containers"] += 1
            gate.campaign.annotate(
                "omp:cancellation",
                first_event=None if broken == "first_event" else cancellation["first_event"],
                cancel_issued=broken != "cancel_issued",
            )
            return cancellation

    monkeypatch.setattr(live, "preflight", lambda *_: Prepared())

    def inspect(gate: Any, key: str, slot: dict[str, Any]) -> dict[str, Any]:
        result = workflow if key.endswith("completed-workflow") else cancellation
        return {
            "container_gone": True,
            "terminal_status": "completed" if key.endswith("completed-workflow") else "cancelled",
            "workflow": result,
        }

    monkeypatch.setattr(live, "reconcile_interrupted", inspect)
    if recovered:
        for role in ("completed-workflow", "cancellation"):
            key = gate.campaign.reserve("omp", role)
            gate.campaign.transition(key, "running")
            if role == "cancellation":
                gate.campaign.annotate(
                    key,
                    first_event=None if broken == "first_event" else cancellation["first_event"],
                    cancel_issued=broken != "cancel_issued",
                )
            gate.campaign.transition(key, "interrupted", owner_pid=999999999)
            protocol._reconcile(gate)
    original_slot_result = protocol._slot_result

    def result_with_budget_fault(gate: Any, role: str) -> dict[str, Any]:
        result = original_slot_result(gate, role)
        if role == "cancellation":
            if broken == "container-count":
                gate.effects["isolated_containers"] += 1
            elif broken in {"terminal-slot", "bound-evidence"}:
                with gate.campaign._locked() as manifest:
                    if broken == "terminal-slot":
                        manifest["slots"]["omp:cancellation"]["state"] = "failed"
                    else:
                        manifest["evidence"]["omp:cancellation"]["workflow"] = {}
        return result

    monkeypatch.setattr(protocol, "_slot_result", result_with_budget_fault)
    protocol.run(gate)
    assertion = gate.evidence._assertions["cancellation-reconcile"]
    cancellation_fault = broken in {
        "cancel_status",
        "reconcile_status",
        "first_event",
        "cancel_issued",
    }
    assert assertion["status"] == ("fail" if cancellation_fault else "pass")
    assert gate.evidence._assertions["campaign-budget"]["status"] == (
        "fail" if broken in {"container-count", "terminal-slot", "bound-evidence"} else "pass"
    )


@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
def test_live_preflight_hold_precedes_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    from dataclasses import replace

    import factory_gate_protocol as protocol
    import factory_gate_protocol_live as live
    from factory_gate_context import GateHoldError
    from factory_gate_evidence import EvidenceBuilder
    from factory_gate_support import Scrubber

    gate = protocol.ProtocolGate(
        parse_options(argv(tmp_path, "omp")),
        EvidenceBuilder(scenario="protocol", mode="live", required=REQUIRED),
        Scrubber(),
        None,
    )
    gate.options = replace(gate.options, live=True)

    def hold(*_: object) -> None:
        raise GateHoldError("qualification unavailable")

    def reserve(*_: object) -> str:
        pytest.fail("reservation happened before preflight completed")

    monkeypatch.setattr(live, "preflight", hold)
    monkeypatch.setattr(Campaign, "reserve", reserve)
    with pytest.raises(GateHoldError, match="qualification unavailable"):
        protocol._slot_result(gate, role)
    assert gate.campaign.read()["slots"] == {}


def test_shared_pr_snapshot_reads_every_page_and_rechecks_head() -> None:
    from factory_gate_host_reads import pr_snapshot

    calls: list[list[str]] = []

    def execute(arguments: list[str]) -> Any:
        calls.append(arguments)
        endpoint = arguments[-1]
        if "/files?" in endpoint:
            return [[{"filename": "second.py"}], [{"filename": "first.py"}]]
        if "/check-runs?" in endpoint:
            return [{"check_runs": [{"id": 1}]}, {"check_runs": [{"id": 2}]}]
        if "/reviews?" in endpoint:
            return [[{"id": 3}], [{"id": 4}]]
        return {"state": "open", "merged": False, "head": {"sha": "head"}, "base": {}}

    snapshot = pr_snapshot("trusted/repository", 7, execute)
    assert snapshot["changed_paths"] == ["first.py", "second.py"]
    assert snapshot["check_runs"] == [{"id": 1}, {"id": 2}]
    assert snapshot["reviews"] == [{"id": 3}, {"id": 4}]
    assert len(calls) == 5
    assert all("--paginate" in arguments and "--slurp" in arguments for arguments in calls[1:4])


@pytest.mark.parametrize(
    "changed", ["head.sha", "head.ref", "base.sha", "base.ref", "state", "merged"]
)
def test_shared_pr_snapshot_rejects_mid_snapshot_change(changed: str) -> None:
    from factory_gate_context import GateHoldError
    from factory_gate_host_reads import pr_snapshot

    reads = 0

    def execute(arguments: list[str]) -> Any:
        nonlocal reads
        if "?" in arguments[-1]:
            return []
        reads += 1
        pr = {
            "state": "open",
            "merged": False,
            "head": {"sha": "head", "ref": "topic"},
            "base": {"sha": "base", "ref": "main"},
        }
        if reads == 2:
            if "." in changed:
                side, field = changed.split(".")
                pr[side][field] = "changed"
            else:
                pr[changed] = True if changed == "merged" else "closed"
        return pr

    with pytest.raises(GateHoldError, match=r"changed during the fresh host snapshot"):
        pr_snapshot("trusted/repository", 7, execute)


@pytest.mark.parametrize(
    "shebang", ["#!/usr/bin/python3", "/usr/bin/python3", "#!/usr/bin/env python3"]
)
def test_shared_bound_interpreter_requires_direct_absolute_shebang(
    tmp_path: Path, shebang: str
) -> None:
    from factory_gate_context import GateHoldError
    from factory_gate_host_reads import bound_interpreter

    entry = tmp_path / "enginery"
    entry.write_text(shebang + "\n")
    if shebang == "#!/usr/bin/python3":
        assert bound_interpreter(entry) == "/usr/bin/python3"
    else:
        with pytest.raises(GateHoldError, match="absolute virtualenv"):
            bound_interpreter(entry)


@pytest.mark.parametrize("side", ["head", "base"])
@pytest.mark.parametrize(
    "field", ["pushed_at", "updated_at", "open_issues_count", "watchers_count"]
)
def test_shared_pr_snapshot_ignores_repository_activity(side: str, field: str) -> None:
    from factory_gate_host_reads import pr_snapshot

    reads = 0

    def execute(arguments: list[str]) -> Any:
        nonlocal reads
        if "?" in arguments[-1]:
            return []
        reads += 1
        pr = {
            "state": "open",
            "merged": False,
            "head": {"sha": "head", "ref": "topic", "repo": {field: 1}},
            "base": {"sha": "base", "ref": "main", "repo": {field: 1}},
        }
        if reads == 2:
            pr[side]["repo"][field] = 2
        return pr

    assert pr_snapshot("trusted/repository", 7, execute)["head"]["sha"] == "head"


@pytest.mark.parametrize(
    "error",
    [
        OSError("missing gh"),
        subprocess.CalledProcessError(1, ["gh"]),
        subprocess.TimeoutExpired(["gh"], 30),
        ValueError("invalid JSON"),
        KeyError("labels"),
        TypeError("invalid issue"),
    ],
)
def test_binding_failure_produces_hold_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    import factory_gate_protocol as protocol

    def unavailable(_: Any) -> Any:
        raise error

    monkeypatch.setattr(protocol, "campaign_binding", unavailable)
    assert main(argv(tmp_path, "omp")) == 3
    document = json.loads((tmp_path / "omp.json").read_text())
    assert document["verdict"] == "hold"
    assert "campaign binding unavailable" in document["reason"]
    assert Campaign(parse_options(argv(tmp_path, "omp"))).read()["slots"] == {}


@pytest.mark.parametrize("live_recovery", [False, True])
@pytest.mark.parametrize("failure", ["after-binding", "manifest-commit"])
@pytest.mark.parametrize("role", ["completed-workflow", "cancellation"])
def test_interrupted_finalization_recovers_prewritten_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    role: str,
    live_recovery: bool,
) -> None:
    from dataclasses import replace

    import factory_gate_protocol as protocol
    import factory_gate_protocol_live as live
    from factory_gate_evidence import EvidenceBuilder
    from factory_gate_support import Scrubber

    gate = protocol.ProtocolGate(
        parse_options(argv(tmp_path, "omp")),
        EvidenceBuilder(scenario="protocol", mode="local", required=REQUIRED),
        Scrubber(),
        None,
    )
    result = {"terminal_status": "cancelled" if role == "cancellation" else "completed"}
    monkeypatch.setattr(protocol, "_local_workflow", lambda *_: result)
    monkeypatch.setattr(protocol, "_local_cancellation", lambda *_: result)
    real_replace = os.replace
    armed = True

    real_annotate = gate.campaign.annotate

    def annotate(key: str, **observations: Any) -> None:
        nonlocal armed
        real_annotate(key, **observations)
        if failure == "after-binding" and "workflow_sha256" in observations and armed:
            armed = False
            raise OSError("injected after workflow binding")

    def replace_manifest(source: Any, target: Any) -> None:
        nonlocal armed
        payload = json.loads(Path(source).read_text())
        if (
            Path(target) == gate.campaign.path
            and failure == "manifest-commit"
            and payload["evidence"]
            and armed
        ):
            armed = False
            raise OSError("injected before terminal manifest replace")
        real_replace(source, target)

    monkeypatch.setattr(gate.campaign, "annotate", annotate)
    monkeypatch.setattr("factory_gate_campaign.os.replace", replace_manifest)
    with pytest.raises(OSError, match="injected"):
        protocol._slot_result(gate, role)
    key = f"omp:{role}"
    document = gate.campaign.read()
    assert document["slots"][key]["state"] == "interrupted"
    assert key not in document["evidence"]
    with gate.campaign._locked() as document:
        document["slots"][key]["owner_pid"] = 999999999
    monkeypatch.setattr(protocol, "_local_workflow", lambda *_: pytest.fail("must not relaunch"))
    monkeypatch.setattr(
        protocol, "_local_cancellation", lambda *_: pytest.fail("must not relaunch")
    )
    if live_recovery:
        gate.options = replace(gate.options, live=True)
        monkeypatch.setattr(
            live,
            "reconcile_interrupted",
            lambda *_: {
                "container_gone": True,
                "terminal_status": result["terminal_status"],
                "workflow": {"must_use_prewritten_workflow_instead": True},
            },
        )
    protocol._reconcile(gate)
    assert protocol._slot_result(gate, role) == result
    document = gate.campaign.read()
    assert document["slots"][key]["state"] == "reconciled"
    assert document["evidence"][key]["workflow"] == result


def test_workflow_write_is_atomic_on_partial_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import factory_gate_protocol as protocol

    path = tmp_path / "workflow.json"
    original = b'{"previous-campaign": true}\n'
    path.write_bytes(original)

    def interrupted(value: Any, stream: Any, **_: Any) -> None:
        stream.write('{"partial":')
        raise OSError("interrupted serialization")

    monkeypatch.setattr(protocol.json, "dump", interrupted)
    with pytest.raises(OSError, match="interrupted serialization"):
        protocol._write_json(path, {"current": True})
    assert path.read_bytes() == original
    assert sorted(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("damage", ["truncated", "stale", "stale-before-write", "missing"])
def test_recovery_rejects_unbound_or_damaged_prewritten_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    import factory_gate_protocol as protocol
    from factory_gate_context import GateHoldError
    from factory_gate_evidence import EvidenceBuilder
    from factory_gate_support import Scrubber

    gate = protocol.ProtocolGate(
        parse_options(argv(tmp_path, "omp")),
        EvidenceBuilder(scenario="protocol", mode="local", required=REQUIRED),
        Scrubber(),
        None,
    )
    key = "omp:completed-workflow"
    path = gate.options.raw_dir / "omp-completed-workflow.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = b'{"earlier_campaign": true}\n'
    path.write_bytes(stale)
    monkeypatch.setattr(protocol, "_local_workflow", lambda *_: {"current_campaign": True})

    def stop(*_: Any) -> None:
        raise OSError("interrupted before finalization")

    if damage == "stale-before-write":
        monkeypatch.setattr(protocol, "_write_json", stop)
    else:
        monkeypatch.setattr(gate.campaign, "finalize", stop)
    with pytest.raises(OSError, match="interrupted before finalization"):
        protocol._slot_result(gate, "completed-workflow")
    if damage == "truncated":
        path.write_bytes(b'{"partial":')
    elif damage == "stale":
        path.write_bytes(stale)
    elif damage == "missing":
        path.unlink()
    monkeypatch.undo()
    with gate.campaign._locked() as document:
        document["slots"][key]["owner_pid"] = 999999999
    protocol._reconcile(gate)
    assert key not in gate.campaign.read()["evidence"]
    assert gate.campaign.read()["slots"][key]["state"] == "reconciled"
    with pytest.raises(GateHoldError, match="already spent"):
        protocol._slot_result(gate, "completed-workflow")


@pytest.mark.parametrize(
    ("role", "bound_terminal", "recovered_terminal"),
    [
        ("completed-workflow", "completed", "failed"),
        ("cancellation", "failed", "cancelled"),
    ],
)
def test_live_recovery_rejects_failed_or_mismatching_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    bound_terminal: str,
    recovered_terminal: str,
) -> None:
    from dataclasses import replace

    import factory_gate_protocol as protocol
    import factory_gate_protocol_live as live
    from factory_gate_evidence import EvidenceBuilder
    from factory_gate_support import Scrubber

    gate = protocol.ProtocolGate(
        parse_options(argv(tmp_path, "omp")),
        EvidenceBuilder(scenario="protocol", mode="local", required=REQUIRED),
        Scrubber(),
        None,
    )
    key = f"omp:{role}"
    gate.campaign.reserve("omp", role)
    gate.campaign.transition(key, "running", owner_pid=999999999)
    path = gate.options.raw_dir / (key.replace(":", "-") + ".json")
    record = protocol._slot_record(path, {"prewritten": True})
    gate.campaign.annotate(
        key,
        workflow_path=record["path"],
        workflow_sha256=record["sha256"],
        workflow_terminal=bound_terminal,
    )
    gate.campaign.transition(key, "interrupted")
    gate.options = replace(gate.options, live=True)
    monkeypatch.setattr(
        live,
        "reconcile_interrupted",
        lambda *_: {"container_gone": True, "terminal_status": recovered_terminal},
    )
    protocol._reconcile(gate)
    document = gate.campaign.read()
    assert key not in document["evidence"]
    assert document["slots"][key]["reconciliation"]["terminal_status"] == recovered_terminal
