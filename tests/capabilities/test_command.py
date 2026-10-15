from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from enginery.application.delivery_ports import PackageFile
from enginery.capabilities.command import CommandResult, run_package_command
from enginery.capabilities.compatibility import CompatibilityReceipt, CompatibilityStatus
from enginery.capabilities.errors import (
    CapabilityApprovalRequiredError,
    CapabilityCommandFailedError,
    CapabilityCommandNotRunnableError,
    CapabilityCommandOutputError,
    CapabilityCommandTimeoutError,
    CapabilityIncompatibleError,
    CapabilityInputSchemaError,
    CapabilityIntegrityError,
    CapabilityLicenseHoldError,
    CapabilityNotFoundError,
    CapabilityNotInstalledError,
)
from enginery.capabilities.install import OWNED_MANIFEST_NAME, install_lock, uninstall_install
from enginery.capabilities.lock import CapabilityLock
from enginery.capabilities.manifest import CommandSpec, PackageKind
from enginery.capabilities.materialize import digest_path, load_verified_closure
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.capabilities.sources import CapabilitySource
from tests.support.capabilities import lock_of, package_files, project_source, user_source

INTERPRETERS = {"python3": sys.executable}
NAME = "tool"


def _script(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


ECHO = _script(
    "import json, sys",
    "json.dump({'echo': json.load(sys.stdin)}, sys.stdout, sort_keys=True)",
)


@dataclass(frozen=True)
class Installed:
    lock: CapabilityLock
    install_root: Path
    capabilities_root: Path

    def run(
        self,
        payload: object,
        *,
        name: str = NAME,
        lock: CapabilityLock | None = None,
        approved: frozenset[str] = frozenset(),
    ) -> CommandResult:
        return run_package_command(
            self.lock if lock is None else lock,
            name,
            payload,
            install_root=self.install_root,
            capabilities_root=self.capabilities_root,
            approved_names=approved,
            interpreter_paths=INTERPRETERS,
        )

    @property
    def directory(self) -> Path:
        entry = self.lock.get(NAME)
        assert entry is not None
        return digest_path(self.capabilities_root, entry.digest)


def _resolve(source: CapabilitySource) -> CapabilityLock:
    return CapabilityResolver([source]).resolve([CapabilityRequest(NAME, "1")])


def _install(
    tmp_path: Path,
    files: Sequence[PackageFile],
    *,
    source: CapabilitySource | None = None,
    approved: frozenset[str] = frozenset(),
) -> Installed:
    resolved = CapabilityResolver([source or project_source(files)]).resolve_with_files(
        [CapabilityRequest(NAME, "1")]
    )
    install_root = tmp_path / "install"
    capabilities_root = tmp_path / "capabilities"
    install_lock(
        resolved,
        install_root=install_root,
        capabilities_root=capabilities_root,
        approved_names=approved,
    )
    return Installed(resolved.lock, install_root, capabilities_root)


def _command_package(
    script: bytes,
    *,
    command: CommandSpec | None = None,
    extra: dict[str, bytes] | None = None,
    license_id: str | None = "MIT",
) -> tuple[PackageFile, ...]:
    return package_files(
        NAME,
        kind=PackageKind.COMMAND,
        script=script,
        command=command,
        extra=extra,
        license_id=license_id,
    )


def _marker_script(marker_key: str = "marker") -> bytes:
    return _script(
        "import json, sys",
        "payload = json.load(sys.stdin)",
        f"open(payload[{marker_key!r}], 'w').close()",
        "print('{}')",
    )


def _spec(
    *,
    timeout_seconds: int = 10,
    max_output_bytes: int = 64 * 1024,
    schema: dict[str, object] | None = None,
) -> CommandSpec:
    return CommandSpec.create(
        argv=("python3", "scripts/run.py"),
        input_schema=schema or {"type": "object"},
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


# --- success -----------------------------------------------------------------------------------


def test_run_returns_canonical_output_and_parsed_output(tmp_path: Path) -> None:
    script = _script('print(r\'{"b": 1, "a": {"d": 2, "c": [1, "caf\\u00e9"]}}\')')
    installed = _install(tmp_path, _command_package(script))

    result = installed.run({})

    assert result.canonical_output == r'{"a":{"c":[1,"caf\u00e9"],"d":2},"b":1}'
    assert result.output == {"b": 1, "a": {"d": 2, "c": [1, "caf\u00e9"]}}
    entry = installed.lock.get(NAME)
    assert entry is not None
    assert (result.name, result.version, result.closure_digest) == (NAME, "1", entry.digest)
    assert result.argv[0] == sys.executable
    assert result.argv[1:] == ("scripts/run.py",)


def test_run_passes_the_payload_as_canonical_json_on_stdin(tmp_path: Path) -> None:
    script = _script("import json, sys", "print(json.dumps({'raw': sys.stdin.read()}))")
    installed = _install(tmp_path, _command_package(script))

    result = installed.run({"b": 1, "a": [1, 2]})

    assert result.output == {"raw": '{"a":[1,2],"b":1}'}


def test_run_captures_stderr_as_diagnostics_even_on_success(tmp_path: Path) -> None:
    script = _script("import sys", "sys.stderr.write('note: all good\\n')", "print('[1, 2]')")
    installed = _install(tmp_path, _command_package(script))

    result = installed.run({})

    assert result.output == [1, 2]
    assert result.stderr == "note: all good\n"
    assert result.duration_seconds >= 0


def test_run_executes_in_the_materialized_directory_with_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(
        "import json, os",
        "print(json.dumps({'cwd': os.getcwd(), 'env': sorted(os.environ)}))",
    )
    installed = _install(tmp_path, _command_package(script))
    monkeypatch.setenv("ENGINERY_TEST_SECRET", "leak")

    result = installed.run({})

    assert isinstance(result.output, dict)
    assert os.path.realpath(str(result.output["cwd"])) == os.path.realpath(installed.directory)
    environment = result.output["env"]
    assert isinstance(environment, list)
    assert "ENGINERY_TEST_SECRET" not in environment
    assert "PATH" in environment
    assert "PYTHONDONTWRITEBYTECODE" in environment


# --- schema gate -------------------------------------------------------------------------------


def test_schema_violation_is_rejected_before_any_process_is_spawned(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    command = _spec(
        schema={
            "type": "object",
            "required": ["marker"],
            "additionalProperties": False,
            "properties": {"marker": {"type": "string"}},
        }
    )
    installed = _install(tmp_path, _command_package(_marker_script(), command=command))

    with pytest.raises(CapabilityInputSchemaError) as caught:
        installed.run({"marker": str(marker), "extra": 1})

    assert caught.value.details["path"] == "$.extra"
    assert not marker.exists()


def test_a_conforming_payload_does_spawn_the_process(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    command = _spec(schema={"type": "object", "required": ["marker"]})
    installed = _install(tmp_path, _command_package(_marker_script(), command=command))

    installed.run({"marker": str(marker)})

    assert marker.exists()


def test_a_non_object_payload_is_rejected_by_an_object_schema(tmp_path: Path) -> None:
    installed = _install(tmp_path, _command_package(ECHO))

    with pytest.raises(CapabilityInputSchemaError):
        installed.run(["not", "an", "object"])


# --- bounded execution -------------------------------------------------------------------------


def test_timeout_raises_and_kills_the_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    script = _script(
        "import json, os, sys, time",
        "open(json.load(sys.stdin)['pid_file'], 'w').write(str(os.getpid()))",
        "time.sleep(60)",
    )
    installed = _install(tmp_path, _command_package(script, command=_spec(timeout_seconds=1)))

    started = time.monotonic()
    with pytest.raises(CapabilityCommandTimeoutError) as caught:
        installed.run({"pid_file": str(pid_file)})

    assert time.monotonic() - started < 15
    assert caught.value.details["timeout_seconds"] == 1
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    assert not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_nonzero_exit_raises_with_stderr_in_details(tmp_path: Path) -> None:
    script = _script("import sys", "sys.stderr.write('boom happened')", "sys.exit(3)")
    installed = _install(tmp_path, _command_package(script))

    with pytest.raises(CapabilityCommandFailedError) as caught:
        installed.run({})

    assert caught.value.details["exit_code"] == 3
    assert "boom happened" in str(caught.value.details["stderr"])


def test_a_non_zero_exit_fails_even_when_stdout_is_valid_json(tmp_path: Path) -> None:
    script = _script("import sys", "print('{}')", "sys.exit(1)")
    installed = _install(tmp_path, _command_package(script))

    with pytest.raises(CapabilityCommandFailedError):
        installed.run({})


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_script("print('not json at all')"), id="not-json"),
        pytest.param(_script("pass"), id="empty"),
        pytest.param(_script("print('{}')", "print('{}')"), id="two-documents"),
        pytest.param(_script("print('{} trailing')"), id="trailing-text"),
        pytest.param(_script("print('NaN')"), id="non-finite"),
        pytest.param(
            _script("import sys", "sys.stdout.buffer.write(b'\\xff\\xfe')"), id="not-utf8"
        ),
    ],
)
def test_stdout_that_is_not_one_json_document_is_rejected(tmp_path: Path, body: bytes) -> None:
    installed = _install(tmp_path, _command_package(body))

    with pytest.raises(CapabilityCommandOutputError) as caught:
        installed.run({})

    assert "single valid JSON document" in str(caught.value)


def test_stdout_over_the_output_limit_is_rejected(tmp_path: Path) -> None:
    script = _script("import json", "print(json.dumps('x' * 5000))")
    installed = _install(tmp_path, _command_package(script, command=_spec(max_output_bytes=100)))

    with pytest.raises(CapabilityCommandOutputError) as caught:
        installed.run({})

    assert caught.value.details["max_output_bytes"] == 100


def test_stdout_within_the_output_limit_is_accepted(tmp_path: Path) -> None:
    script = _script("import json", "print(json.dumps('x' * 20))")
    installed = _install(tmp_path, _command_package(script, command=_spec(max_output_bytes=100)))

    assert installed.run({}).output == "x" * 20


def test_stderr_over_the_output_limit_is_rejected(tmp_path: Path) -> None:
    script = _script("import sys", "sys.stderr.write('e' * 5000)", "print('{}')")
    installed = _install(tmp_path, _command_package(script, command=_spec(max_output_bytes=100)))

    with pytest.raises(CapabilityCommandOutputError):
        installed.run({})


# --- not runnable, unknown, not installed ------------------------------------------------------


def test_an_instruction_package_is_not_runnable(tmp_path: Path) -> None:
    files = package_files(NAME)
    installed = _install(tmp_path, files)

    with pytest.raises(CapabilityCommandNotRunnableError) as caught:
        installed.run({})

    assert caught.value.details["kind"] == "instruction"


def test_a_name_outside_the_lock_is_not_found(tmp_path: Path) -> None:
    installed = _install(tmp_path, _command_package(ECHO))

    with pytest.raises(CapabilityNotFoundError):
        installed.run({}, name="other")


def test_a_resolved_but_never_installed_lock_does_not_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    files = _command_package(_marker_script())
    lock = _resolve(project_source(files))

    with pytest.raises(CapabilityNotInstalledError):
        run_package_command(
            lock,
            NAME,
            {"marker": str(marker)},
            install_root=tmp_path / "install",
            capabilities_root=tmp_path / "capabilities",
            interpreter_paths=INTERPRETERS,
        )

    assert not marker.exists()


def test_uninstall_revokes_the_authority_to_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    installed = _install(tmp_path, _command_package(_marker_script()))
    installed.run({"marker": str(marker)})
    marker.unlink()

    uninstall_install(installed.install_root)

    with pytest.raises(CapabilityNotInstalledError):
        installed.run({"marker": str(marker)})
    assert not marker.exists()


def test_a_lock_other_than_the_installed_one_does_not_run(tmp_path: Path) -> None:
    installed = _install(tmp_path, _command_package(ECHO))
    other = _resolve(
        project_source(_command_package(ECHO, extra={"references/notes.txt": b"changed"}))
    )

    with pytest.raises(CapabilityNotInstalledError):
        installed.run({}, lock=other)


# --- drift -------------------------------------------------------------------------------------


def _rewrite_script(directory: Path) -> None:
    script = directory / "scripts" / "run.py"
    script.chmod(0o644)
    script.write_bytes(_marker_script("marker") + b"# tampered\n")


def _add_sibling(directory: Path) -> None:
    (directory / "scripts" / "extra.py").write_text("print('injected')\n")


def _remove_reference(directory: Path) -> None:
    (directory / "references" / "guide.md").unlink()


def _replace_with_symlink(directory: Path) -> None:
    script = directory / "scripts" / "run.py"
    script.unlink()
    script.symlink_to("/bin/sh")


@pytest.mark.parametrize(
    "tamper",
    [_rewrite_script, _add_sibling, _remove_reference, _replace_with_symlink],
    ids=["rewritten-script", "added-sibling", "removed-reference", "symlinked-script"],
)
def test_a_drifted_materialized_closure_is_rejected_and_never_runs(
    tmp_path: Path, tamper: Callable[[Path], None]
) -> None:
    marker = tmp_path / "ran"
    installed = _install(tmp_path, _command_package(_marker_script()))
    tamper(installed.directory)

    with pytest.raises(CapabilityIntegrityError):
        installed.run({"marker": str(marker)})

    assert not marker.exists()


def test_a_missing_materialized_directory_is_rejected(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    installed = _install(tmp_path, _command_package(_marker_script()))
    shutil.rmtree(installed.directory)

    with pytest.raises(CapabilityIntegrityError):
        installed.run({"marker": str(marker)})

    assert not marker.exists()


# --- approval, license, compatibility ----------------------------------------------------------


def test_a_run_introduced_package_needs_approval_again_at_run_time(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    files = _command_package(_marker_script())
    installed = _install(tmp_path, files, source=user_source(files), approved=frozenset({NAME}))

    with pytest.raises(CapabilityApprovalRequiredError):
        installed.run({"marker": str(marker)})
    assert not marker.exists()

    installed.run({"marker": str(marker)}, approved=frozenset({NAME}))

    assert marker.exists()


def _rebind_install(installed: Installed, tampered: CapabilityLock) -> None:
    """Make the active install record claim ``tampered`` as the lock it was made from."""

    path = installed.install_root / OWNED_MANIFEST_NAME
    document = json.loads(path.read_text())
    document["lock_digest"] = str(tampered.digest())
    path.unlink()
    path.write_text(json.dumps(document))


def test_a_license_hold_refuses_to_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    files = _command_package(_marker_script())
    installed = _install(tmp_path, files, source=user_source(files), approved=frozenset({NAME}))
    entry = installed.lock.get(NAME)
    assert entry is not None
    held = lock_of(dataclasses.replace(entry, license=None))
    _rebind_install(installed, held)

    with pytest.raises(CapabilityLicenseHoldError):
        installed.run({"marker": str(marker)}, lock=held, approved=frozenset({NAME}))

    assert not marker.exists()


def test_an_incompatible_receipt_refuses_to_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    installed = _install(tmp_path, _command_package(_marker_script()))
    entry = installed.lock.get(NAME)
    assert entry is not None
    incompatible = CompatibilityReceipt(
        host_adapter_api_version=entry.receipt.host_adapter_api_version,
        host_platform=entry.receipt.host_platform,
        status=CompatibilityStatus.INCOMPATIBLE,
        reasons=("host is not supported",),
    )
    broken = lock_of(dataclasses.replace(entry, receipt=incompatible))
    _rebind_install(installed, broken)

    with pytest.raises(CapabilityIncompatibleError):
        installed.run({"marker": str(marker)}, lock=broken)

    assert not marker.exists()


def test_the_rebound_install_runs_when_the_lock_is_unchanged(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    installed = _install(tmp_path, _command_package(_marker_script()))
    _rebind_install(installed, installed.lock)

    installed.run({"marker": str(marker)})

    assert marker.exists()


# --- no shell, no bytecode ---------------------------------------------------------------------


def test_shell_metacharacters_in_the_payload_have_no_side_effect(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    hostile = f"$(touch {marker}); `touch {marker}`; touch {marker} | cat && touch {marker}"
    script = _script(
        "import json, sys", "print(json.dumps({'text': json.load(sys.stdin)['text']}))"
    )
    installed = _install(tmp_path, _command_package(script))

    result = installed.run({"text": hostile})

    assert result.output == {"text": hostile}
    assert not marker.exists()
    assert result.argv == (sys.executable, "scripts/run.py")


def test_running_leaves_no_bytecode_and_the_closure_still_verifies(tmp_path: Path) -> None:
    script = _script(
        "import json, sys",
        "import helper",
        "json.dump({'value': helper.VALUE}, sys.stdout)",
    )
    installed = _install(
        tmp_path, _command_package(script, extra={"scripts/helper.py": b"VALUE = 7\n"})
    )

    assert installed.run({}).output == {"value": 7}
    assert installed.run({}).output == {"value": 7}

    assert not list(installed.directory.rglob("__pycache__"))
    entry = installed.lock.get(NAME)
    assert entry is not None
    load_verified_closure(entry, installed.capabilities_root)
