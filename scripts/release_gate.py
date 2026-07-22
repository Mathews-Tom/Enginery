#!/usr/bin/env python3
"""Pre-tag release gate for one canonical `enginery` version.

Verifies version consistency across `pyproject.toml`, `CHANGELOG.md`, and
the CLI regression test, then builds fresh artifacts (never trusting a
stale `dist/` left over from a prior commit), checks them with `twine`,
records their `sha256` hashes as release evidence, and -- unless
`--skip-install-smoke` is given -- clean-installs the built wheel into an
isolated virtual environment and smoke-tests `enginery --version` and
`enginery doctor`.

Fails closed: any check failure is a non-zero exit and a clear message,
never a silently skipped or partially-passing gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from check_docs_currency import DocsCurrencyError
from check_docs_currency import run_check as _run_docs_currency_check

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"
VERSION_TEST_PATH = REPO_ROOT / "tests" / "cli" / "test_version.py"
DIST_DIR = REPO_ROOT / "dist"

_CHANGELOG_HEADING = re.compile(r"^##\s+\[?(?P<version>[^\]\s]+)\]?", re.MULTILINE)


class ReleaseGateError(RuntimeError):
    """A fatal, fail-closed release-gate check failure."""


def _load_pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _check_pyproject_version(expected_version: str) -> None:
    pyproject = _load_pyproject()
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ReleaseGateError("pyproject.toml has no [project] table")
    actual = project.get("version")
    if actual != expected_version:
        raise ReleaseGateError(
            f"pyproject.toml [project].version is {actual!r}, expected {expected_version!r}"
        )
    if project.get("name") != "enginery":
        raise ReleaseGateError(
            f"pyproject.toml [project].name is {project.get('name')!r}, expected 'enginery'"
        )


def _check_changelog_entry(expected_version: str) -> None:
    if not CHANGELOG_PATH.is_file():
        raise ReleaseGateError(f"{CHANGELOG_PATH.relative_to(REPO_ROOT)} does not exist")
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    headings = list(_CHANGELOG_HEADING.finditer(text))
    matching = [heading for heading in headings if heading.group("version") == expected_version]
    if not matching:
        raise ReleaseGateError(
            f"CHANGELOG.md has no '## {expected_version}' (or '## [{expected_version}]') entry"
        )
    heading = matching[0]
    following = headings[headings.index(heading) + 1] if heading in headings[:-1] else None
    body_start = heading.end()
    body_end = following.start() if following is not None else len(text)
    body = text[body_start:body_end].strip()
    if not body:
        raise ReleaseGateError(f"CHANGELOG.md's {expected_version} entry has no body content")


def _check_version_test(expected_version: str) -> None:
    if not VERSION_TEST_PATH.is_file():
        raise ReleaseGateError(f"{VERSION_TEST_PATH.relative_to(REPO_ROOT)} does not exist")
    text = VERSION_TEST_PATH.read_text(encoding="utf-8")
    if f'== "{expected_version}"' not in text:
        raise ReleaseGateError(
            f"{VERSION_TEST_PATH.relative_to(REPO_ROOT)} does not assert version "
            f"{expected_version!r} (still pinned to a different literal)"
        )


def _run(command: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _build_fresh_artifacts(expected_version: str) -> tuple[Path, Path]:
    """Remove any stale ``dist/`` and build fresh wheel + sdist for the exact worktree."""
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    result = _run(["uv", "build"])
    if result.returncode != 0:
        raise ReleaseGateError(f"'uv build' failed:\n{result.stdout}\n{result.stderr}")

    normalized = expected_version.replace("-", "_")
    wheels = sorted(DIST_DIR.glob(f"enginery-{normalized}-*.whl"))
    sdists = sorted(DIST_DIR.glob(f"enginery-{normalized}.tar.gz"))
    if len(wheels) != 1:
        raise ReleaseGateError(
            f"expected exactly one matching wheel in dist/, found {[w.name for w in wheels]}"
        )
    if len(sdists) != 1:
        raise ReleaseGateError(
            f"expected exactly one matching sdist in dist/, found {[s.name for s in sdists]}"
        )
    return wheels[0], sdists[0]


def _twine_check(*artifacts: Path) -> None:
    result = _run(["uvx", "twine", "check", *(str(path) for path in artifacts)])
    if result.returncode != 0 or "PASSED" not in result.stdout:
        raise ReleaseGateError(f"'twine check' failed:\n{result.stdout}\n{result.stderr}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _install_smoke(wheel: Path, expected_version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="enginery-release-gate-") as tmp:
        venv_dir = Path(tmp) / "venv"
        create = _run(["uv", "venv", str(venv_dir), "--python", "3.12"])
        if create.returncode != 0:
            raise ReleaseGateError(f"'uv venv' failed:\n{create.stdout}\n{create.stderr}")

        python = venv_dir / "bin" / "python"
        install = _run(["uv", "pip", "install", "--python", str(python), str(wheel)])
        if install.returncode != 0:
            raise ReleaseGateError(
                f"clean-install of {wheel.name} failed:\n{install.stdout}\n{install.stderr}"
            )

        enginery_cli = venv_dir / "bin" / "enginery"
        version_result = _run([str(enginery_cli), "--version"], cwd=Path(tmp))
        if version_result.returncode != 0 or expected_version not in version_result.stdout:
            raise ReleaseGateError(
                f"'enginery --version' did not report {expected_version!r}: "
                f"{version_result.stdout!r} {version_result.stderr!r}"
            )

        doctor_result = _run([str(enginery_cli), "doctor"], cwd=Path(tmp))
        if doctor_result.returncode != 0:
            raise ReleaseGateError(
                f"'enginery doctor' failed on a clean install:\n"
                f"{doctor_result.stdout}\n{doctor_result.stderr}"
            )


def _check_docs_currency() -> None:
    try:
        _run_docs_currency_check(repo_root=REPO_ROOT)
    except DocsCurrencyError as error:
        raise ReleaseGateError(f"docs-currency check failed: {error}") from error


def run_gate(*, version: str, skip_install_smoke: bool) -> dict[str, object]:
    _check_docs_currency()
    _check_pyproject_version(version)
    _check_changelog_entry(version)
    _check_version_test(version)

    wheel, sdist = _build_fresh_artifacts(version)
    _twine_check(wheel, sdist)

    artifact_hashes = {wheel.name: _sha256(wheel), sdist.name: _sha256(sdist)}

    if not skip_install_smoke:
        _install_smoke(wheel, version)

    return {
        "version": version,
        "artifacts": artifact_hashes,
        "install_smoke": "not-run" if skip_install_smoke else "passed",
        "docs_currency": "passed",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="canonical version being released")
    parser.add_argument(
        "--skip-install-smoke",
        action="store_true",
        help="skip the isolated clean-install smoke test (faster local iteration only)",
    )
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=None,
        help="optional path to also write the JSON evidence report",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        evidence = run_gate(version=args.version, skip_install_smoke=args.skip_install_smoke)
    except ReleaseGateError as error:
        print(f"RELEASE GATE FAILED: {error}", file=sys.stderr)
        return 1

    rendered = json.dumps(evidence, indent=2, sort_keys=True)
    print(rendered)
    if args.evidence_out is not None:
        args.evidence_out.write_text(rendered + "\n", encoding="utf-8")
    print(f"PASS release-gate version={args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
