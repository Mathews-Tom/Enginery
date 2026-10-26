#!/usr/bin/env python3
"""Install and switch isolated Enginery wheel runtimes without importing Enginery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from typing import cast


class RefusedError(Exception):
    """An operation cannot safely proceed with the supplied inputs."""


class UsageError(Exception):
    """An input or runtime-manager configuration is invalid."""


@dataclass(frozen=True)
class State:
    current: str | None = None
    history: tuple[str, ...] = ()


def _xdg_home(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    path = Path(value) if value else default
    if not path.is_absolute():
        raise UsageError(f"{variable} must be an absolute path; refusing {str(path)!r}")
    return path


def _locations(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    home = Path(os.environ.get("HOME", ""))
    if not home.is_absolute():
        raise UsageError("HOME must be an absolute path")
    data = _xdg_home("XDG_DATA_HOME", home / ".local" / "share")
    state = _xdg_home("XDG_STATE_HOME", home / ".local" / "state")
    _xdg_home("XDG_CONFIG_HOME", home / ".config")
    root = getattr(args, "root", None) or data / "enginery" / "runtimes"
    bin_dir = getattr(args, "bin_dir", None) or home / ".local" / "bin"
    return root.resolve(), bin_dir.resolve(), state / "enginery" / "projects"


def _runtime(root: Path, runtime_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_]*-[0-9a-f]{12}", runtime_id):
        raise RefusedError(f"Invalid runtime identifier in state: {runtime_id!r}")
    return root / runtime_id


def _load_state(root: Path) -> State:
    path = root / "state.json"
    if not path.exists():
        return State()
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RefusedError(f"Invalid runtime state: {path}")
    current = raw.get("current")
    history = raw.get("history")
    if not isinstance(current, str) or not isinstance(history, list):
        raise RefusedError(f"Invalid runtime state: {path}")
    if not all(isinstance(item, str) for item in history):
        raise RefusedError(f"Invalid runtime history: {path}")
    entries = [current, *cast(list[str], history)]
    for runtime_id in entries:
        _runtime(root, runtime_id)
    if len(set(entries)) != len(entries):
        raise RefusedError(f"Duplicate runtime identifiers in state: {path}")
    return State(current, tuple(cast(list[str], history)))


def _receipt(root: Path, runtime_id: str) -> dict[str, str]:
    runtime = _runtime(root, runtime_id)
    raw: object = json.loads((runtime / "receipt.json").read_text(encoding="utf-8"))
    fields = ("wheel", "sha256", "version", "installed_at", "python")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(key), str) for key in fields):
        raise RefusedError(f"Invalid runtime receipt: {runtime / 'receipt.json'}")
    return {**cast(dict[str, str], raw), "id": runtime_id, "path": str(runtime)}


def _child_environment() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }


def _run(command: list[str]) -> str:
    result = subprocess.run(
        command, check=True, capture_output=True, text=True, env=_child_environment()
    )
    return result.stdout.strip()


def _wheel_details(wheel: Path) -> tuple[str, str]:
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise UsageError(f"Expected a wheel file: {wheel}")
    parts = wheel.stem.split("-")
    if len(parts) not in {5, 6} or parts[0].lower() != "enginery":
        raise UsageError(f"Expected an enginery wheel filename: {wheel.name}")
    with wheel.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_paths = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_paths) != 1:
                raise UsageError("Wheel must contain exactly one distribution METADATA file")
            metadata = BytesParser().parsebytes(archive.read(metadata_paths[0]))
    except zipfile.BadZipFile as error:
        raise UsageError(f"Invalid wheel archive: {wheel}") from error
    version = str(metadata.get("Version", ""))
    name = str(metadata.get("Name", ""))
    if name.lower() != "enginery" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_]*", version):
        raise UsageError("Wheel METADATA must identify enginery and a safe version")
    if version.replace("_", "-").lower() != parts[1].replace("_", "-").lower():
        raise UsageError("Wheel filename version does not match METADATA Version")
    return version, digest


def _confirm(version: str, digest: str, accepted: str | None) -> None:
    print(f"Version: {version}\nSHA256: {digest}", flush=True)
    if accepted is not None:
        if accepted.lower() != digest:
            raise RefusedError("--yes-sha256 does not match the wheel SHA256; nothing changed")
        return
    if not sys.stdin.isatty():
        raise RefusedError(
            "Non-interactive install requires --yes-sha256 matching the wheel SHA256"
        )
    try:
        answer = input("Install this wheel? Type exactly yes: ")
    except EOFError as error:
        raise RefusedError("Install was not confirmed; nothing changed") from error
    if answer != "yes":
        raise RefusedError("Install was not confirmed; nothing changed")


def _temporary_bytes(directory: Path, contents: bytes) -> Path:
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".runtime-", delete=False) as stream:
        path = Path(stream.name)
        try:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return path


def _require_managed_shim(root: Path, bin_dir: Path) -> None:
    """Refuse to replace an ``enginery`` entry this launcher did not create."""
    shim = bin_dir / "enginery"
    if not shim.is_symlink():
        if shim.exists():
            raise RefusedError(
                f"{shim} is not managed by this launcher; remove it or choose another "
                "--bin-dir. Nothing was changed."
            )
        return
    target = Path(os.path.realpath(shim))
    managed = Path(os.path.realpath(root))
    if not target.is_relative_to(managed):
        raise RefusedError(
            f"{shim} points to {os.readlink(shim)}, which this launcher does not manage; "
            "remove it (for a uv tool install: `uv tool uninstall enginery`) or choose "
            "another --bin-dir. Nothing was changed."
        )


def _activate(root: Path, bin_dir: Path, state: State) -> None:
    """Replace each public entry atomically, restoring state if shim replacement fails."""
    assert state.current is not None
    executable = _runtime(root, state.current) / "venv" / "bin" / "enginery"
    if not executable.is_file():
        raise RefusedError(f"Runtime executable is missing: {executable}")
    state_path = root / "state.json"
    old_state = state_path.read_bytes() if state_path.exists() else None
    contents = json.dumps({"current": state.current, "history": state.history}, indent=2) + "\n"
    bin_dir.mkdir(parents=True, exist_ok=True)
    staged_state = _temporary_bytes(root, contents.encode())
    staged_shim: Path | None = None
    try:
        # Reserve a unique name on the shim's filesystem, then replace it with a symlink.
        staged_shim = _temporary_bytes(bin_dir, b"")
        staged_shim.unlink()
        staged_shim.symlink_to(executable)
        os.replace(staged_state, state_path)
        try:
            os.replace(staged_shim, bin_dir / "enginery")
        except BaseException:
            if old_state is None:
                state_path.unlink()
            else:
                restored = _temporary_bytes(root, old_state)
                try:
                    os.replace(restored, state_path)
                finally:
                    restored.unlink(missing_ok=True)
            raise
    finally:
        staged_state.unlink(missing_ok=True)
        if staged_shim is not None:
            staged_shim.unlink(missing_ok=True)


def _install(args: argparse.Namespace, root: Path, bin_dir: Path) -> None:
    source = args.wheel.resolve()
    if not source.is_file() or source.suffix != ".whl":
        raise UsageError(f"Expected a wheel file: {args.wheel}")
    _require_managed_shim(root, bin_dir)
    with tempfile.TemporaryDirectory(prefix="enginery-runtime-") as directory:
        wheel = Path(directory) / args.wheel.name
        shutil.copyfile(source, wheel)
        _install_copy(args, root, bin_dir, wheel)


def _install_copy(args: argparse.Namespace, root: Path, bin_dir: Path, wheel: Path) -> None:
    """Confirm and install only the private copy, never the mutable input wheel."""
    version, digest = _wheel_details(wheel)
    _confirm(version, digest, args.yes_sha256)
    previous = _load_state(root)
    runtime_id = f"{version}-{digest[:12]}"
    runtime = _runtime(root, runtime_id)
    root.mkdir(parents=True, exist_ok=True)
    try:
        runtime.mkdir()
    except FileExistsError as error:
        raise RefusedError(f"Runtime already exists; retaining it unchanged: {runtime}") from error
    try:
        installed_wheel = runtime / wheel.name
        shutil.move(str(wheel), installed_wheel)
        venv = runtime / "venv"
        _run([sys.executable, "-I", "-m", "venv", str(venv)])
        python = venv / "bin" / "python"
        command = [str(python), "-I", "-m", "pip", "--isolated", "install"]
        if args.no_index:
            command.append("--no-index")
        if args.find_links is not None:
            command.extend(["--find-links", str(args.find_links.resolve())])
        command.append(str(installed_wheel))
        _run(command)
        actual = _run([str(venv / "bin" / "enginery"), "--version"])
        if actual != f"enginery {version}":
            raise RefusedError(
                f"Installed runtime version mismatch: expected {version}, got {actual!r}"
            )
        receipt = {
            "wheel": wheel.name,
            "sha256": digest,
            "version": version,
            "installed_at": datetime.now(UTC).isoformat(),
            "python": _run([str(python), "-I", "--version"]),
        }
        (runtime / "receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )
        history = (() if previous.current is None else (previous.current,)) + previous.history
        _activate(root, bin_dir, State(runtime_id, history))
    except BaseException:
        shutil.rmtree(runtime)
        raise
    print(f"Installed {runtime_id}; shim: {bin_dir / 'enginery'}")


def _list(root: Path, *, as_json: bool) -> None:
    state = _load_state(root)
    current = None if state.current is None else _receipt(root, state.current)
    previous = [_receipt(root, runtime_id) for runtime_id in state.history]
    if as_json:
        print(json.dumps({"current": current, "previous": previous}, indent=2))
        return
    if current is None:
        print("No installed runtimes.")
    for label, receipt in [("current", current), *(("previous", item) for item in previous)]:
        if receipt is not None:
            print(
                f"{label}: {receipt['version']} sha256={receipt['sha256']} "
                f"installed_at={receipt['installed_at']} python={receipt['python']} "
                f"wheel={receipt['wheel']} path={receipt['path']}"
            )


def _ledger_version(ledger: Path) -> int:
    try:
        with closing(sqlite3.connect(f"{ledger.as_uri()}?mode=ro", uri=True)) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if table is None:
                return 0
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.Error as error:
        raise RefusedError(f"Cannot inspect ledger {ledger} read-only: {error}") from error
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _rollback(args: argparse.Namespace, root: Path, bin_dir: Path, projects: Path) -> None:
    state = _load_state(root)
    if state.current is None or not state.history:
        raise RefusedError("No previous runtime is available for rollback")
    _require_managed_shim(root, bin_dir)
    target = state.history[0]
    _receipt(root, target)
    python = _runtime(root, target) / "venv" / "bin" / "python"
    supported = int(
        _run(
            [
                str(python),
                "-I",
                "-c",
                "from enginery.ledger.schema import MIGRATIONS; "
                "print(MIGRATIONS[-1].version if MIGRATIONS else 0)",
            ]
        )
    )
    ledgers = {path.resolve() for path in args.ledger}
    ledgers.update(path.resolve() for path in projects.glob("*/ledger.sqlite"))
    # Inspect every supplied and discovered database before any switch, even on refusal.
    problems: list[str] = []
    for ledger in sorted(ledgers):
        try:
            current = _ledger_version(ledger)
        except RefusedError as error:
            problems.append(str(error))
            continue
        if current > supported:
            remedy = (
                f"{shlex.quote(str(python.parent / 'enginery'))} operator restore "
                f"--database {shlex.quote(str(ledger))} --backup <dir> --force-unpaused"
            )
            problems.append(
                f"Ledger {ledger}: current schema {current}, "
                f"previous runtime supports {supported}. "
                "Pause and drain dispatch/workers first, then restore the pre-upgrade backup "
                f"with the PREVIOUS runtime (it cannot read the newer live ledger): {remedy}; "
                "then retry rollback."
            )
    if problems:
        raise RefusedError("\n".join(problems))
    _activate(root, bin_dir, State(target, (state.current, *state.history[1:])))
    print(f"Rolled back to {target}; retained {state.current}")


def _parser() -> argparse.ArgumentParser:
    locations = argparse.ArgumentParser(add_help=False)
    locations.add_argument("--root", type=Path, default=argparse.SUPPRESS)
    locations.add_argument("--bin-dir", type=Path, default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[locations])
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser(
        "install", parents=[locations], help="Install a confirmed wheel"
    )
    install.add_argument("wheel", type=Path, metavar="WHEEL")
    install.add_argument("--yes-sha256", metavar="HEX")
    install.add_argument("--find-links", type=Path, metavar="DIR")
    install.add_argument("--no-index", action="store_true")
    listing = subparsers.add_parser(
        "list", parents=[locations], help="List current and previous runtimes"
    )
    listing.add_argument("--json", action="store_true")
    rollback = subparsers.add_parser(
        "rollback", parents=[locations], help="Switch to the previous runtime"
    )
    rollback.add_argument("--ledger", type=Path, action="append", default=[], metavar="PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        root, bin_dir, projects = _locations(args)
        if args.command == "install":
            _install(args, root, bin_dir)
        elif args.command == "list":
            _list(root, as_json=args.json)
        else:
            _rollback(args, root, bin_dir, projects)
    except UsageError as error:
        parser.error(str(error))
    except RefusedError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 5
    except subprocess.CalledProcessError as error:
        print(f"Runtime command failed: {error.cmd}\n{error.stderr}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"Runtime manager failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
