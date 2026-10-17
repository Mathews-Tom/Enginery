#!/usr/bin/env python3
"""Exercise the released command surface from an isolated installation.

Installs exactly one artifact -- a built wheel, a built sdist, or a published
requirement -- into a fresh virtual environment, then drives the installed
`enginery` entry point through the consumer surface a new release must keep
working: the reported version, `doctor`, the G4 and Stage 1 request commands'
help, execution-selection pinning (an unsupported pin and a contradictory strict
requirement are rejected with exit code 2 and write nothing; valid pins write a
request whose execution selection is bound to the packaged catalog), the packaged
acceptance baseline and execution catalog, the fail-closed G4 gate, and the isolated-runner
surface: `runner` and `enginery-operator` help, the packaged worker and relay recipes (their
digests equal the source tree's and every recipe file is in the wheel), the Stage 1 runner flags,
and -- only when a container engine is reachable -- that `runner status` holds an absent image.
A check that cannot run is reported as NOT RUN; any NOT RUN check makes the run INCOMPLETE
(exit status 3), never a PASS. An engine that is reachable but unqualified is reported as such,
distinct from an unreachable one, and a digest or byte-identity claim is made only when the
comparison was actually made.

No hosted model or GitHub call is made and nothing is written outside the temporary
directory: the request commands read their issue from a stand-in `gh` written there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

_WHEEL_NAME = re.compile(r"^enginery-(?P<version>[0-9][0-9A-Za-z.+!]*?)-py3-none-any\.whl$")
_SDIST_NAME = re.compile(r"^enginery-(?P<version>[0-9][0-9A-Za-z.+!]*?)\.tar\.gz$")
_REQUIREMENT = re.compile(r"^enginery==(?P<version>[0-9][0-9A-Za-z.+!]*)$")

_SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src"
_ASSET_DIRECTORY = ("enginery", "adapters", "oci", "assets")
_RUNNER_ASSETS = (
    "Dockerfile.worker",
    "worker_entry.py",
    "omp-isolated-policy.yml",
    "Dockerfile.relay",
    "relay.py",
)
_ABSENT_IMAGE = "sha256:" + "0" * 63 + "1"

_CLAUDE_MODEL = "claude-sonnet-4-5"
_OMP_MODEL = "anthropic/claude-sonnet-4-5"


class ConsumerSmokeError(RuntimeError):
    """Raised when a released consumer command does not meet its contract."""


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "XDG_CONFIG_HOME": str(cwd / "xdg-config"),
            "XDG_STATE_HOME": str(cwd / "xdg-state"),
        },
    )


def _describe(command: list[str], result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"command: {' '.join(command)}\nexit code: {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _require_success(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    result = _run(command, cwd=cwd)
    if result.returncode != 0:
        raise ConsumerSmokeError(f"command failed\n{_describe(command, result)}")
    return result


def _require_g4_fail_closed(command: list[str], *, cwd: Path) -> None:
    result = _run(command, cwd=cwd)
    if result.returncode != 3 or '"overall": "fail"' not in result.stdout:
        raise ConsumerSmokeError(f"G4 did not remain fail-closed\n{_describe(command, result)}")


def _note(message: str) -> None:
    print(f"ok {message}", flush=True)


def expected_version(*, wheel: Path | None, sdist: Path | None, package: str | None) -> str:
    """Derive the version the installed package must report from the artifact identity."""
    if wheel is not None:
        match = _WHEEL_NAME.match(wheel.name)
        label = f"wheel {wheel.name!r}"
    elif sdist is not None:
        match = _SDIST_NAME.match(sdist.name)
        label = f"sdist {sdist.name!r}"
    else:
        match = _REQUIREMENT.match(package or "")
        label = f"requirement {package!r}"
    if match is None:
        raise ConsumerSmokeError(
            f"cannot derive the expected version from {label}; use an `enginery` wheel, an "
            "`enginery` sdist, or a pinned `enginery==VERSION` requirement"
        )
    return match.group("version")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _installer(python: Path) -> list[str]:
    if shutil.which("uv") is not None:
        return ["uv", "pip", "install", "--python", str(python), "--no-cache"]
    return [str(python), "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check"]


def _check_version(executable: Path, *, root: Path, version: str) -> None:
    result = _require_success([str(executable), "--version"], cwd=root)
    if result.stdout.strip() != f"enginery {version}":
        raise ConsumerSmokeError(
            f"`enginery --version` reported {result.stdout!r}, expected 'enginery {version}'"
        )
    _note(f"enginery --version == enginery {version}")


def _check_doctor(executable: Path, *, root: Path, version: str) -> None:
    result = _require_success([str(executable), "doctor"], cwd=root)
    expected = f"package_metadata: enginery {version} installed"
    if expected not in result.stdout:
        raise ConsumerSmokeError(f"`enginery doctor` did not report {expected!r}:\n{result.stdout}")
    _note("enginery doctor reports the installed version")


def _check_help(executable: Path, *, root: Path) -> None:
    for command in (
        ["gate", "record-g4-deficiency", "--help"],
        ["gate", "record-g4-deficiency-evidence", "--help"],
    ):
        _require_success([str(executable), *command], cwd=root)
    result = _require_success([str(executable), "stage1", "build-request", "--help"], cwd=root)
    missing = [
        flag for flag in ("--model", "--effort", "--strict-effective") if flag not in result.stdout
    ]
    if missing:
        raise ConsumerSmokeError(f"`stage1 build-request --help` does not list {missing}")
    _note("gate and stage1 build-request help; --model/--effort/--strict-effective listed")


_STAND_IN_GITHUB = """#!__PYTHON__
import json
import sys

