#!/usr/bin/env python3
"""Qualify the isolated runner with real attempts, and record evidence of what was observed.

Scenarios:

``runner-boundary``
    Hostile code runs inside the real hardened OCI runner (the product's own launcher, worker image
    and model-access relay) and must be refused on every axis: reading host credentials, writing
    protected paths, escaping the container, reaching the gateway, broker, GitHub or the internet,
    abusing the relay's routes, exceeding resource limits, and shipping hooks the host must never
    execute; and admission must fail closed. Runs on whichever engine ``--docker-context`` /
    ``DOCKER_HOST`` selects (Docker Desktop, or rootless Docker inside a Lima VM).

``runner-auth``
    Exactly one non-mutating authentication session (a prompt asking for the word OK, no tools)
    through the relay inside the runner. Without ``--live`` it uses a loopback stub upstream (mode
    ``local``). ``--live`` uses the operator's real gateway and token file and is an owner-
    authorized action: it needs ``--gateway-url``, ``--gateway-token-file`` and
    ``--confirm-single-session``.

The evidence document (schema_version 1) is printed to stdout and written to ``--evidence-out``;
raw artifacts go to ``<evidence stem>.raw/`` beside it. Exit status: 0 only if every required
assertion ran and passed; 1 if any assertion failed or did not run; 2 for a usage error; 3 when the
engine, an image or a permission is missing (verdict ``hold``). No assertion is ever skipped.

The script and its ``factory_gate_*`` siblings are self-contained: copy the ``scripts/`` directory
next to an installed ``enginery`` wheel and run it there.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from enginery.adapters.oci.engine import OciEngine  # noqa: E402
from enginery.adapters.oci.relay import check_token_file, validate_upstream  # noqa: E402
from enginery.domain.errors import EngineryError  # noqa: E402
from factory_gate_context import (  # noqa: E402
    Gate,
    GateHoldError,
    GateOptions,
    host_identity,
    prepare_gate,
    subject_identity,
)
from factory_gate_evidence import (  # noqa: E402
    EXIT_FAIL,
    EXIT_HOLD,
    EXIT_USAGE,
    MODE_LIVE,
    MODE_LOCAL,
    VERDICT_HOLD,
    EvidenceBuilder,
    dumps,
    exit_code_for,
    utc_now,
)
from factory_gate_support import Scrubber  # noqa: E402

SCENARIO_BOUNDARY = "runner-boundary"
SCENARIO_AUTH = "runner-auth"
SCENARIO_NAMES = (SCENARIO_BOUNDARY, SCENARIO_AUTH)
CONFIRMATION_ENVIRONMENT = "ENGINERY_FACTORY_GATE_CONFIRM_SINGLE_SESSION"
MAX_SESSION_DEADLINE_SECONDS = 900
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SUBJECT_COMMIT_ENVIRONMENT = "ENGINERY_FACTORY_GATE_SUBJECT_COMMIT"

EngineFactory = Callable[[str | None], OciEngine]
PrepareGate = Callable[..., Gate]


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    required: Callable[[GateOptions], dict[str, str]]
    need_relay: bool
    run: Callable[[Gate], None]
    finalize: Callable[[Gate], None]


def default_scenarios() -> dict[str, ScenarioSpec]:
    import factory_gate_auth as auth
    import factory_gate_boundary as boundary

    return {
        SCENARIO_BOUNDARY: ScenarioSpec(
            boundary.required_assertions, True, boundary.run, boundary.finalize
        ),
        SCENARIO_AUTH: ScenarioSpec(auth.required_assertions, True, auth.run, auth.finalize),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_factory_gate.py",
        description="Qualify the isolated runner with real attempts and record evidence.",
    )
    parser.add_argument("--scenario", required=True, choices=SCENARIO_NAMES)
    parser.add_argument(
        "--evidence-out", required=True, type=Path, help="evidence JSON path (raw dir beside it)"
    )
    parser.add_argument("--harness", choices=("omp", "claude"), help="harness (runner-auth)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="runner-auth only: use the real gateway (owner-authorized, exactly one session)",
    )
    parser.add_argument(
        "--confirm-single-session",
        action="store_true",
        help=f"confirm a live run (or set {CONFIRMATION_ENVIRONMENT}=1)",
    )
    parser.add_argument("--docker-context", help="docker context naming the engine to qualify")
    parser.add_argument("--runner-image", help="pinned worker image id sha256:<64 hex>")
    parser.add_argument("--relay-image", help="pinned relay image id sha256:<64 hex>")
    parser.add_argument("--gateway-url", help="http(s)://host[:port] of the model gateway")
    parser.add_argument(
        "--gateway-token-file", type=Path, help="0600 file holding the gateway token"
    )
    parser.add_argument("--model", help="runner-auth only: pin a model for the session")
    parser.add_argument(
        "--session-deadline-seconds",
        type=int,
        default=MAX_SESSION_DEADLINE_SECONDS,
        help=f"runner-auth process-group deadline (1..{MAX_SESSION_DEADLINE_SECONDS})",
    )
    parser.add_argument(
        "--custody-receipt", type=Path, help="runner-auth only: the host's auth-custody receipt"
    )
    parser.add_argument(
        "--subject-commit",
        help=f"git commit id of the build under test (or {SUBJECT_COMMIT_ENVIRONMENT}); "
        "recorded in the evidence, required to name an installed wheel's source",
    )
    return parser


def parse_options(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> GateOptions:
    """Parse and validate the command line; a usage error exits with status 2."""
    parser = build_parser()
    args = parser.parse_args(argv)
    environment = os.environ if environ is None else environ
    scenario: str = args.scenario
    if scenario == SCENARIO_AUTH and args.harness is None:
        parser.error("--harness {omp,claude} is required for --scenario runner-auth")
    if scenario == SCENARIO_BOUNDARY and (args.harness or args.model or args.live):
        parser.error("--harness, --model and --live apply only to --scenario runner-auth")
    if scenario == SCENARIO_BOUNDARY and args.custody_receipt is not None:
        parser.error("--custody-receipt applies only to --scenario runner-auth")
    if (args.gateway_url is None) != (args.gateway_token_file is None):
        parser.error("--gateway-url and --gateway-token-file must be given together")
    confirmed = args.confirm_single_session or environment.get(CONFIRMATION_ENVIRONMENT) == "1"
    if args.live:
        if args.gateway_url is None or args.gateway_token_file is None:
            parser.error("--live requires --gateway-url and --gateway-token-file")
        if not confirmed:
            parser.error(
                "--live runs one real subscription session; confirm it with "
                f"--confirm-single-session (or {CONFIRMATION_ENVIRONMENT}=1)"
            )
    elif args.confirm_single_session:
        parser.error("--confirm-single-session applies only with --live")
    if not 1 <= args.session_deadline_seconds <= MAX_SESSION_DEADLINE_SECONDS:
        parser.error(f"--session-deadline-seconds must be 1..{MAX_SESSION_DEADLINE_SECONDS}")
    for flag, value in (("--runner-image", args.runner_image), ("--relay-image", args.relay_image)):
        if value is not None and not _IMAGE_ID.fullmatch(value):
            parser.error(f"{flag} must be an exact image id sha256:<64 hex>")
    if args.gateway_url is not None:
        try:
            validate_upstream(args.gateway_url)
        except EngineryError as error:
            parser.error(f"--gateway-url: {error}")
    token_file = None
    if args.gateway_token_file is not None:
        token_file = Path(os.path.abspath(args.gateway_token_file))
    custody = None
    if args.custody_receipt is not None:
        custody = Path(os.path.abspath(args.custody_receipt))
    subject_commit = args.subject_commit or environment.get(SUBJECT_COMMIT_ENVIRONMENT)
    if subject_commit is not None and not _COMMIT_SHA.fullmatch(subject_commit):
        parser.error("--subject-commit must be a full 40 or 64 hex git commit id")
    return GateOptions(
        scenario=scenario,
        evidence_out=Path(os.path.abspath(args.evidence_out)),
        harness=args.harness,
        live=bool(args.live),
        confirm_single_session=bool(confirmed),
        docker_context=args.docker_context,
        runner_image=args.runner_image,
        relay_image=args.relay_image,
        gateway_url=args.gateway_url,
        gateway_token_file=token_file,
        model=args.model,
        session_deadline_seconds=args.session_deadline_seconds,
        custody_receipt=custody,
        subject_commit=subject_commit,
    )


def _default_engine(context: str | None) -> OciEngine:
    return OciEngine(context=context)


def _minimal_identities(options: GateOptions, required: dict[str, str]) -> dict[str, Any]:
    from factory_gate_context import evaluator_identity

    return {
        "subject": subject_identity(options.subject_commit),
        "build": {},
        "config": {
            "docker_context": options.docker_context,
            "gateway_url_given": options.gateway_url is not None,
        },
        "host": host_identity(),
        "engine": None,
        "image": None,
        "profile": None,
        "evaluator": evaluator_identity(required),
    }


def _gateway_prerequisites(options: GateOptions) -> None:
    """The operator's token file must exist and be private before any container is started."""
    if options.gateway_token_file is None:
        return
    try:
        check_token_file(options.gateway_token_file)
    except EngineryError as error:
        raise GateHoldError(f"the gateway token file is not usable: {error}") from error


