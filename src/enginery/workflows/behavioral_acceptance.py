"""Trusted behavioral observation, protected fault execution and durable receipts.

The caller supplies assertions already read from evaluator custody and an existing
private evaluator directory. Nothing is installed or read from a worker's assertion files.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from enginery.adapters.behavioral_observers import observe
from enginery.adapters.checkout_identity import verified_build
from enginery.application.behavioral_calibration import (
    CALIBRATION_DRIVER,
    AcceptanceInputs,
    CalibrationClassification,
    CalibrationOutcome,
    CalibrationReceipt,
    CalibrationRung,
)
from enginery.application.behavioral_checks import (
    EVALUATOR_VERSION,
    BehavioralCheck,
    FaultClass,
)
from enginery.application.behavioral_observation import ObservationData
from enginery.domain.behavioral_evidence import (
    MAX_EVIDENCE_BYTES,
    BuildIdentity,
    CaptureArtifact,
    CaptureManifest,
    EnvironmentFingerprint,
    EvaluatorIdentity,
    EvidenceReceipt,
    ObserverIdentity,
    ObserverKind,
    ReceiptCategory,
)
from enginery.domain.digests import Digest
from enginery.domain.errors import InvalidInputError, StaleEvidenceError
from enginery.domain.node_attempt import EvidenceResult
from enginery.evidence.behavioral import (
    BehavioralEvaluation,
    ObservedCheck,
    assertion_id,
    canonical_bytes,
    classify_calibration,
    evaluate_assertions,
    evaluate_behavioral,
    evaluate_observation,
    fault_applies,
    observation_payload,
)
from enginery.evidence.media import validate_media
from enginery.ledger.artifact_store import ArtifactStore
from enginery.ledger.evidence_receipts import EvidenceReceiptRepository

# These bytes are copied exclusively into evaluator custody and checked before execution.
# The fixture deliberately implements the faults: it is not a returned canned verdict.
_FIXTURE = b'''"""Protected deliberate-fault program; no acceptance decisions live here."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

spec = json.loads(Path(sys.argv[1]).read_text())
constant = "enginery-calibration-constant"
if spec["observer"] == "command":
    # The normal persistence fixture writes real bytes; the injected mode suppresses
    # that operation while preserving successful process termination.
    if spec["fault"] not in ("constant_result", "skipped_persistence"):
        for name, content in spec["produced_files"]:
            target = Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    print(constant, end="")
    print(constant, end="", file=sys.stderr)
else:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = ("<html><body>" + constant + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    HTTPServer(("127.0.0.1", spec["port"]), Handler).serve_forever()
'''


@dataclass(frozen=True, slots=True)
class AcceptanceRun:
    result: EvidenceResult
    receipt: EvidenceReceipt
    receipt_digest: Digest
    observation_receipt_digests: tuple[Digest, ...]
    holds: tuple[str, ...]


def _private_directory(path: Path, *, cwd: Path | None = None) -> None:
    """Validate the directory initialized by composition's ensure_private_directory."""
    if not path.is_absolute() or ".." in path.parts:
        raise InvalidInputError("behavioral evaluator directory must be an absolute private path")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise InvalidInputError(
                "behavioral evaluator directory must not contain symbolic links"
            )
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
        raise InvalidInputError("behavioral evaluator directory must have mode 0700")
    if metadata.st_uid != os.getuid():
        raise InvalidInputError("behavioral evaluator directory must be owned by the operator")
    if cwd is not None and path.is_relative_to(cwd.resolve()):
        raise InvalidInputError("behavioral evaluator custody must be outside the worker checkout")


def _write_private(path: Path, content: bytes) -> None:
    if len(content) > MAX_EVIDENCE_BYTES:
        raise InvalidInputError("behavioral private document exceeds the 1 MiB limit")
    _private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".behavioral-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_private(path: Path) -> bytes | None:
    _private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.getuid()
            or metadata.st_size > MAX_EVIDENCE_BYTES
        ):
            raise InvalidInputError("behavioral private document must be a bounded private file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(content) > MAX_EVIDENCE_BYTES:
            raise InvalidInputError("behavioral private document exceeds the 1 MiB limit")
        return content
    finally:
        os.close(descriptor)


def _receipt(
    inputs: AcceptanceInputs,
    *,
    observer: ObserverIdentity,
    captures: tuple[CaptureArtifact, ...],
    started_at: datetime,
    ended_at: datetime,
    category: ReceiptCategory = ReceiptCategory.INSTRUMENT_OBSERVATION,
    evaluation: BehavioralEvaluation | None = None,
    references: tuple[Digest, ...] = (),
) -> EvidenceReceipt:
    return EvidenceReceipt(
        source_revision=inputs.source_revision,
        base_revision=inputs.base_revision,
        head_revision=inputs.head_revision,
        build=inputs.build,
        configuration_snapshot_digest=inputs.configuration_snapshot_digest,
        evaluator=inputs.evaluator,
        environment=inputs.environment,
        observer=observer,
        capture_manifest=CaptureManifest(captures),
        started_at=started_at,
        ended_at=ended_at,
        category=category,
        result=None if evaluation is None else evaluation.result,
        input_receipt_digests=references,
        assertion_outcomes=() if evaluation is None else evaluation.assertion_outcomes,
    )


class BehavioralAcceptanceService:
    """Compose the existing ArtifactStore with real observers and the pure evaluator."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        evaluator_directory: Path,
        *,
        browser_executable: str | None = None,
        browser_expected_digest: str | None = None,
        calibration_python: str = sys.executable,
    ) -> None:
        _private_directory(evaluator_directory)
        self.artifacts = artifacts
        self.evaluator_directory = evaluator_directory
        self.browser_executable = browser_executable
        self.browser_expected_digest = browser_expected_digest
        self.calibration_python = calibration_python
        self.receipts = EvidenceReceiptRepository(artifacts, media_validator=validate_media)

    def _inputs(
        self,
        *,
        checks: tuple[BehavioralCheck, ...],
        cwd: Path,
        source_revision: str,
        base_revision: str,
        head_revision: str,
        configuration_snapshot_digest: Digest,
        evaluator: EvaluatorIdentity,
        environment: EnvironmentFingerprint,
        build: BuildIdentity | None,
    ) -> AcceptanceInputs:
        _private_directory(self.evaluator_directory, cwd=cwd)
        if (
            evaluator.version != EVALUATOR_VERSION
            or evaluator.assertion_set_digest
            != Digest.of_json([check.to_state() for check in checks])
        ):
            raise StaleEvidenceError(
                "behavioral_evaluator_identity_mismatch: rebuild request custody"
            )
        current_build = verified_build(cwd, head_revision)
        if build is not None and current_build != build:
            raise StaleEvidenceError(
                "behavioral_build_mismatch: supplied build differs from checkout"
            )
        return AcceptanceInputs(
            source_revision,
            base_revision,
            head_revision,
            current_build,
            configuration_snapshot_digest,
            evaluator,
            environment,
        )

    def _confirm_build(self, cwd: Path, inputs: AcceptanceInputs) -> None:
        if verified_build(cwd, inputs.head_revision) != inputs.build:
            raise StaleEvidenceError(
                "behavioral_build_mismatch: checkout changed during observation"
            )

    def _capture(self, payload: object) -> CaptureArtifact:
        return self.receipts.publish_capture(
            canonical_bytes(payload), media_type="application/json"
        )

    def _observe(self, check: BehavioralCheck, cwd: Path) -> ObservationData:
        return observe(
            check,
            cwd=cwd,
            browser_executable=self.browser_executable,
            browser_expected_digest=self.browser_expected_digest,
        )

    def _record_observation(
        self,
        check: BehavioralCheck,
        data: ObservationData,
        inputs: AcceptanceInputs,
        *,
        extra_captures: tuple[CaptureArtifact, ...] = (),
        references: tuple[Digest, ...] = (),
    ) -> ObservedCheck:
        now = datetime.now(UTC)
        try:
            start, end = (
                datetime.fromisoformat(data.started_at),
                datetime.fromisoformat(data.ended_at),
            )
            if start.utcoffset() is None or end.utcoffset() is None or end < start:
                raise ValueError("invalid observation time")
            semantic = self._capture(observation_payload(data))
        except (InvalidInputError, ValueError):
            data = ObservationData(
                observer=check.observer,
                started_at=now.isoformat(),
                ended_at=now.isoformat(),
                infrastructure_error="behavioral_observer_capture_invalid",
                ran=False,
            )
            start = end = now
            semantic = self._capture(observation_payload(data))
        media = tuple(
            self.receipts.publish_capture(content, media_type=media_type)
            for media_type, content in data.captures
        )
        transport = self._capture({"check": check.to_state()})
        receipt = _receipt(
            inputs,
            observer=ObserverIdentity(
                f"enginery-{check.observer}-observer",
                ObserverKind(check.observer),
                EVALUATOR_VERSION,
                mock=data.mock,
            ),
            captures=(semantic, transport, *media, *extra_captures),
            started_at=start,
            ended_at=end,
            references=references,
        )
        self.receipts.publish(receipt)
        return ObservedCheck(check.name, data, receipt)

    def _calibration_path(self, inputs: AcceptanceInputs) -> Path:
        key = Digest.of_json(inputs.to_state()).hex_value
        return self.evaluator_directory / f"calibration-{key}.json"

    def _load_calibration(self, inputs: AcceptanceInputs) -> CalibrationReceipt | None:
        raw = _read_private(self._calibration_path(inputs))
        if raw is None:
            return None
        try:
            calibration = CalibrationReceipt.from_state(json.loads(raw))
        except (ValueError, UnicodeError) as error:
            raise InvalidInputError("behavioral calibration custody is not valid JSON") from error
        persisted = self.receipts.load(calibration.receipt_digest)
        if persisted != calibration.receipt:
            raise StaleEvidenceError("behavioral_calibration_capture_mismatch")
        for digest in persisted.input_receipt_digests:
            self.receipts.load(digest)
        return calibration

    def run(
        self,
        *,
        checks: tuple[BehavioralCheck, ...],
        cwd: Path,
        source_revision: str,
        base_revision: str,
        head_revision: str,
        configuration_snapshot_digest: Digest,
        evaluator: EvaluatorIdentity,
        environment: EnvironmentFingerprint,
        calibration: CalibrationReceipt | None = None,
        build: BuildIdentity | None = None,
    ) -> AcceptanceRun:
        inputs = self._inputs(
            checks=checks,
            cwd=cwd,
            source_revision=source_revision,
            base_revision=base_revision,
            head_revision=head_revision,
            configuration_snapshot_digest=configuration_snapshot_digest,
            evaluator=evaluator,
            environment=environment,
            build=build,
        )
        start = datetime.now(UTC)
        calibration = calibration if calibration is not None else self._load_calibration(inputs)
        if calibration is not None:
            self.receipts.load(calibration.receipt_digest)
        observed: list[ObservedCheck] = []
        for check in checks:
            self._confirm_build(cwd, inputs)
            observed.append(self._record_observation(check, self._observe(check, cwd), inputs))
            self._confirm_build(cwd, inputs)
        evaluation = evaluate_behavioral(
            checks=checks,
            inputs=inputs,
            observations=tuple(observed),
            calibration=calibration,
        )
        run_capture = self._capture(
            {
                "checks": [check.to_state() for check in checks],
                "result": evaluation.result.value,
                "holds": list(evaluation.holds),
                "calibration_receipt_digest": None
                if calibration is None
                else str(calibration.digest),
                "assertion_outcomes": [item.to_state() for item in evaluation.assertion_outcomes],
            }
        )
        end = datetime.now(UTC)
        context = _receipt(
            inputs,
            observer=ObserverIdentity(
                "enginery-behavioral-run", ObserverKind.COMMAND, EVALUATOR_VERSION
            ),
            captures=(run_capture,),
            started_at=start,
            ended_at=end,
        )
        context_digest = self.receipts.publish(context)
        observation_digests = tuple(item.receipt.digest for item in observed)
        references = (context_digest, *observation_digests)
        if calibration is not None:
            references += (calibration.digest,)
        verdict = _receipt(
            inputs,
            observer=ObserverIdentity(
                "enginery-semantic-evaluator", ObserverKind.EVALUATOR, EVALUATOR_VERSION
            ),
            captures=(run_capture,),
            started_at=start,
            ended_at=end,
            category=ReceiptCategory.EVALUATED_VERDICT,
            evaluation=evaluation,
            references=references,
        )
        return AcceptanceRun(
            evaluation.result,
            verdict,
            self.receipts.publish(verdict),
            observation_digests,
            evaluation.holds,
        )

    def calibrate(
        self,
        *,
        checks: tuple[BehavioralCheck, ...],
        cwd: Path,
        source_revision: str,
        base_revision: str,
        head_revision: str,
        configuration_snapshot_digest: Digest,
        evaluator: EvaluatorIdentity,
        environment: EnvironmentFingerprint,
        build: BuildIdentity | None = None,
        inject_faults: tuple[FaultClass, ...] | None = None,
    ) -> CalibrationReceipt:
        inputs = self._inputs(
            checks=checks,
            cwd=cwd,
            source_revision=source_revision,
            base_revision=base_revision,
            head_revision=head_revision,
            configuration_snapshot_digest=configuration_snapshot_digest,
            evaluator=evaluator,
            environment=environment,
            build=build,
        )
        start = datetime.now(UTC)
        fixture_capture = self.receipts.publish_capture(_FIXTURE, media_type="text/x-python")
        outcomes: list[CalibrationOutcome] = []
        references: list[Digest] = []
        for check in checks:
            self._confirm_build(cwd, inputs)
            baseline = self._record_observation(check, self._observe(check, cwd), inputs)
            self._confirm_build(cwd, inputs)
            references.append(baseline.receipt.digest)
            baseline_results = evaluate_observation(
                check=check,
                observation=baseline,
                inputs=inputs,
            ).assertion_outcomes
            for fault in check.fault_classes:
                applicable = any(fault_applies(assertion, fault) for assertion in check.assertions)
                requested = inject_faults is None or fault in inject_faults
                observation: ObservedCheck | None = None
                binding_rejected = False
                infrastructure_error: str | None = None
                applied = False
                if applicable and requested:
                    if fault == "wrong_build":
                        observation, binding_rejected, infrastructure_error = self._wrong_build(
                            check,
                            cwd,
                            inputs,
                            fixture_capture,
                        )
                        applied = True
                    else:
                        observation = self._fault_observation(check, fault, inputs, fixture_capture)
                        applied = observation.data.ran
                        infrastructure_error = observation.data.infrastructure_error
                    references.append(observation.receipt.digest)
                fault_results = (
                    ()
                    if observation is None
                    else (
                        evaluate_assertions(check, observation.data)
                        if fault == "wrong_build"
                        else evaluate_observation(
                            check=check,
                            observation=observation,
                            inputs=inputs,
                        ).assertion_outcomes
                    )
                )
                for index, assertion in enumerate(check.assertions):
                    relevant = fault_applies(assertion, fault)
                    baseline_result = baseline_results[index].result
                    fault_result = None if not fault_results else fault_results[index].result
                    classification: CalibrationClassification
                    rung: CalibrationRung | None
                    # A failed start was attempted injection, but never a demonstrated fault/catch.
                    if relevant and requested and infrastructure_error is not None:
                        classification, rung = "inconclusive", None
                    else:
                        classification, rung = classify_calibration(
                            applied=applied and relevant,
                            baseline_result=baseline_result,
                            fault_result=fault_result,
                            infrastructure_error=infrastructure_error,
                            binding_rejected=binding_rejected,
                            binding_accepted=(
                                fault == "wrong_build"
                                and applied
                                and not binding_rejected
                                and infrastructure_error is None
                            ),
                        )
                    outcomes.append(
                        CalibrationOutcome(
                            assertion_id(check, index),
                            fault,
                            classification,
                            rung,
                            applied=applied and relevant,
                            baseline_result=baseline_result,
                            fault_result=fault_result if relevant else None,
                            baseline_receipt_digest=baseline.receipt.digest,
                            observation_receipt_digest=(
                                None if observation is None else observation.receipt.digest
                            ),
                            reason=(
                                "fault_not_applicable"
                                if not relevant
                                else "fault_not_requested"
                                if not requested
                                else infrastructure_error
                            ),
                        )
                    )
        self._confirm_build(cwd, inputs)
        result_payload = {
            "schema_version": 1,
            "fixture_digest": str(fixture_capture.digest),
            "results": [item.to_state() for item in outcomes],
        }
        capture = self._capture(result_payload)
        receipt = _receipt(
            inputs,
            observer=ObserverIdentity(
                CALIBRATION_DRIVER, ObserverKind.COMMAND, inputs.evaluator.version
            ),
            captures=(fixture_capture, capture),
            started_at=start,
            ended_at=datetime.now(UTC),
            references=tuple(dict.fromkeys(references)),
        )
        self.receipts.publish(receipt)
        calibration = CalibrationReceipt(receipt, tuple(outcomes), fixture_capture.digest)
        _write_private(self._calibration_path(inputs), canonical_bytes(calibration.to_state()))
        return calibration

    def _fault_observation(
        self,
        check: BehavioralCheck,
        fault: FaultClass,
        inputs: AcceptanceInputs,
        fixture_capture: CaptureArtifact,
    ) -> ObservedCheck:
        with tempfile.TemporaryDirectory(
            prefix="fault-", dir=self.evaluator_directory
        ) as directory:
            root = Path(directory)
            _private_directory(root)
            fixture = root / "deliberate_fault.py"
            _write_private(fixture, _FIXTURE)
            port = 0
            if check.observer != "command":
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
            file_contents: dict[str, str] = {}
            for assertion in check.assertions:
                if assertion.path is None or not fault_applies(assertion, "skipped_persistence"):
                    continue
                if assertion.operator == "equals" and isinstance(assertion.expected, str):
                    file_contents[assertion.path] = assertion.expected
                else:
                    file_contents.setdefault(assertion.path, "enginery-calibration-persisted")
            for assertion in check.assertions:
                if (
                    assertion.path is not None
                    and assertion.operator == "contains"
                    and isinstance(assertion.expected, str)
                    and assertion.expected not in file_contents[assertion.path]
                ):
                    file_contents[assertion.path] += assertion.expected
            produced_files = [[path, content] for path, content in file_contents.items()]
            spec: dict[str, object] = {
                "fault": fault,
                "observer": check.observer,
                "port": port,
                "produced_files": produced_files,
            }
            spec_path = root / "fault.json"
            _write_private(spec_path, canonical_bytes(spec))
            fault_check = replace(
                check,
                argv=(self.calibration_python, "-I", str(fixture), str(spec_path)),
                url=None if check.observer == "command" else f"http://127.0.0.1:{port}/",
            )
            references: tuple[Digest, ...] = ()
            if fault == "skipped_persistence":
                baseline_root = root / "baseline"
                baseline_root.mkdir(mode=0o700)
                baseline_spec = {**spec, "fault": "baseline"}
                _write_private(spec_path, canonical_bytes(baseline_spec))
                normal = self._record_observation(
                    fault_check,
                    self._observe(fault_check, baseline_root),
                    inputs,
                    extra_captures=(fixture_capture, self._capture(baseline_spec)),
                )
                references = (normal.receipt.digest,)
                normal_outcomes = evaluate_observation(
                    check=check,
                    observation=normal,
                    inputs=inputs,
                ).assertion_outcomes
                if normal.data.infrastructure_error is not None or any(
                    normal_outcomes[index].result is not EvidenceResult.PASS
                    for index, assertion in enumerate(check.assertions)
                    if fault_applies(assertion, fault)
                ):
                    data = replace(
                        normal.data,
                        ran=False,
                        infrastructure_error=(
                            normal.data.infrastructure_error
                            or "behavioral_calibration_fixture_baseline_failed"
                        ),
                    )
                    return self._record_observation(
                        fault_check,
                        data,
                        inputs,
                        extra_captures=(fixture_capture,),
                        references=references,
                    )
                _write_private(spec_path, canonical_bytes(spec))
            if _read_private(fixture) != _FIXTURE:
                raise StaleEvidenceError("behavioral_calibration_fixture_changed")
            injected_root = root / "injected"
            injected_root.mkdir(mode=0o700)
            data = self._observe(fault_check, injected_root)
            return self._record_observation(
                fault_check,
                data,
                inputs,
                extra_captures=(fixture_capture, self._capture(spec)),
                references=references,
            )

    def _wrong_build(
        self,
        check: BehavioralCheck,
        cwd: Path,
        inputs: AcceptanceInputs,
        fixture_capture: CaptureArtifact,
    ) -> tuple[ObservedCheck, bool, str | None]:
        wrong_head = ("0" if inputs.head_revision != "0" * 40 else "1") * 40
        rejected = False
        infrastructure_error: str | None = None
        try:
            verified_build(cwd, wrong_head)
        except StaleEvidenceError as error:
            if str(error).startswith("behavioral_build_mismatch:"):
                rejected = True
            else:
                infrastructure_error = "behavioral_binding_unavailable"
        except (OSError, TimeoutError):
            infrastructure_error = "behavioral_binding_unavailable"
        now = datetime.now(UTC).isoformat()
        gate = self._capture(
            {
                "fault": "wrong_build",
                "expected_head": wrong_head,
                "current_head": inputs.head_revision,
                "binding_rejected": rejected,
                "infrastructure_error": infrastructure_error,
            }
        )
        data = ObservationData(
            check.observer,
            started_at=now,
            ended_at=now,
            ran=False,
            infrastructure_error=infrastructure_error,
        )
        observation = self._record_observation(
            check,
            data,
            replace(inputs, head_revision=wrong_head),
            extra_captures=(fixture_capture, gate),
        )
        return observation, rejected, infrastructure_error


def run_acceptance(
    *,
    checks: tuple[BehavioralCheck, ...],
    inputs: AcceptanceInputs,
    cwd: Path,
    evaluator_directory: Path,
    artifacts: ArtifactStore,
    calibration: CalibrationReceipt | None = None,
    browser_executable: str | None = None,
    browser_expected_digest: str | None = None,
) -> AcceptanceRun:
    """Functional composition entry point for an injected Stage 1 verification callback."""
    service = BehavioralAcceptanceService(
        artifacts,
        evaluator_directory,
        browser_executable=browser_executable,
        browser_expected_digest=browser_expected_digest,
    )
    return service.run(
        checks=checks,
        cwd=cwd,
        source_revision=inputs.source_revision,
        base_revision=inputs.base_revision,
        head_revision=inputs.head_revision,
        configuration_snapshot_digest=inputs.configuration_snapshot_digest,
        evaluator=inputs.evaluator,
        environment=inputs.environment,
        calibration=calibration,
        build=inputs.build,
    )