arguments = sys.argv[1:]
endpoint = arguments[-1] if arguments[:1] == ["api"] else ""
path, _, query = endpoint.partition("?")
parts = path.split("/")
if len(parts) in (5, 6) and parts[0] == "repos" and parts[3] == "issues" and parts[4].isdecimal():
    if len(parts) == 5:
        print(
            json.dumps(
                {
                    "url": "https://api.github.com/" + path,
                    "number": int(parts[4]),
                    "title": "Consumer smoke",
                    "body": "Exercise the installed build-request command.",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            )
        )
        raise SystemExit(0)
    if parts[5] == "labels":
        page = int(dict(pair.split("=") for pair in query.split("&") if pair).get("page", "1"))
        labels = ["enginery/work-kind/issue", "enginery/risk/low"] if page == 1 else []
        print(json.dumps([{"name": name} for name in labels]))
        raise SystemExit(0)
sys.stderr.write("HTTP 404: Not Found\\n")
raise SystemExit(1)
"""


def _stand_in_github(root: Path) -> Path:
    """Write (once) a `gh` that serves one labelled issue, so `build-request` needs no network."""
    executable = root / "gh"
    if not executable.exists():
        executable.write_text(
            _STAND_IN_GITHUB.replace("__PYTHON__", sys.executable), encoding="utf-8"
        )
        executable.chmod(0o755)
    return executable


def _build_request_command(executable: Path, *, root: Path, output: Path) -> list[str]:
    repository = root / "repository"
    if not (repository / ".git").exists():
        repository.mkdir(parents=True, exist_ok=True)
        _require_success(["git", "init", "-q", str(repository)], cwd=root)
        _require_success(["git", "config", "user.name", "consumer smoke"], cwd=repository)
        _require_success(
            ["git", "config", "user.email", "consumer-smoke@example.invalid"], cwd=repository
        )
        (repository / "README").write_text("consumer smoke fixture\n", encoding="utf-8")
        _require_success(["git", "add", "README"], cwd=repository)
        _require_success(["git", "commit", "-qm", "consumer smoke base"], cwd=repository)
    base_revision = _require_success(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=repository
    ).stdout.strip()
    return [
        str(executable),
        "stage1",
        "build-request",
        "--runner",
        "trusted-local",
        "--output",
        str(output),
        "--run-id",
        "consumer-smoke-run",
        "--repository",
        "example/repo",
        "--external-reference",
        "example/repo#1",
        "--github-executable",
        str(_stand_in_github(root)),
        "--base-revision",
        base_revision,
        "--repository-path",
        str(root / "repository"),
        "--workspace-path",
        str(root / "workspace"),
        "--artifact-root",
        str(root / "artifacts"),
    ]


def _check_rejected_pins(executable: Path, *, root: Path) -> None:
    rejected = (
        ("an alias pin", ["--harness-provider", "claude-code", "--model", "opus"], "opus"),
        ("a bare OMP model", ["--harness-provider", "omp", "--model", "gpt-5"], "gpt-5"),
        (
            "a strict requirement on an effort pin",
            [
                "--harness-provider",
                "claude-code",
                "--model",
                _CLAUDE_MODEL,
                "--effort",
                "high",
                "--strict-effective",
            ],
            "strict",
        ),
        (
            "a strict requirement with nothing pinned",
            ["--harness-provider", "claude-code", "--strict-effective"],
            "strict",
        ),
    )
    for label, extra, expected_fragment in rejected:
        output = root / "rejected-request.json"
        command = [*_build_request_command(executable, root=root, output=output), *extra]
        result = _run(command, cwd=root)
        if result.returncode != 2:
            raise ConsumerSmokeError(
                f"{label} was not rejected with exit code 2\n{_describe(command, result)}"
            )
        if expected_fragment not in result.stderr:
            raise ConsumerSmokeError(
                f"{label} was rejected without naming {expected_fragment!r}\n"
                f"{_describe(command, result)}"
            )
        if output.exists():
            raise ConsumerSmokeError(f"{label} was rejected but still wrote {output}")
    _note("unsupported pins exit 2 and write no request")


def check_bound_selection(
    document: Mapping[str, object],
    *,
    harness: str,
    model: str,
    effort: str,
    catalog_version: str,
    catalog_digest: str,
    accepted_versions: str,
    tested_version: str,
    observed_version: str,
) -> None:
    """Require a request document's execution selection to be a bound manual pin.

    The host section must name the catalog's accepted range and tested version and the
    version observed when the request was built, and the observed version must be the
    stand-in host's own, never the tested one by default; callers pass a version other than
    the tested one so a copy of the tested version into the observed one is detected.
    """
    selection = document.get("execution_selection")
    if not isinstance(selection, dict):
        raise ConsumerSmokeError("request has no execution_selection")

    def field(*path: str) -> object:
        node: object = selection
        for key in path:
            if not isinstance(node, dict) or key not in node:
                raise ConsumerSmokeError(f"execution_selection has no {'.'.join(path)}")
            node = node[key]
        return node

    expected: tuple[tuple[tuple[str, ...], object], ...] = (
        (("harness", "value"), harness),
        (("harness", "source"), "run"),
        (("model", "value"), model),
        (("model", "selected"), model),
        (("model", "source"), "run"),
        (("model", "pinned"), True),
        (("effort", "value"), effort),
        (("effort", "selected"), effort),
        (("effort", "source"), "run"),
        (("effort", "pinned"), True),
        (("mode",), "manual_pin"),
        (("strict_effective",), False),
        (("catalog", "version"), catalog_version),
        (("catalog", "digest"), catalog_digest),
        (("host", "support_tier"), "experimental"),
        (("host", "executable_versions"), accepted_versions),
        (("host", "tested_executable_version"), tested_version),
        (("host", "observed_executable_version"), observed_version),
    )
    for path, wanted in expected:
        observed = field(*path)
        if observed != wanted:
            raise ConsumerSmokeError(
                f"execution_selection.{'.'.join(path)} is {observed!r}, expected {wanted!r}"
            )
    configuration = document.get("execution_configuration")
    if not isinstance(configuration, dict) or configuration.get("harness_provider") != harness:
        raise ConsumerSmokeError("execution_configuration does not record the selected harness")


def _packaged_data(python: Path, *, root: Path) -> Mapping[str, object]:
    program = (
        "import json\n"
        "from enginery.application.execution_catalog import load_execution_catalog\n"
        "from enginery.evidence.acceptance_baseline import load_acceptance_baseline\n"
        "catalog = load_execution_catalog()\n"
        "baseline = load_acceptance_baseline()\n"
        "def stand_in(host):\n"
        "    tested = host.version.tested\n"
        "    version = type(tested)(tested.major, tested.minor, tested.patch + 1)\n"
        "    assert host.version.accepted.contains(version), host.harness\n"
        "    return str(version)\n"
        "print(json.dumps({\n"
        "    'catalog_version': catalog.version,\n"
        "    'catalog_digest': str(catalog.digest),\n"
        "    'hosts': {host.harness: host.support_tier.value for host in catalog.hosts},\n"
        "    'versions': {\n"
        "        host.harness: {\n"
        "            'accepted': str(host.version.accepted),\n"
        "            'tested': str(host.version.tested),\n"
        "            'stand_in': stand_in(host),\n"
        "        }\n"
        "        for host in catalog.hosts\n"
        "    },\n"
        "    'baseline_version': baseline.baseline_version,\n"
        "    'journeys': len(baseline.journeys),\n"
        "    'check_ids': sorted(baseline.check_ids),\n"
        "}))\n"
    )
    result = _require_success([str(python), "-c", program], cwd=root)
    loaded = json.loads(result.stdout)
    if not isinstance(loaded, dict):
        raise ConsumerSmokeError(f"packaged data probe returned {loaded!r}")
    return loaded


def _check_packaged_data(python: Path, *, root: Path) -> Mapping[str, object]:
    data = _packaged_data(python, root=root)
    if data["hosts"] != {"omp": "experimental", "claude-code": "experimental"}:
        raise ConsumerSmokeError(f"execution catalog hosts are {data['hosts']!r}")
    if data["baseline_version"] != "1" or not data["journeys"] or not data["check_ids"]:
        raise ConsumerSmokeError(f"acceptance baseline did not load as v1: {data!r}")
    _note(
        f"packaged execution catalog v{data['catalog_version']} and acceptance baseline "
        f"v{data['baseline_version']} load and verify their registered digests"
    )
    return data


def _stand_in_host(root: Path, harness: str, version: str) -> Path:
    """Write a host stand-in whose identity command prints a banner for ``version``.

    The smoke binds requests against this stand-in, never whatever host happens to be
    installed, so the result does not depend on the machine's own harness versions.
    """
    banner = f"omp v{version}" if harness == "omp" else f"{version} (Claude Code)"
    executable = root / f"stand-in-{harness}"
    executable.write_text(f"#!/bin/sh\necho '{banner}'\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _check_valid_pins(executable: Path, *, root: Path, packaged: Mapping[str, object]) -> None:
    catalog_version = str(packaged["catalog_version"])
    catalog_digest = str(packaged["catalog_digest"])
    versions = packaged["versions"]
    if not isinstance(versions, dict):
        raise ConsumerSmokeError(f"packaged host versions are {versions!r}")
    pins = (
        ("claude-code", _CLAUDE_MODEL, "high"),
        ("omp", _OMP_MODEL, "medium"),
    )
    for harness, model, effort in pins:
        output = root / f"request-{harness}.json"
        host_versions = versions[harness]
        accepted_range = str(host_versions["accepted"])
        tested = str(host_versions["tested"])
        observed = str(host_versions["stand_in"])
        if observed == tested:
            raise ConsumerSmokeError(
                f"the {harness} stand-in must run a version other than the tested {tested}"
            )
        command = [
            *_build_request_command(executable, root=root, output=output),
            "--harness-executable",
            str(_stand_in_host(root, harness, observed)),
            "--harness-provider",
            harness,
            "--model",
            model,
            "--effort",
            effort,
        ]
        result = _require_success(command, cwd=root)
        if not output.is_file():
            raise ConsumerSmokeError(f"a valid {harness} pin wrote no request\n{result.stdout}")
        try:
            printed = json.loads(result.stdout)
            document = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ConsumerSmokeError(f"{harness} request is not valid JSON: {error}") from error
        if not isinstance(document, dict) or not isinstance(printed, dict):
            raise ConsumerSmokeError(f"{harness} request or report is not a JSON object")
        if printed.get("request_digest") != document.get("request_digest"):
            raise ConsumerSmokeError(f"{harness} request digest differs from its printed report")
        check_bound_selection(
            document,
            harness=harness,
            model=model,
            effort=effort,
            catalog_version=catalog_version,
            catalog_digest=catalog_digest,
            accepted_versions=accepted_range,
            tested_version=tested,
            observed_version=observed,
        )
    _note("valid pins write requests whose selection is bound to the packaged catalog")


def _check_g4_fail_closed(executable: Path, *, root: Path) -> None:
    _require_g4_fail_closed(
        [
            str(executable),
            "gate",
            "status",
            "--gate",
            "G4",
            "--database",
            str(root / "ledger.db"),
            "--json",
        ],
        cwd=root,
    )
    _note("gate status --gate G4 remains fail-closed (exit 3, overall fail)")


_RECIPE_PROGRAM = (
    "import json\n"
    "from importlib import resources\n"
    "import enginery\n"
    "from enginery.adapters.oci import image\n"
    "root = resources.files('enginery.adapters.oci').joinpath('assets')\n"
    "print(json.dumps({\n"
    "    'package': enginery.__file__,\n"
    "    'worker': image.recipe_digest(),\n"
    "    'relay': image.relay_recipe_digest(),\n"
    "    'assets': {p.name: len(p.read_bytes()) for p in root.iterdir() if p.is_file()},\n"
    "}))\n"
)

_ENGINE_UNQUALIFIED_EXIT = 4

_ENGINE_PROGRAM = (
    "import sys\n"
    "from enginery.adapters.oci.engine import OciEngine\n"
    "try:\n"
    "    OciEngine().qualify()\n"
    "except Exception as error:\n"
    "    # qualify() names every missing requirement in `details` only for an engine it reached.\n"
    "    kind = 'unqualified' if 'missing' in getattr(error, 'details', {}) else 'unreachable'\n"
    "    print(kind + ': ' + type(error).__name__ + ': ' + str(error)[:300])\n"
    "    sys.exit(4 if kind == 'unqualified' else 3)\n"
    "print('qualified')\n"
)


def _check_runner_help(executable: Path, *, root: Path) -> None:
    for command in (["runner", "--help"], ["runner", "status", "--help"]):
        _require_success([str(executable), *command], cwd=root)
    operator = executable.with_name("enginery-operator")
    if not operator.is_file():
        raise ConsumerSmokeError(
            f"the `enginery-operator` entry point is not installed: {operator}"
        )
    result = _require_success([str(operator), "--help"], cwd=root)
    missing = [
        word for word in ("status", "pause", "backup", "restore") if word not in result.stdout
    ]
    if missing:
        raise ConsumerSmokeError(f"`enginery-operator --help` does not list {missing}")
    _note("runner --help, runner status --help and enginery-operator --help run")


def _check_stage1_runner_flags(executable: Path, *, root: Path) -> None:
    result = _require_success([str(executable), "stage1", "build-request", "--help"], cwd=root)
    missing = [
        flag
        for flag in ("--runner", "--runner-image", "--model-access")
        if flag not in result.stdout
    ]
    if missing:
        raise ConsumerSmokeError(f"`stage1 build-request --help` does not list {missing}")
    _note("stage1 build-request lists --runner, --runner-image and --model-access")


def _recipe_report(python: Path, *, root: Path, source: Path | None) -> Mapping[str, object]:
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if source is not None:
        environment["PYTHONPATH"] = str(source)
    result = subprocess.run(
        [str(python), "-c", _RECIPE_PROGRAM],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise ConsumerSmokeError(
            f"recipe probe failed\n{_describe([str(python), '-c', '<recipe probe>'], result)}"
        )
    report = json.loads(result.stdout)
    if not isinstance(report, dict):
        raise ConsumerSmokeError(f"recipe probe returned {report!r}")
    return report


def _check_runner_recipes(
    python: Path, *, root: Path, source_root: Path | None, wheel: Path | None
) -> list[str]:
    """The installed recipes are present, non-empty and identical to the source tree's."""
    installed = _recipe_report(python, root=root, source=None)
    assets = installed["assets"]
    if not isinstance(assets, dict):
        raise ConsumerSmokeError(f"installed recipe assets are {assets!r}")
    absent = [name for name in _RUNNER_ASSETS if not assets.get(name)]
    if absent:
        raise ConsumerSmokeError(f"the installed package lacks recipe files {absent}")
    _note(f"packaged worker and relay recipe files are present ({', '.join(_RUNNER_ASSETS)})")
    not_run: list[str] = []
    asset_root = None if source_root is None else source_root.joinpath(*_ASSET_DIRECTORY)
    if (
        source_root is None
        or asset_root is None
        or not asset_root.joinpath("Dockerfile.worker").is_file()
    ):
        message = "NOT RUN recipe digests equal the source tree's (no source tree beside scripts/)"
        print(message, flush=True)
        not_run.append(message)
    elif str(installed["package"]).startswith(str(source_root)):
        message = (
            "NOT RUN installed recipe digests equal the source tree's (the installed package is "
            "the source tree itself: an editable install)"
        )
        print(message, flush=True)
        not_run.append(message)
    else:
        source = _recipe_report(python, root=root, source=source_root)
        if not str(source["package"]).startswith(str(source_root)):
            raise ConsumerSmokeError("the source-tree recipe probe did not import the source tree")
        for key in ("worker", "relay"):
            if installed[key] != source[key]:
                raise ConsumerSmokeError(
                    f"the installed {key} recipe digest {installed[key]} differs from the source "
                    f"tree's {source[key]}"
                )
        _note(
            f"installed recipe digests equal the source tree's (worker {installed['worker']}, "
            f"relay {installed['relay']})"
        )
    if wheel is not None:
        compared = 0
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            for name in _RUNNER_ASSETS:
                member = "/".join((*_ASSET_DIRECTORY, name))
                if member not in names:
                    raise ConsumerSmokeError(f"the wheel {wheel.name} lacks {member}")
                if asset_root is not None and asset_root.joinpath(name).is_file():
                    packaged = hashlib.sha256(archive.read(member)).hexdigest()
                    if packaged != _sha256(asset_root / name):
                        raise ConsumerSmokeError(f"{member} in the wheel differs from the source")
                    compared += 1
        if compared == len(_RUNNER_ASSETS):
            _note(
                f"the wheel {wheel.name} carries every recipe file, byte-identical to the source "
                "tree"
            )
        else:
            _note(f"the wheel {wheel.name} carries every recipe file")
            message = (
                f"NOT RUN wheel recipe files are byte-identical to the source tree (compared "
                f"{compared} of {len(_RUNNER_ASSETS)}: no source tree beside scripts/)"
            )
            print(message, flush=True)
            not_run.append(message)
    return not_run


def _check_runner_engine(executable: Path, python: Path, *, root: Path) -> list[str]:
    """Against a reachable container engine, `runner status` must hold an absent image id."""
    probe = subprocess.run(
        [str(python), "-c", _ENGINE_PROGRAM],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        reason = (probe.stdout.strip() or probe.stderr.strip() or "no answer")[:300]
        if probe.returncode == _ENGINE_UNQUALIFIED_EXIT or reason.startswith("unqualified:"):
            state = "a reachable engine that cannot enforce the isolated profile (unqualified)"
        else:
            state = "no reachable container engine (unreachable)"
        message = (
            f"engine NOT RUN runner status against a qualified container engine: {state}: {reason}"
        )
        print(message, flush=True)
        return [message]
    result = _run([str(executable), "runner", "status", "--image", _ABSENT_IMAGE], cwd=root)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ConsumerSmokeError(
            f"`runner status` printed no report\n{_describe([str(executable)], result)}"
        ) from error
    hold = report.get("hold") if isinstance(report, dict) else None
    if (
        result.returncode != 5
        or not isinstance(report, dict)
        or report.get("admitted") is not False
        or not isinstance(hold, dict)
        or hold.get("reason") != "runner_unqualified"
    ):
        raise ConsumerSmokeError(
            f"`runner status` did not hold an absent image on a reachable engine\n{result.stdout}"
        )
    print(
        "engine ok runner status holds an absent image id on the reachable engine (exit 5)",
        flush=True,
    )
    return []


def exercise_surface(
    *,
    executable: Path,
    python: Path,
    root: Path,
    version: str,
    wheel: Path | None = None,
    source_root: Path | None = _SOURCE_ROOT,
    require_engine: bool = False,
) -> list[str]:
    """Run every consumer-surface check against one installed `enginery`.

    Returns the checks that could not run (each already printed as ``NOT RUN``); a check that
    cannot run is never counted as a pass, and ``require_engine`` turns one into a failure.
    """
    _check_version(executable, root=root, version=version)
    _check_doctor(executable, root=root, version=version)
    _check_help(executable, root=root)
    _check_rejected_pins(executable, root=root)
    packaged = _check_packaged_data(python, root=root)
    _check_valid_pins(executable, root=root, packaged=packaged)
    _check_g4_fail_closed(executable, root=root)
    _check_runner_help(executable, root=root)
    _check_stage1_runner_flags(executable, root=root)
    not_run = _check_runner_recipes(python, root=root, source_root=source_root, wheel=wheel)
    not_run += _check_runner_engine(executable, python, root=root)
    unrun_engine = [item for item in not_run if item.startswith("engine NOT RUN")]
    if require_engine and unrun_engine:
        kind = "unqualified" if "(unqualified)" in unrun_engine[0] else "unreachable"
        raise ConsumerSmokeError(f"--require-engine: the container engine was {kind}")
    return not_run


INCOMPLETE_EXIT = 3


def report_outcome(not_run: list[str]) -> int:
    """``PASS`` only when every check ran; otherwise ``INCOMPLETE`` naming each check not run."""
    if not_run:
        print(
            f"INCOMPLETE release-consumer-smoke: {len(not_run)} check(s) NOT RUN, so this is not "
            "a pass:",
            flush=True,
        )
        for item in not_run:
            print(f"  - {item}", flush=True)
        return INCOMPLETE_EXIT
    print("PASS release-consumer-smoke")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--wheel", type=Path)
    source.add_argument("--sdist", type=Path)
    source.add_argument(
        "--package", help="published distribution requirement, for example enginery==0.6.0"
    )
    parser.add_argument(
        "--require-engine",
        action="store_true",
        help="fail, rather than report NOT RUN, when no qualified container engine is reachable",
    )
    args = parser.parse_args(argv)
    version = expected_version(wheel=args.wheel, sdist=args.sdist, package=args.package)
    artifact: Path | None = args.wheel or args.sdist
    if artifact is not None and not artifact.is_file():
        raise ConsumerSmokeError(f"artifact does not exist: {artifact}")
    with tempfile.TemporaryDirectory(prefix="enginery-consumer-") as temporary_directory:
        root = Path(temporary_directory)
        venv = root / "venv"
        _require_success([sys.executable, "-m", "venv", str(venv)], cwd=root)
        python = venv / "bin" / "python"
        install = _installer(python)
        install.append(str(artifact.resolve()) if artifact is not None else args.package)
        _require_success(install, cwd=root)
        print(
            f"installed {artifact.name if artifact is not None else args.package} "
            f"sha256={_sha256(artifact) if artifact is not None else 'n/a'} "
            f"installer={install[0]} python={platform.python_version()} "
            f"platform={platform.platform()}",
            flush=True,
        )
        not_run = exercise_surface(
            executable=venv / "bin" / "enginery",
            python=python,
            root=root,
            version=version,
            wheel=args.wheel,
            require_engine=args.require_engine,
        )
    return report_outcome(not_run)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConsumerSmokeError as error:
        print(f"RELEASE-CONSUMER SMOKE FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
