"""Semantic acceptance negatives and real, protected deliberate-fault execution."""

from __future__ import annotations

import socket
import struct
import subprocess
import sys
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enginery.adapters.checkout_identity import host_environment, verified_build
from enginery.application.behavioral_calibration import (
    CALIBRATION_DRIVER,
    AcceptanceInputs,
    CalibrationOutcome,
    CalibrationReceipt,
)
from enginery.application.behavioral_checks import (
    EVALUATOR_VERSION,
    BehavioralAssertion,
    BehavioralCheck,
    FaultClass,
)
from enginery.application.behavioral_observation import ObservationData
from enginery.application.git_environment import repository_isolated_environment
from enginery.domain.behavioral_evidence import (
    BuildIdentity,
    BuildKind,
    CaptureArtifact,
    CaptureManifest,
    EnvironmentFingerprint,
    EvaluatorIdentity,
    EvidenceReceipt,
    ObserverIdentity,
    ObserverKind,
    ReceiptCategory,
    ToolVersion,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import StaleEvidenceError
from enginery.domain.node_attempt import EvidenceResult
from enginery.evidence.behavioral import (
    ObservedCheck,
    canonical_bytes,
    classify_calibration,
    evaluate_behavioral,
    observation_payload,
)
from enginery.evidence.media import validate_media
from enginery.ledger.artifact_store import ArtifactStore
from enginery.workflows.behavioral_acceptance import (
    AcceptanceRun,
    BehavioralAcceptanceService,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _check() -> BehavioralCheck:
    return BehavioralCheck(
        name="changed",
        observer="command",
        argv=(sys.executable, "-c", "print('changed')"),
        assertions=(BehavioralAssertion("stdout", "contains", "changed"),),
        fault_classes=("constant_result",),
    )


def _inputs(checks: tuple[BehavioralCheck, ...]) -> AcceptanceInputs:
    return AcceptanceInputs(
        source_revision="a" * 40,
        base_revision="b" * 40,
        head_revision="c" * 40,
        build=BuildIdentity(BuildKind.SOURCE_TREE, Digest.of_bytes(b"current-build")),
        configuration_snapshot_digest=Digest.of_bytes(b"config"),
        evaluator=EvaluatorIdentity(
            Digest.of_json([check.to_state() for check in checks]), EVALUATOR_VERSION
        ),
        environment=EnvironmentFingerprint(Digest.of_bytes(b"host"), (ToolVersion("python", "3"),)),
    )


def _artifact(raw: bytes, media_type: str = "application/json") -> CaptureArtifact:
    return CaptureArtifact(
        Digest.of_bytes(raw), media_type, len(raw), validate_media(raw, media_type)
    )


def _receipt(
    inputs: AcceptanceInputs,
    captures: tuple[CaptureArtifact, ...],
    *,
    identity: str = "enginery-command-observer",
    kind: ObserverKind = ObserverKind.COMMAND,
    category: ReceiptCategory = ReceiptCategory.INSTRUMENT_OBSERVATION,
    references: tuple[Digest, ...] = (),
    mock: bool = False,
) -> EvidenceReceipt:
    return EvidenceReceipt(
        source_revision=inputs.source_revision,
        base_revision=inputs.base_revision,
        head_revision=inputs.head_revision,
        build=inputs.build,
        configuration_snapshot_digest=inputs.configuration_snapshot_digest,
        evaluator=inputs.evaluator,
        environment=inputs.environment,
        observer=ObserverIdentity(identity, kind, EVALUATOR_VERSION, mock=mock),
        capture_manifest=CaptureManifest(captures),
        started_at=_NOW,
        ended_at=_NOW,
        category=category,
        input_receipt_digests=references,
    )


def _observed(check: BehavioralCheck, inputs: AcceptanceInputs, **changes: object) -> ObservedCheck:
    data = ObservationData(
        observer=check.observer,
        returncode=0,
        stdout="changed",
        started_at=_NOW.isoformat(),
        ended_at=_NOW.isoformat(),
    )
    data = replace(data, **changes)
    captures = (
        _artifact(canonical_bytes(observation_payload(data))),
        *(_artifact(content, media_type) for media_type, content in data.captures),
    )
    receipt = _receipt(
        inputs,
        captures,
        identity=f"enginery-{check.observer}-observer",
        kind=ObserverKind(check.observer),
    )
    return ObservedCheck(check.name, data, receipt)


def _unit_calibration(
    inputs: AcceptanceInputs,
    check: BehavioralCheck,
    *,
    classification: str = "caught",
    empty: bool = False,
) -> CalibrationReceipt:
    """Construct pure-layer values; the separate driver tests execute actual faults."""
    fixture = _artifact(b"unit-value-fixture", "text/plain")
    baseline, observation = Digest.of_bytes(b"baseline"), Digest.of_bytes(b"fault")
    if classification == "caught":
        outcome = CalibrationOutcome(
            f"{check.name}:0",
            check.fault_classes[0],
            "caught",
            "evaluator",
            True,
            EvidenceResult.PASS,
            EvidenceResult.FAIL,
            baseline,
            observation,
        )
    elif classification == "escaped":
        outcome = CalibrationOutcome(
            f"{check.name}:0",
            check.fault_classes[0],
            "escaped",
            None,
            True,
            EvidenceResult.PASS,
            EvidenceResult.PASS,
            baseline,
            observation,
        )
    else:
        outcome = CalibrationOutcome(
            f"{check.name}:0",
            check.fault_classes[0],
            "inconclusive",
            None,
            True,
            EvidenceResult.PASS,
            EvidenceResult.INDETERMINATE,
            baseline,
            observation,
        )
    outcomes = () if empty else (outcome,)
    payload = {
        "schema_version": 1,
        "fixture_digest": str(fixture.digest),
        "results": [item.to_state() for item in outcomes],
    }
    receipt = _receipt(
        inputs,
        (fixture, _artifact(canonical_bytes(payload))),
        identity=CALIBRATION_DRIVER,
        references=(baseline, observation),
    )
    return CalibrationReceipt(receipt, outcomes, fixture.digest)


def test_pure_evaluator_computes_a_nonempty_current_verdict() -> None:
    check = _check()
    inputs = _inputs((check,))
    result = evaluate_behavioral(
        checks=(check,),
        inputs=inputs,
        observations=(_observed(check, inputs),),
        calibration=_unit_calibration(inputs, check),
    )
    assert result.result is EvidenceResult.PASS
    assert result.holds == ()
    assert result.assertion_outcomes[0].ran


@pytest.mark.parametrize("marker", ["unrun", "skipped", "mock", "worker_claim"])
def test_untrusted_or_unexecuted_observations_cannot_pass(marker: str) -> None:
    check = _check()
    inputs = _inputs((check,))
    changes: dict[str, object] = {
        "unrun": {"ran": False},
        "skipped": {"skipped": True},
        "mock": {"mock": True},
    }.get(marker, {})
    observed = _observed(check, inputs, **changes)
    if marker == "worker_claim":
        observed = replace(
            observed,
            receipt=replace(
                observed.receipt,
                category=ReceiptCategory.WORKER_CLAIM,
                observer=ObserverIdentity("worker", ObserverKind.WORKER, "1"),
            ),
        )
    result = evaluate_behavioral(
        checks=(check,),
        inputs=inputs,
        observations=(observed,),
        calibration=_unit_calibration(inputs, check),
    )
    assert result.result is EvidenceResult.INDETERMINATE
    assert result.holds
    assert all(item.result is not EvidenceResult.PASS for item in result.assertion_outcomes)


def test_zero_assertions_cannot_reach_empty_requirements_pass() -> None:
    check = replace(_check(), assertions=())
    inputs = _inputs((check,))
    result = evaluate_behavioral(
        checks=(check,),
        inputs=inputs,
        observations=(_observed(check, inputs),),
        calibration=_unit_calibration(inputs, check, empty=True),
    )
    assert result.result is EvidenceResult.INDETERMINATE
    assert "no_behavioral_checks" in result.holds


@pytest.mark.parametrize(
    "field",
    [
        "source_revision",
        "base_revision",
        "head_revision",
        "build",
        "configuration_snapshot_digest",
        "evaluator",
        "environment",
    ],
)
def test_every_current_input_identity_is_checked(field: str) -> None:
    check = _check()
    inputs = _inputs((check,))
    stale_values: dict[str, object] = {
        "source_revision": "d" * 40,
        "base_revision": "e" * 40,
        "head_revision": "f" * 40,
        "build": BuildIdentity(BuildKind.SOURCE_TREE, Digest.of_bytes(b"old-build")),
        "configuration_snapshot_digest": Digest.of_bytes(b"old-config"),
        "evaluator": replace(inputs.evaluator, version="old-evaluator"),
        "environment": replace(
            inputs.environment, host_receipt_digest=Digest.of_bytes(b"old-host")
        ),
    }
    observed = _observed(check, inputs)
    observed = replace(observed, receipt=replace(observed.receipt, **{field: stale_values[field]}))
    result = evaluate_behavioral(
        checks=(check,),
        inputs=inputs,
        observations=(observed,),
        calibration=_unit_calibration(inputs, check),
    )
    assert result.result is EvidenceResult.INDETERMINATE
    assert "behavioral_observation_stale" in result.holds


@pytest.mark.parametrize("classification", ["escaped", "inconclusive"])
def test_incomplete_or_unsound_calibration_never_qualifies(classification: str) -> None:
    check = _check()
    inputs = _inputs((check,))
    for calibration in (
        _unit_calibration(inputs, check, classification=classification),
        _unit_calibration(inputs, check, empty=True),
    ):
        result = evaluate_behavioral(
            checks=(check,),
            inputs=inputs,
            observations=(_observed(check, inputs),),
            calibration=calibration,
        )
        assert result.result is EvidenceResult.INDETERMINATE
        assert any("calibration" in hold for hold in result.holds)


def test_semantics_not_claimed_success_are_evaluated() -> None:
    check = _check()
    inputs = _inputs((check,))
    result = evaluate_behavioral(
        checks=(check,),
        inputs=inputs,
        observations=(_observed(check, inputs, stdout="constant"),),
        calibration=_unit_calibration(inputs, check),
    )
    assert result.result is EvidenceResult.FAIL
    assert result.assertion_outcomes[0].result is EvidenceResult.FAIL


def test_decodable_browser_media_does_not_replace_a_semantic_assertion() -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )
    check = BehavioralCheck(
        "browser",
        "browser",
        (BehavioralAssertion("dom", "contains", "changed", selector="#result"),),
        ("constant_result",),
        url="http://127.0.0.1:1234/",
    )
    inputs = _inputs((check,))
    observed = _observed(
        check,
        inputs,
        dom_results=((0, False),),
        captures=(("image/png", png),),
    )
    result = evaluate_behavioral(
        checks=(check,),
        inputs=inputs,
        observations=(observed,),
        calibration=_unit_calibration(inputs, check),
    )
    assert result.holds == ("behavioral_assertion_failed:browser:0",)
    assert result.result is EvidenceResult.FAIL


def test_calibration_requires_the_same_evaluator_identity() -> None:
    check = _check()
    inputs = _inputs((check,))
    calibration = _unit_calibration(inputs, check)
    calibration = replace(
        calibration,
        receipt=replace(
            calibration.receipt,
            evaluator=replace(inputs.evaluator, version="stale-evaluator"),
        ),
    )
    result = evaluate_behavioral(
        checks=(check,),
        inputs=inputs,
        observations=(_observed(check, inputs),),
        calibration=calibration,
    )
    assert result.result is EvidenceResult.INDETERMINATE
    assert "behavioral_calibration_stale" in result.holds


def test_infrastructure_and_unapplied_faults_have_distinct_classifications() -> None:
    assert classify_calibration(
        applied=False,
        baseline_result=EvidenceResult.PASS,
        fault_result=EvidenceResult.FAIL,
    ) == ("not_injected", None)
    assert classify_calibration(
        applied=True,
        baseline_result=EvidenceResult.PASS,
        fault_result=EvidenceResult.FAIL,
        infrastructure_error="timeout",
        binding_rejected=True,
    ) == ("inconclusive", None)
    assert classify_calibration(
        applied=True,
        baseline_result=EvidenceResult.PASS,
        fault_result=EvidenceResult.FAIL,
    ) == ("caught", "evaluator")
    assert classify_calibration(
        applied=True,
        baseline_result=EvidenceResult.PASS,
        fault_result=EvidenceResult.PASS,
    ) == ("escaped", None)
    assert classify_calibration(
        applied=True,
        baseline_result=EvidenceResult.PASS,
        binding_accepted=True,
    ) == ("escaped", None)


def _repository(root: Path, program: str) -> tuple[Path, str]:
    checkout = root / "checkout"
    checkout.mkdir()
    (checkout / "behavior.py").write_text(program)
    environment = repository_isolated_environment()
    for arguments in (
        ("init", "--quiet"),
        ("add", "behavior.py"),
        (
            "-c",
            "user.name=Local Test",
            "-c",
            "user.email=local@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "-m",
            "candidate",
        ),
    ):
        subprocess.run(
            ("git", "-C", str(checkout), *arguments),
            check=True,
            capture_output=True,
            env=environment,
        )
    head = subprocess.run(
        ("git", "-C", str(checkout), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    return checkout, head


def _service(root: Path, **options: str) -> BehavioralAcceptanceService:
    custody = root / "evaluator"
    custody.mkdir(mode=0o700)
    return BehavioralAcceptanceService(ArtifactStore(root / "artifacts"), custody, **options)


def _current(checks: tuple[BehavioralCheck, ...], checkout: Path, head: str) -> AcceptanceInputs:
    return replace(
        _inputs(checks),
        source_revision=head,
        base_revision=head,
        head_revision=head,
        build=verified_build(checkout, head),
        environment=host_environment(),
    )


def _calibrate(
    service: BehavioralAcceptanceService,
    checks: tuple[BehavioralCheck, ...],
    inputs: AcceptanceInputs,
    checkout: Path,
    *,
    inject_faults: tuple[FaultClass, ...] | None = None,
) -> CalibrationReceipt:
    return service.calibrate(
        checks=checks,
        cwd=checkout,
        source_revision=inputs.source_revision,
        base_revision=inputs.base_revision,
        head_revision=inputs.head_revision,
        configuration_snapshot_digest=inputs.configuration_snapshot_digest,
        evaluator=inputs.evaluator,
        environment=inputs.environment,
        build=inputs.build,
        inject_faults=inject_faults,
    )


def _run(
    service: BehavioralAcceptanceService,
    checks: tuple[BehavioralCheck, ...],
    inputs: AcceptanceInputs,
    checkout: Path,
    *,
    calibration: CalibrationReceipt | None = None,
) -> AcceptanceRun:
    return service.run(
        checks=checks,
        cwd=checkout,
        source_revision=inputs.source_revision,
        base_revision=inputs.base_revision,
        head_revision=inputs.head_revision,
        configuration_snapshot_digest=inputs.configuration_snapshot_digest,
        evaluator=inputs.evaluator,
        environment=inputs.environment,
        build=inputs.build,
        calibration=calibration,
    )


def test_real_faults_are_caught_at_their_rungs_and_current_receipts_persist(tmp_path: Path) -> None:
    checkout, head = _repository(
        tmp_path,
        (
            "from pathlib import Path\n"
            "Path('state.txt').write_text('saved-change')\n"
            "print('changed-path', end='')\n"
        ),
    )
    check = BehavioralCheck(
        "changed",
        "command",
        (
            BehavioralAssertion("stdout", "equals", "changed-path"),
            BehavioralAssertion("produced_file", "equals", "saved-change", path="state.txt"),
        ),
        ("constant_result", "skipped_persistence", "wrong_build"),
        argv=(sys.executable, "-I", "behavior.py"),
    )
    checks = (check,)
    inputs = _current(checks, checkout, head)
    service = _service(tmp_path)
    uncalibrated = _run(service, checks, inputs, checkout)
    assert uncalibrated.result is EvidenceResult.INDETERMINATE
    assert "behavioral_calibration_required" in uncalibrated.holds
    calibration = _calibrate(service, checks, inputs, checkout)
    rows = {(item.assertion_id, item.fault_class): item for item in calibration.results}
    assert len(rows) == 6
    assert rows["changed:0", "constant_result"].classification == "caught"
    assert rows["changed:0", "constant_result"].rung == "evaluator"
    assert rows["changed:1", "skipped_persistence"].classification == "caught"
    assert rows["changed:1", "skipped_persistence"].rung == "evaluator"
    assert rows["changed:0", "skipped_persistence"].classification == "not_injected"
    assert rows["changed:1", "constant_result"].classification == "not_injected"
    assert all(
        rows[f"changed:{index}", "wrong_build"].rung == "binding_identity" for index in (0, 1)
    )
    # Autoload uses the current private record and verifies its persisted receipt/captures.
    result = _run(service, checks, inputs, checkout)
    assert result.result is EvidenceResult.PASS
    assert result.receipt.category is ReceiptCategory.EVALUATED_VERDICT
    assert service.receipts.load(result.receipt_digest) == result.receipt
    assert service.receipts.load(calibration.digest) == calibration.receipt
    assert inputs.matches(result.receipt)
    assert calibration.digest in result.receipt.input_receipt_digests
    assert all(
        service.receipts.load(digest).category is ReceiptCategory.INSTRUMENT_OBSERVATION
        for digest in result.observation_receipt_digests
    )
    changed_config = replace(
        inputs, configuration_snapshot_digest=Digest.of_bytes(b"changed-config")
    )
    held = _run(service, checks, changed_config, checkout, calibration=calibration)
    assert held.result is EvidenceResult.INDETERMINATE
    assert "behavioral_calibration_stale" in held.holds
    assert held.receipt_digest != result.receipt_digest


def test_real_constant_output_insensitive_assertion_cannot_pass(tmp_path: Path) -> None:
    checkout, head = _repository(tmp_path, "print('enginery-calibration-constant', end='')\n")
    check = BehavioralCheck(
        "constant",
        "command",
        (BehavioralAssertion("stdout", "equals", "enginery-calibration-constant"),),
        ("constant_result",),
        argv=(sys.executable, "-I", "behavior.py"),
    )
    checks = (check,)
    inputs = _current(checks, checkout, head)
    service = _service(tmp_path)
    calibration = _calibrate(service, checks, inputs, checkout)
    assert calibration.results[0].classification == "escaped"
    assert calibration.results[0].applied
    result = _run(service, checks, inputs, checkout)
    assert result.result is EvidenceResult.INDETERMINATE
    assert "behavioral_calibration_escaped" in result.holds


@pytest.mark.parametrize("missing", ["candidate", "fixture"])
def test_actual_observer_start_failures_are_inconclusive(tmp_path: Path, missing: str) -> None:
    checkout, head = _repository(tmp_path, "print('changed', end='')\n")
    check = replace(
        _check(),
        fault_classes=("constant_result",),
        argv=(str(tmp_path / "missing-program"),)
        if missing == "candidate"
        else (sys.executable, "-I", "behavior.py"),
    )
    checks = (check,)
    inputs = _current(checks, checkout, head)
    service = _service(
        tmp_path,
        **(
            {"calibration_python": str(tmp_path / "missing-python")} if missing == "fixture" else {}
        ),
    )
    calibration = _calibrate(service, checks, inputs, checkout)
    assert calibration.results[0].classification == "inconclusive"
    assert calibration.results[0].rung is None
    assert _run(service, checks, inputs, checkout).result is EvidenceResult.INDETERMINATE


def test_actual_uninjected_faults_are_not_successful_calibration(tmp_path: Path) -> None:
    checkout, head = _repository(tmp_path, "print('changed', end='')\n")
    check = replace(_check(), argv=(sys.executable, "-I", "behavior.py"))
    checks = (check,)
    inputs = _current(checks, checkout, head)
    service = _service(tmp_path)
    calibration = _calibrate(service, checks, inputs, checkout, inject_faults=())
    assert all(
        item.classification == "not_injected" and not item.applied for item in calibration.results
    )
    assert _run(service, checks, inputs, checkout).result is EvidenceResult.INDETERMINATE


def test_checkout_identity_is_independently_verified(tmp_path: Path) -> None:
    checkout, head = _repository(tmp_path, "print('changed', end='')\n")
    checks = (replace(_check(), argv=(sys.executable, "-I", "behavior.py")),)
    inputs = _current(checks, checkout, head)
    service = _service(tmp_path)
    with pytest.raises(StaleEvidenceError, match="behavioral_build_mismatch"):
        _run(service, checks, replace(inputs, head_revision="0" * 40), checkout)
    (checkout / "behavior.py").write_text("print('worker mutation')\n")
    with pytest.raises(StaleEvidenceError, match="behavioral_build_mismatch"):
        _calibrate(service, checks, inputs, checkout)


def test_local_service_uses_real_http_observation_and_constant_fault(tmp_path: Path) -> None:
    checkout, head = _repository(
        tmp_path,
        (
            "import sys\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "class Handler(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n        self.end_headers()\n"
            "        self.wfile.write(b'changed-service')\n"
            "    def log_message(self, *args):\n        pass\n"
            "HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()\n"
        ),
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    check = BehavioralCheck(
        "service",
        "service",
        (BehavioralAssertion("body", "contains", "changed-service"),),
        ("constant_result", "wrong_build"),
        argv=(sys.executable, "-I", "behavior.py", str(port)),
        url=f"http://127.0.0.1:{port}/",
    )
    checks = (check,)
    inputs = _current(checks, checkout, head)
    service = _service(tmp_path)
    calibration = _calibrate(service, checks, inputs, checkout)
    assert all(item.classification == "caught" for item in calibration.results)
    assert {item.rung for item in calibration.results} == {"evaluator", "binding_identity"}
    assert _run(service, checks, inputs, checkout).result is EvidenceResult.PASS


def test_zero_check_run_records_a_hold_not_an_empty_pass(tmp_path: Path) -> None:
    checkout, head = _repository(tmp_path, "print('unused')\n")
    checks: tuple[BehavioralCheck, ...] = ()
    inputs = _current(checks, checkout, head)
    service = _service(tmp_path)
    result = _run(service, checks, inputs, checkout)
    assert result.result is EvidenceResult.INDETERMINATE
    assert result.receipt.category is ReceiptCategory.EVALUATED_VERDICT
    assert "no_behavioral_checks" in result.holds
    assert result.receipt.assertion_outcomes == ()