def execute(
    options: GateOptions,
    *,
    engine_factory: EngineFactory = _default_engine,
    scenarios: Mapping[str, ScenarioSpec] | None = None,
    prepare: PrepareGate = prepare_gate,
) -> tuple[dict[str, Any], int]:
    """Run one scenario; return the evidence document and the process exit status."""
    registry = dict(default_scenarios() if scenarios is None else scenarios)
    spec = registry[options.scenario]
    required = spec.required(options)
    scrubber = Scrubber()
    if options.gateway_token_file is not None:
        with contextlib.suppress(OSError):  # an unreadable file is reported as a hold below
            scrubber.register(
                "gateway-token", options.gateway_token_file.read_text(encoding="utf-8").strip()
            )
    evidence = EvidenceBuilder(
        scenario=options.scenario,
        mode=MODE_LIVE if options.live else MODE_LOCAL,
        required=required,
        scrub=scrubber.data,
    )
    if options.scenario == SCENARIO_BOUNDARY and options.gateway_url is None:
        evidence.not_requested["f-relay-real-gateway-models-and-denials"] = (
            "no --gateway-url/--gateway-token-file given; the relay was exercised against the "
            "loopback stub upstream only"
        )
    gate: Gate | None = None
    interrupted = False
    try:
        _gateway_prerequisites(options)
        gate = prepare(options, evidence, scrubber, engine_factory, need_relay=spec.need_relay)
        try:
            spec.run(gate)
        except GateHoldError as hold:
            evidence.hold(str(hold))
        except KeyboardInterrupt:
            interrupted = True
            evidence.abort("interrupted before every assertion ran")
        except Exception as error:
            evidence.abort(f"the scenario stopped on {type(error).__name__}: {error}")
        finally:
            gate.cleanup()
        try:
            spec.finalize(gate)
        except Exception as error:
            evidence.abort(f"final checks stopped on {type(error).__name__}: {error}")
    except GateHoldError as hold:
        evidence.hold(str(hold))
    identities = (
        gate.identities(required) if gate is not None else _minimal_identities(options, required)
    )
    effects: dict[str, Any] = {"hosted_inference_calls": 0}
    if gate is not None:
        effects = {"hosted_inference_calls": 0, **gate.effects}
    document = evidence.build(
        identities=identities,
        effects=effects,
        raw_artifacts_dir=(options.raw_dir.name if options.raw_dir.exists() else None),
        ended_at=utc_now(),
    )
    leaked = _scan_for_secrets(options, scrubber, document)
    if leaked:
        document["verdict"] = "fail"
        document["reason"] = "a registered secret value was found in: " + ", ".join(leaked)
    code = exit_code_for(document)
    if interrupted:
        code = 130
    return document, code


