"""Campaign reservations are durable, serial, immutable and never retried."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata as metadata
import io
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from factory_gate_campaign import (
    Campaign,
    campaign_binding,
    configuration,
    issue_binding,
    sha256,
    stable_host_identity,
)
from factory_gate_context import GateHoldError, GateOptions
from factory_gate_evidence import IDENTITY_KEYS, EvidenceBuilder
from factory_gate_identity_probe import installed_identity, verify_records


def options(root: Path) -> GateOptions:
    return GateOptions(
        scenario="protocol",
        evidence_out=root / "omp.json",
        harness="omp",
        campaign_id="campaign-test",
    )


def campaign(root: Path) -> Campaign:
    opts = options(root)
    result = Campaign(opts)
    result.bind(campaign_binding(opts))
    return result


def _append_reuse(root: str, number: int) -> None:
    Campaign(options(Path(root))).record_reuse([number], [])


def test_reservation_budget_and_concurrency(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    first = manifest.reserve("omp", "completed-workflow")
    with pytest.raises(GateHoldError, match="concurrency"):
        manifest.reserve("claude", "completed-workflow")
    manifest.transition(first, "running")
    with pytest.raises(GateHoldError, match="concurrency"):
        manifest.reserve("claude", "completed-workflow")
    manifest.transition(first, "completed")
    with pytest.raises(GateHoldError, match="used"):
        manifest.reserve("omp", "completed-workflow")
    for harness, role in (
        ("omp", "cancellation"),
        ("claude", "completed-workflow"),
        ("claude", "cancellation"),
    ):
        key = manifest.reserve(harness, role)
        manifest.transition(key, "running")
        manifest.transition(key, "cancelled" if role == "cancellation" else "completed")
    with pytest.raises(GateHoldError, match="budget"):
        manifest.reserve("omp", "fifth")
    assert len(manifest.read()["slots"]) == 4


def test_binding_mismatch_precedes_reservation(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    binding = campaign_binding(options(tmp_path))
    binding["catalog_digest"] = "wrong"
    with pytest.raises(GateHoldError, match="catalog_digest"):
        manifest.bind(binding)
    assert manifest.read()["slots"] == {}


@pytest.mark.parametrize("target", ["synthetic", "real"])
def test_qualification_target_accepts_exact_classes(tmp_path: Path, target: str) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"qualification_target": target}))
    opts = replace(options(tmp_path), campaign_config=config)
    assert configuration(opts)["qualification_target"] == target


@pytest.mark.parametrize("target", [None, "", "Synthetic", "other", 1, [], {}])
@pytest.mark.parametrize("live", [False, True])
def test_invalid_qualification_target_precedes_binding(
    tmp_path: Path, target: Any, live: bool
) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({} if target is None else {"qualification_target": target}))
    opts = replace(options(tmp_path), campaign_config=config, live=live)
    manifest = Campaign(opts)
    with pytest.raises(GateHoldError, match="qualification_target"):
        manifest.bind(campaign_binding(opts))
    assert manifest.read()["binding"] is None
    assert manifest.read()["slots"] == {}


def test_qualification_target_is_immutable(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    binding = campaign_binding(options(tmp_path))
    assert binding["qualification_target"] == "synthetic"
    binding["qualification_target"] = "real"
    with pytest.raises(GateHoldError, match="qualification_target"):
        manifest.bind(binding)
    assert manifest.read()["slots"] == {}


def test_dead_invocation_is_reconciled_never_relaunched(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.transition(key, "running", owner_pid=999999999)
    observed = []
    assert manifest.reconcile_dead(
        lambda k, s: (
            observed.append((k, s["operation_id"]))
            or {"container_gone": True, "result": "interrupted"}
        )
    ) == [key]
    assert len(observed) == 1
    assert manifest.read()["slots"][key]["history"] == [
        "reserved",
        "running",
        "interrupted",
        "reconciled",
    ]
    with pytest.raises(GateHoldError, match="used"):
        manifest.reserve("omp", "completed-workflow")


def test_live_invocation_cannot_be_reconciled(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    manifest.reserve("omp", "completed-workflow")
    with pytest.raises(GateHoldError, match="still alive"):
        manifest.reconcile_dead(lambda *_: pytest.fail("must not inspect live invocation"))


def test_atomic_concurrent_writers_preserve_all_updates(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    context = multiprocessing.get_context("fork")
    children = [context.Process(target=_append_reuse, args=(str(tmp_path), i)) for i in range(8)]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=20)
        assert child.exitcode == 0
    assert sorted(row["records"][0] for row in manifest.read()["reuse"]) == list(range(8))
    assert not list(manifest.path.parent.glob("manifest-*.tmp"))


def test_failed_replace_does_not_corrupt_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = campaign(tmp_path)
    original = manifest.path.read_bytes()

    def fail_replace(*_: object) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        manifest.record_reuse(["lost"], [])
    assert manifest.path.read_bytes() == original
    assert not list(manifest.path.parent.glob("manifest-*.tmp"))


def test_retained_artifact_digest_detects_byte_drift(tmp_path: Path) -> None:
    from enginery.ledger.artifact_store import ArtifactStore
    from factory_gate_protocol import retain_artifacts

    store = ArtifactStore(tmp_path / "artifacts")
    digest = store.publish_bytes(b"\xff\x00original bytes")
    retained = retain_artifacts(tmp_path / "artifacts", tmp_path / "raw")
    path = tmp_path / "raw" / f"{digest.hex_value}.bin"
    assert path.read_bytes() == store.read_bytes(digest)
    assert retained["raw_originals"][str(path.resolve())] == sha256(path)
    path.write_bytes(path.read_bytes() + b"!")
    assert retained["raw_originals"][str(path.resolve())] != sha256(path)


@pytest.mark.parametrize("identifier", ["aa", "../escape", "A-campaign", "a" * 64])
def test_invalid_campaign_identifiers_refused(tmp_path: Path, identifier: str) -> None:
    with pytest.raises(GateHoldError):
        Campaign(replace(options(tmp_path), campaign_id=identifier))


def test_schema_mismatch_fails_closed(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    document = json.loads(manifest.path.read_text())
    document["schema_version"] = 1
    manifest.path.write_text(json.dumps(document))
    with pytest.raises(GateHoldError, match="unsupported"):
        manifest.read()


def test_product_identity_is_durable_before_launch_and_terminally_immutable(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.annotate(key, operation_id="implement:fr-bound", run_id="fr-bound")
    assert manifest.read()["slots"][key]["operation_id"] == "implement:fr-bound"
    with pytest.raises(GateHoldError, match="ownership"):
        manifest.annotate(key, state="completed")
    manifest.transition(key, "running")
    manifest.transition(key, "completed")
    with pytest.raises(GateHoldError, match="terminal"):
        manifest.annotate(key, operation_id="different-worker")


@pytest.mark.parametrize("payload", ["{", "[]", '{"schema_version": 1}'])
def test_corrupt_manifest_holds_without_overwrite(tmp_path: Path, payload: str) -> None:
    manifest = campaign(tmp_path)
    manifest.path.write_text(payload)
    with pytest.raises(GateHoldError):
        manifest.read()
    assert manifest.path.read_text() == payload


def test_invalid_slot_on_empty_campaign_is_refused(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    with pytest.raises(GateHoldError, match="invalid slot"):
        manifest.reserve("omp", "fifth")
    assert manifest.read()["slots"] == {}


def test_four_slot_count_guard_refuses_corrupt_manifest(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.transition(key, "running")
    manifest.transition(key, "completed")
    document = manifest.read()
    for number in range(3):
        document["slots"][f"stray-{number}"] = {"state": "completed"}
    manifest.path.write_text(json.dumps(document))
    before = manifest.path.read_bytes()
    with pytest.raises(GateHoldError, match="budget"):
        manifest.reserve("claude", "cancellation")
    assert manifest.path.read_bytes() == before


def test_remaining_container_holds_without_reconciling(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.transition(key, "running", owner_pid=999999999)
    inspected = []

    def inspect(slot_key: str, slot: dict[str, Any]) -> dict[str, Any]:
        inspected.append((slot_key, slot["state"]))
        return {"container_gone": False}

    with pytest.raises(GateHoldError, match="worker remains"):
        manifest.reconcile_dead(inspect)
    assert inspected == [(key, "interrupted")]
    slot = manifest.read()["slots"][key]
    assert slot["state"] == "interrupted"
    assert slot["history"] == ["reserved", "running", "interrupted"]
    assert key not in manifest.read()["evidence"]


def test_host_binding_ignores_boot_os_and_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = {
        "hostname": "qualification-host",
        "machine_id": "machine-1",
        "arch": "arm64",
        "boot_id": "boot-1",
        "os": "old OS",
        "python": "3.13",
    }
    monkeypatch.setattr("factory_gate_campaign.host_identity", lambda: identity)
    manifest = campaign(tmp_path)
    binding = campaign_binding(options(tmp_path))
    binding["host"] = stable_host_identity()
    manifest_with_host = Campaign(replace(options(tmp_path), campaign_id="stable-host"))
    manifest_with_host.bind(binding)
    identity.update(boot_id="boot-2", os="new OS", python="3.14", hostname="different-network-host")
    binding["host"] = stable_host_identity()
    assert binding["host"] == {
        "machine_id": "machine-1",
        "arch": "arm64",
    }
    manifest_with_host.bind(binding)
    identity["machine_id"] = "different-machine"
    binding["host"] = stable_host_identity()
    with pytest.raises(GateHoldError, match="host"):
        manifest_with_host.bind(binding)
    assert manifest.read()["slots"] == {}


def test_reboot_reconciliation_never_probes_reused_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = {"boot_id": "old-boot", "machine_id": "machine-1", "arch": "arm64"}
    monkeypatch.setattr("factory_gate_campaign.host_identity", lambda: identity)
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.transition(key, "running")
    assert manifest.read()["slots"][key]["owner_boot_id"] == "old-boot"
    identity["boot_id"] = "new-boot"

    def forbidden_kill(*_: object) -> None:
        pytest.fail("a PID from another boot must never be probed")

    monkeypatch.setattr(os, "kill", forbidden_kill)
    assert manifest.reconcile_dead(lambda *_: {"container_gone": True}) == [key]
    assert manifest.read()["slots"][key]["state"] == "reconciled"


def test_unknown_boot_holds_without_probing_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = campaign(tmp_path)
    manifest.reserve("omp", "completed-workflow")
    machine_id = manifest.read()["slots"]["omp:completed-workflow"]["owner_machine_id"]
    monkeypatch.setattr(
        "factory_gate_campaign.host_identity",
        lambda: {"machine_id": machine_id, "arch": "arm64"},
    )
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("unknown boot must hold"))
    with pytest.raises(GateHoldError, match="boot identity"):
        manifest.reconcile_dead(lambda *_: pytest.fail("must not inspect without boot identity"))


class InstalledWheel(metadata.Distribution):
    def __init__(self, root: Path, metadata_dir: str) -> None:
        self.root = root
        self.metadata_dir = metadata_dir

    def read_text(self, filename: str) -> str | None:
        path = self.root / self.metadata_dir / filename
        return path.read_text() if path.exists() else None

    def locate_file(self, path: str | os.PathLike[str]) -> Path:
        return self.root / path


def _installed_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, InstalledWheel]:
    prefix = tmp_path / "venv"
    root = prefix / "lib" / "python3.13" / "site-packages"
    monkeypatch.setattr(sys, "prefix", str(prefix))
    metadata_dir = "enginery-0.5.0.dist-info"
    payloads = {
        "enginery/__init__.py": b"VALUE = 1\ndef main():\n    return 0\n",
        f"{metadata_dir}/METADATA": b"Metadata-Version: 2.1\nName: enginery\nVersion: 0.5.0\n",
        f"{metadata_dir}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{metadata_dir}/entry_points.txt": (b"[console_scripts]\nenginery = enginery:main\n"),
    }
    record_name = f"{metadata_dir}/RECORD"
    stream = io.StringIO()
    writer = csv.writer(stream)
    for name, payload in payloads.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        writer.writerow((name, f"sha256={digest.decode()}", len(payload)))
    writer.writerow((record_name, "", ""))
    payloads[record_name] = stream.getvalue().encode()
    wheel = tmp_path / "enginery-0.5.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
    direct_url = root / metadata_dir / "direct_url.json"
    direct_url.write_text(json.dumps({"url": wheel.as_uri(), "archive_info": {}}))
    generated = {
        f"{metadata_dir}/direct_url.json": direct_url.read_bytes(),
        f"{metadata_dir}/INSTALLER": b"uv\n",
        f"{metadata_dir}/REQUESTED": b"",
        f"{metadata_dir}/uv_cache.json": b'{"timestamp": null}\n',
        "../../../bin/enginery": b"#!/venv/bin/python\n",
    }
    with (root / record_name).open("a") as installed_record:
        writer = csv.writer(installed_record)
        for name, payload in generated.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
            writer.writerow((name, f"sha256={digest.decode()}", len(payload)))
    return wheel, InstalledWheel(root, metadata_dir)


def test_uv_hashless_archive_identity_verified_by_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel, distribution = _installed_wheel(tmp_path, monkeypatch)
    monkeypatch.setattr(metadata, "distribution", lambda _: distribution)
    identity = installed_identity(wheel)
    assert identity["version"] == "0.5.0"
    assert identity["direct_url"]["archive_info"] == {}
    assert identity["wheel_record_sha256"] == verify_records(wheel, distribution)


def test_real_uv_installed_wheel_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    wheel, _ = _installed_wheel(tmp_path, monkeypatch)
    venv = tmp_path / "real-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin" / "python"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        check=True,
    )
    probe = Path(__file__).resolve().parents[2] / "scripts" / "factory_gate_identity_probe.py"
    result = subprocess.run(
        [str(python), "-I", str(probe), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    identity = json.loads(result.stdout)
    assert identity["version"] == "0.5.0"
    assert identity["prefix"] == str(venv)
    assert identity["wheel_record_sha256"]


@pytest.mark.parametrize("damage", ["payload", "record", "extra", "duplicate", "symlink"])
def test_record_identity_rejects_installed_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    wheel, distribution = _installed_wheel(tmp_path, monkeypatch)
    record = distribution.root / distribution.metadata_dir / "RECORD"
    source = distribution.root / "enginery" / "__init__.py"
    if damage == "payload":
        source.write_bytes(b"VALUE = 2\n")
    elif damage == "record":
        rows = list(csv.reader(io.StringIO(record.read_text())))
        rows[0][1] = "sha256=unrelated"
        with record.open("w") as stream:
            csv.writer(stream).writerows(rows)
    elif damage == "extra":
        with record.open("a") as stream:
            csv.writer(stream).writerow(("enginery/injected.py", "", ""))
    elif damage == "duplicate":
        with record.open("a") as stream:
            csv.writer(stream).writerow(("enginery/__init__.py", "", ""))
    else:
        outside = tmp_path / "outside.py"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
    with pytest.raises(ValueError):
        verify_records(wheel, distribution)


def test_record_identity_rejects_unsafe_wheel_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel, distribution = _installed_wheel(tmp_path, monkeypatch)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../escape.py", b"untrusted")
    with pytest.raises(ValueError, match="unsafe"):
        verify_records(wheel, distribution)


def test_record_identity_rejects_unverified_wheel_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel, distribution = _installed_wheel(tmp_path, monkeypatch)
    with zipfile.ZipFile(wheel) as archive:
        payloads = {name: archive.read(name) for name in archive.namelist()}
    payloads["enginery/__init__.py"] = b"VALUE = 2\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    with pytest.raises(ValueError, match="payload does not match RECORD"):
        verify_records(wheel, distribution)


def test_installed_archive_hash_mismatch_is_not_bypassed_by_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel, distribution = _installed_wheel(tmp_path, monkeypatch)
    direct_url = distribution.root / distribution.metadata_dir / "direct_url.json"
    direct_url.write_text(json.dumps({"archive_info": {"hashes": {"sha256": "wrong"}}}))
    monkeypatch.setattr(metadata, "distribution", lambda _: distribution)
    with pytest.raises(ValueError, match="archive hash differs"):
        installed_identity(wheel)


def _successful_slot_evidence(manifest: Campaign, root: Path) -> None:
    for harness in ("omp", "claude"):
        for role in ("completed-workflow", "cancellation"):
            key = manifest.reserve(harness, role)
            manifest.transition(key, "running")
            path = root / f"{harness}-{role}.json"
            path.write_text(json.dumps({"slot": key}))
            manifest.finalize(
                key,
                "completed" if role == "completed-workflow" else "cancelled",
                {"path": str(path), "sha256": sha256(path)},
            )


def _protocol_document(root: Path, harness: str, *, passed: bool = True) -> Path:
    builder = EvidenceBuilder(scenario="protocol", mode="local", required={"check": "required"})
    builder.assertion(
        "check",
        passed=passed,
        commands=["observed command"],
        observed={"outcome": passed},
        expected="required",
        observer={"result": passed},
    )
    identities: dict[str, Any] = dict.fromkeys(IDENTITY_KEYS)
    identities["config"] = {"campaign_id": "campaign-test", "harness": harness}
    document = builder.build(
        identities=identities,
        effects={"hosted_inference_calls": 0},
        raw_artifacts_dir=None,
    )
    path = root / f"protocol-{harness}.json"
    path.write_text(json.dumps(document))
    return path


def test_successful_slots_without_protocol_verdicts_cannot_pass(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    _successful_slot_evidence(manifest, tmp_path)
    assert manifest.verify_evidence() == [
        "omp: missing passing protocol evidence",
        "claude: missing passing protocol evidence",
    ]
    manifest.record_protocol_evidence("omp", _protocol_document(tmp_path, "omp"))
    assert manifest.verify_evidence() == ["claude: missing passing protocol evidence"]
    manifest.record_protocol_evidence("claude", _protocol_document(tmp_path, "claude"))
    assert manifest.verify_evidence() == []
    assert manifest.read()["schema_version"] == 2


def test_protocol_evidence_rerun_replaces_latest_digest_and_verdict(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    _successful_slot_evidence(manifest, tmp_path)
    for harness in ("omp", "claude"):
        manifest.record_protocol_evidence(harness, _protocol_document(tmp_path, harness))
    original = manifest.read()["protocol_evidence"]["omp"]
    path = _protocol_document(tmp_path, "omp", passed=False)
    assert manifest.verify_evidence() == ["omp: missing or stale protocol evidence digest"]
    manifest.record_protocol_evidence("omp", path)
    assert manifest.verify_evidence() == ["omp: missing passing protocol evidence"]
    failed = manifest.read()["protocol_evidence"]["omp"]
    assert failed == {"path": str(path), "sha256": sha256(path), "verdict": "fail"}
    assert failed["sha256"] != original["sha256"]
    path = _protocol_document(tmp_path, "omp")
    manifest.record_protocol_evidence("omp", path)
    before = manifest.read()["protocol_evidence"]
    manifest.record_protocol_evidence("omp", path)
    assert manifest.read()["protocol_evidence"] == before
    assert manifest.verify_evidence() == []


@pytest.mark.parametrize("damage", ["missing", "malformed", "wrong-harness", "wrong-campaign"])
def test_invalid_protocol_rerun_invalidates_prior_pass(tmp_path: Path, damage: str) -> None:
    manifest = campaign(tmp_path)
    _successful_slot_evidence(manifest, tmp_path)
    for harness in ("omp", "claude"):
        manifest.record_protocol_evidence(harness, _protocol_document(tmp_path, harness))
    path = tmp_path / "protocol-omp.json"
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_text("{")
    else:
        document = json.loads(path.read_text())
        key = "harness" if damage == "wrong-harness" else "campaign_id"
        document["identities"]["config"][key] = "other"
        path.write_text(json.dumps(document))
    with pytest.raises(GateHoldError, match="protocol evidence"):
        manifest.record_protocol_evidence("omp", path)
    assert manifest.read()["protocol_evidence"]["omp"]["verdict"] == "invalid"
    assert manifest.verify_evidence() == ["omp: missing passing protocol evidence"]


@pytest.mark.parametrize("verdict", ["hold", "fail"])
def test_preparation_failure_without_identities_replaces_protocol_pass(
    tmp_path: Path, verdict: str
) -> None:
    manifest = campaign(tmp_path)
    manifest.record_protocol_evidence("omp", _protocol_document(tmp_path, "omp"))
    path = tmp_path / "protocol-omp.json"
    path.write_text(json.dumps({"schema_version": 1, "scenario": "protocol", "verdict": verdict}))
    manifest.record_protocol_evidence("omp", path)
    record = manifest.read()["protocol_evidence"]["omp"]
    assert record == {"path": str(path), "sha256": sha256(path), "verdict": verdict}


def test_protocol_verdict_cannot_pass_without_assertions(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    path = _protocol_document(tmp_path, "omp")
    document = json.loads(path.read_text())
    document["assertions"] = []
    path.write_text(json.dumps(document))
    with pytest.raises(GateHoldError, match="protocol pass"):
        manifest.record_protocol_evidence("omp", path)
    assert manifest.read()["protocol_evidence"]["omp"]["verdict"] == "invalid"


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_protocol_evidence_binds_only_latest_exact_path(tmp_path: Path, damage: str) -> None:
    manifest = campaign(tmp_path)
    _successful_slot_evidence(manifest, tmp_path)
    for harness in ("omp", "claude"):
        manifest.record_protocol_evidence(harness, _protocol_document(tmp_path, harness))
    original = tmp_path / "protocol-omp.json"
    latest = tmp_path / "rerun-omp.json"
    latest.write_bytes(original.read_bytes())
    manifest.record_protocol_evidence("omp", latest)
    assert manifest.read()["protocol_evidence"]["omp"]["path"] == str(latest)
    original.write_text("obsolete evidence")
    assert manifest.verify_evidence() == []
    if damage == "changed":
        latest.write_text("overwritten after finalization")
    else:
        latest.unlink()
    assert manifest.verify_evidence() == ["omp: missing or stale protocol evidence digest"]


def test_live_binding_preserves_real_qualification_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import factory_gate_auth as auth
    import factory_gate_campaign as campaign_module

    prefix = tmp_path / "installed"
    wheel = tmp_path / "enginery.whl"
    wheel.write_bytes(b"bound wheel")
    custody = tmp_path / "custody.json"
    custody.write_text("{}")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "qualification_target": "real",
                "wheel": str(wheel),
                "executable": str(prefix / "bin" / "enginery"),
                "project_path": str(tmp_path / "project"),
                "repository": "owner/repo",
                "issues": {"omp": 17, "claude": 18},
                "designated_workflow": "omp",
                "engine_kind": "docker",
                "validation_commands": ["python3 -m unittest"],
            }
        )
    )
    opts = replace(
        options(tmp_path),
        live=True,
        campaign_config=config,
        gateway_url="http://127.0.0.1:9999",
        gateway_token_file=tmp_path / "token",
        custody_receipt=custody,
    )
    monkeypatch.setattr(
        auth,
        "load_live_custody",
        lambda *args, **kwargs: SimpleNamespace(host=SimpleNamespace(value="qualification-host")),
    )
    monkeypatch.setattr(campaign_module, "bound_interpreter", lambda _: "/bound/python")
    monkeypatch.setattr(
        campaign_module, "load_execution_catalog", lambda: SimpleNamespace(digest="catalog")
    )
    monkeypatch.setattr(campaign_module, "stable_host_identity", lambda: {"host": "fixture"})

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if argv[0] == "/bound/python":
            stdout = json.dumps({"prefix": str(prefix), "base_prefix": "/system"})
        else:
            assert argv[:2] == ["gh", "api"]
            stdout = _issue_snapshot(number=int(argv[2].rsplit("/", 1)[1]))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    monkeypatch.setattr(campaign_module.subprocess, "run", run)
    binding = campaign_binding(opts)
    assert binding["qualification_target"] == "real"
    manifest = Campaign(opts)
    manifest.bind(binding)
    assert manifest.read()["binding"]["qualification_target"] == "real"


def _issue_snapshot(**changes: Any) -> bytes:
    issue = {
        "number": 17,
        "title": "Implement task",
        "body": "Acceptance criteria",
        "state": "open",
        "labels": [{"name": "factory", "color": "ffffff", "id": 1}],
        "updated_at": "2026-01-01",
        "comments": 0,
    }
    return json.dumps({**issue, **changes}).encode()


def test_issue_binding_ignores_volatile_fields(tmp_path: Path) -> None:
    binding = campaign_binding(options(tmp_path))
    binding["issues"]["omp"] = issue_binding(_issue_snapshot())
    manifest = Campaign(options(tmp_path))
    manifest.bind(binding)
    binding["issues"]["omp"] = issue_binding(
        _issue_snapshot(
            updated_at="2026-10-18",
            comments=37,
            labels=[{"name": "factory", "color": "000000", "id": 999}],
            reactions={"+1": 12},
        )
    )
    manifest.bind(binding)
    selected = {
        "number": 17,
        "title": "Implement task",
        "body": "Acceptance criteria",
        "state": "open",
        "labels": ["factory"],
    }
    expected = hashlib.sha256(
        json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert binding["issues"]["omp"] == {**selected, "snapshot_sha256": expected}
    assert manifest.read()["slots"] == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("number", 18),
        ("title", "Different task"),
        ("body", "Changed acceptance"),
        ("state", "closed"),
        ("labels", [{"name": "different-task-label"}]),
    ],
)
def test_issue_binding_rejects_changed_task_fields(tmp_path: Path, field: str, value: Any) -> None:
    binding = campaign_binding(options(tmp_path))
    binding["issues"]["omp"] = issue_binding(_issue_snapshot())
    manifest = Campaign(options(tmp_path))
    manifest.bind(binding)
    binding["issues"]["omp"] = issue_binding(_issue_snapshot(**{field: value}))
    with pytest.raises(GateHoldError, match="issues"):
        manifest.bind(binding)
    assert manifest.read()["slots"] == {}


def test_reconciliation_uses_machine_identity_not_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = {
        "machine_id": "stable-machine",
        "arch": "arm64",
        "hostname": "old-network",
        "boot_id": "same-boot",
    }
    monkeypatch.setattr("factory_gate_campaign.host_identity", lambda: identity)
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    assert manifest.read()["slots"][key]["owner_machine_id"] == "stable-machine"
    identity["hostname"] = "new-network"

    def dead(*_: object) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", dead)
    assert manifest.reconcile_dead(lambda *_: {"container_gone": True}) == [key]


def test_reconciliation_refuses_different_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = {"machine_id": "first-machine", "arch": "arm64", "boot_id": "same-boot"}
    monkeypatch.setattr("factory_gate_campaign.host_identity", lambda: identity)
    manifest = campaign(tmp_path)
    manifest.reserve("omp", "completed-workflow")
    identity["machine_id"] = "different-machine"
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("foreign PID must not be probed"))
    with pytest.raises(GateHoldError, match="another host"):
        manifest.reconcile_dead(lambda *_: pytest.fail("foreign worker must not be inspected"))


@pytest.mark.parametrize("outcome", ["reconciled", "inspect-error", "worker-remains"])
def test_reconciliation_releases_manifest_lock_and_retains_interruption(
    tmp_path: Path, outcome: str
) -> None:
    import fcntl

    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.transition(key, "running", owner_pid=999999999)

    def inspect(observed_key: str, slot: Any) -> dict[str, Any]:
        with manifest.path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
        assert observed_key == key
        assert json.loads(manifest.path.read_text())["slots"][key]["state"] == "interrupted"
        slot["history"].append("mutated-copy")
        with pytest.raises(GateHoldError, match="concurrency"):
            manifest.reserve("claude", "completed-workflow")
        if outcome == "inspect-error":
            raise OSError("inspection unavailable")
        return {"container_gone": outcome == "reconciled"}

    if outcome == "inspect-error":
        with pytest.raises(OSError, match="inspection unavailable"):
            manifest.reconcile_dead(inspect)
    elif outcome == "worker-remains":
        with pytest.raises(GateHoldError, match="worker remains"):
            manifest.reconcile_dead(inspect)
    else:
        assert manifest.reconcile_dead(inspect) == [key]
    slot = manifest.read()["slots"][key]
    assert slot["state"] == ("reconciled" if outcome == "reconciled" else "interrupted")
    assert "mutated-copy" not in slot["history"]


def test_reconciliation_rechecks_interrupted_state_before_commit(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.transition(key, "running", owner_pid=999999999)

    def inspect(*_: Any) -> dict[str, Any]:
        manifest.transition(key, "reconciled", reconciliation={"winner": True})
        return {"container_gone": True}

    with pytest.raises(GateHoldError, match="invalid campaign finalization"):
        manifest.reconcile_dead(inspect)
    assert manifest.read()["slots"][key]["reconciliation"] == {"winner": True}


def test_finalization_commits_terminal_and_evidence_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.transition(key, "running")

    def fail(*_: Any) -> None:
        raise OSError("replace unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        with pytest.raises(OSError, match="replace unavailable"):
            manifest.finalize(key, "completed", {"workflow": {"result": "complete"}})
    document = manifest.read()
    assert document["slots"][key]["state"] == "running"
    assert key not in document["evidence"]
    manifest.finalize(key, "completed", {"workflow": {"result": "complete"}})
    document = manifest.read()
    assert document["slots"][key]["state"] == "completed"
    assert document["evidence"][key]["workflow"] == {"result": "complete"}


def test_concurrent_reconciliation_is_refused_without_inspection(tmp_path: Path) -> None:
    manifest = campaign(tmp_path)
    key = manifest.reserve("omp", "completed-workflow")
    manifest.transition(key, "running", owner_pid=999999999)
    inspected = []

    def inspect(observed_key: str, _: Any) -> dict[str, Any]:
        inspected.append(observed_key)
        competitor = Campaign(options(tmp_path))
        with pytest.raises(GateHoldError, match="reconciliation is still alive"):
            competitor.reconcile_dead(lambda *_: pytest.fail("duplicate recovery inspected"))
        with pytest.raises(GateHoldError, match="concurrency"):
            competitor.reserve("claude", "completed-workflow")
        return {"container_gone": True}

    assert manifest.reconcile_dead(inspect) == [key]
    assert inspected == [key]
    assert manifest.reconcile_dead(lambda *_: pytest.fail("terminal slot inspected")) == []