def _scan_for_secrets(
    options: GateOptions, scrubber: Scrubber, document: Mapping[str, Any]
) -> list[str]:
    where: list[str] = []
    if scrubber.contains(json.dumps(document)):
        where.append("the evidence document")
    if options.raw_dir.exists():
        for path in sorted(options.raw_dir.rglob("*")):
            if path.is_file() and scrubber.contains(
                path.read_text(encoding="utf-8", errors="replace")
            ):
                where.append(f"{options.raw_dir.name}/{path.name}")
    return where


def main(
    argv: Sequence[str] | None = None,
    *,
    engine_factory: EngineFactory = _default_engine,
    scenarios: Mapping[str, ScenarioSpec] | None = None,
    prepare: PrepareGate = prepare_gate,
    environ: Mapping[str, str] | None = None,
) -> int:
    options = parse_options(argv, environ)
    try:
        options.evidence_out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        document = _unwritable(options, error)
        sys.stdout.write(dumps(document))
        return EXIT_HOLD
    document, code = execute(
        options, engine_factory=engine_factory, scenarios=scenarios, prepare=prepare
    )
    text = dumps(document)
    try:
        options.evidence_out.write_text(text, encoding="utf-8")
    except OSError as error:
        sys.stderr.write(f"run_factory_gate: cannot write {options.evidence_out}: {error}\n")
        sys.stdout.write(text)
        return EXIT_HOLD if code == 0 else code
    sys.stdout.write(text)
    return code


def _unwritable(options: GateOptions, error: OSError) -> dict[str, Any]:
    registry = default_scenarios()
    required = registry[options.scenario].required(options)
    evidence = EvidenceBuilder(
        scenario=options.scenario,
        mode=MODE_LIVE if options.live else MODE_LOCAL,
        required=required,
    )
    evidence.hold(f"the evidence directory cannot be created: {error}")
    return evidence.build(
        identities=_minimal_identities(options, required),
        effects={"hosted_inference_calls": 0},
        raw_artifacts_dir=None,
    )


__all__ = [
    "EXIT_FAIL",
    "EXIT_HOLD",
    "EXIT_USAGE",
    "VERDICT_HOLD",
    "ScenarioSpec",
    "build_parser",
    "default_scenarios",
    "execute",
    "main",
    "parse_options",
]


if __name__ == "__main__":
    sys.exit(main())
