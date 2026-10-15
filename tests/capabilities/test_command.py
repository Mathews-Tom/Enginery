from __future__ import annotations

import dataclasses
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from enginery.application.delivery_ports import PackageFile
from enginery.capabilities import command as command_module
from enginery.capabilities.closure import build_closure
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
    CapabilityManifestError,
    CapabilityNotFoundError,
    CapabilityNotInstalledError,
)
from enginery.capabilities.install import OWNED_MANIFEST_NAME, install_lock, uninstall_install
from enginery.capabilities.lock import ApprovedCapabilities, CapabilityLock, LockedCapability
from enginery.capabilities.manifest import CommandSpec, PackageKind
from enginery.capabilities.materialize import digest_path, load_verified_closure
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.capabilities.sources import CapabilitySource
from enginery.domain.errors import InvalidInputError
from tests.support.capabilities import (
    approvals_for,
    lock_of,
    locked_entry,
    package_files,
    project_source,
    user_source,
)

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
        approvals: ApprovedCapabilities | None = None,
    ) -> CommandResult:
        chosen = self.lock if lock is None else lock
        return run_package_command(
            chosen,
            name,
            payload,
            install_root=self.install_root,
            capabilities_root=self.capabilities_root,
            approvals=approvals_for(chosen, approved) if approvals is None else approvals,
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
        approvals=approvals_for(resolved.lock, approved),
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
    assert result.argv[1:] == ("-B", "-s", "-E", "scripts/run.py")


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


def test_run_executes_from_a_private_copy_with_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(
        "import json, os",
        "print(json.dumps({'cwd': os.getcwd(), 'env': sorted(os.environ),"
        " 'files': sorted(os.listdir('.'))}))",
    )
    installed = _install(tmp_path, _command_package(script))
    monkeypatch.setenv("ENGINERY_TEST_SECRET", "leak")

    result = installed.run({})

    assert isinstance(result.output, dict)
    cwd = Path(str(result.output["cwd"]))
    assert cwd.resolve() != installed.directory.resolve()
    assert cwd.name.startswith("enginery-run-")
    assert not cwd.exists()
    assert result.output["files"] == ["SKILL.md", "capability.json", "references", "scripts"]
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

    approvals = approvals_for(held, [NAME])
    with pytest.raises(CapabilityLicenseHoldError):
        installed.run({"marker": str(marker)}, lock=held, approvals=approvals)

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
    assert result.argv == (sys.executable, "-B", "-s", "-E", "scripts/run.py")


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


# --- hardening ---------------------------------------------------------------------------------


def test_an_approval_computed_for_another_lock_does_not_authorize_a_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    files = _command_package(_marker_script())
    installed = _install(tmp_path, files, source=user_source(files), approved=frozenset({NAME}))
    entry = installed.lock.get(NAME)
    assert entry is not None
    other_lock = lock_of(entry, locked_entry("other", introduced_by_run=True))
    foreign = approvals_for(other_lock, [NAME])

    with pytest.raises(CapabilityApprovalRequiredError):
        installed.run({"marker": str(marker)}, approvals=foreign)

    assert not marker.exists()


def test_a_run_requires_the_install_record_to_hold_the_approved_closure(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    files = _command_package(_marker_script())
    installed = _install(tmp_path, files, source=user_source(files), approved=frozenset({NAME}))
    path = installed.install_root / OWNED_MANIFEST_NAME
    document = json.loads(path.read_text())
    document["approved_closures"] = {}
    path.unlink()
    path.write_text(json.dumps(document))

    with pytest.raises(CapabilityApprovalRequiredError, match="active install"):
        installed.run({"marker": str(marker)}, approved=frozenset({NAME}))

    assert not marker.exists()


def test_a_receipt_recorded_for_another_host_refuses_to_run(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    installed = _install(tmp_path, _command_package(_marker_script()))
    entry = installed.lock.get(NAME)
    assert entry is not None
    foreign_receipt = CompatibilityReceipt(
        host_adapter_api_version=entry.receipt.host_adapter_api_version,
        host_platform="another-platform",
        status=CompatibilityStatus.COMPATIBLE,
    )
    foreign = lock_of(dataclasses.replace(entry, receipt=foreign_receipt))
    _rebind_install(installed, foreign)

    with pytest.raises(CapabilityIncompatibleError, match="not recorded for this host"):
        installed.run({"marker": str(marker)}, lock=foreign)

    assert not marker.exists()


def test_a_background_grandchild_does_not_outlive_a_successful_run(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = _script(
        "import json, subprocess, sys",
        "payload = json.load(sys.stdin)",
        "child = subprocess.Popen(",
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],",
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,",
        ")",
        "open(payload['pid_file'], 'w').write(str(child.pid))",
        "print('{}')",
    )
    installed = _install(tmp_path, _command_package(script))

    installed.run({"pid_file": str(pid_file)})

    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    alive = _alive(pid)
    if alive:
        os.kill(pid, 9)
    assert not alive


def test_python_commands_run_without_user_site_or_environment_hooks(tmp_path: Path) -> None:
    script = _script(
        "import json, sys",
        "flags = sys.flags",
        "json.dump({'no_user_site': flags.no_user_site,",
        "           'ignore_environment': flags.ignore_environment,",
        "           'dont_write_bytecode': flags.dont_write_bytecode}, sys.stdout)",
    )
    installed = _install(tmp_path, _command_package(script))

    result = installed.run({})

    assert result.output == {"no_user_site": 1, "ignore_environment": 1, "dont_write_bytecode": 1}


def test_deeply_nested_command_output_is_a_command_output_error(tmp_path: Path) -> None:
    script = _script("import sys", "sys.stdout.write('[' * 5000 + ']' * 5000)")
    installed = _install(tmp_path, _command_package(script))

    with pytest.raises(CapabilityCommandOutputError, match="nested too deeply"):
        installed.run({})


def test_node_runs_under_its_permission_model_limited_to_the_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "closure"
    directory.mkdir()
    expected_read = f"--allow-fs-read={directory.resolve()}"

    monkeypatch.setattr(command_module, "_node_version", lambda located: (23, 7))
    assert command_module._interpreter_flags("node", "/usr/bin/node", directory) == (
        "--permission",
        expected_read,
    )
    monkeypatch.setattr(command_module, "_node_version", lambda located: (22, 3))
    assert command_module._interpreter_flags("node", "/usr/bin/node", directory) == (
        "--experimental-permission",
        expected_read,
    )
    monkeypatch.setattr(command_module, "_node_version", lambda located: (18, 19))
    with pytest.raises(CapabilityCommandNotRunnableError, match="permission model"):
        command_module._interpreter_flags("node", "/usr/bin/node", directory)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_cannot_resolve_a_bare_module_from_an_ancestor_node_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = shutil.which("node")
    assert node is not None
    if command_module._node_version(node) < (20, 0):
        pytest.skip("node is too old for the permission model")
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    planted = scratch / "node_modules" / "planted"
    planted.mkdir(parents=True)
    (planted / "index.js").write_text("module.exports = 'loaded';\n")
    script = _script(
        "let outcome;",
        "try { require('planted'); outcome = 'resolved'; } catch (error) { outcome = 'blocked'; }",
        "process.stdout.write(JSON.stringify({outcome}));",
    )
    spec = CommandSpec.create(argv=("node", "scripts/run.js"), input_schema={"type": "object"})
    files = package_files(NAME, kind=PackageKind.COMMAND, command=spec, script=script)
    resolved = CapabilityResolver([project_source(files)]).resolve_with_files(
        [CapabilityRequest(NAME, "1")]
    )
    install_lock(
        resolved, install_root=tmp_path / "install", capabilities_root=tmp_path / "capabilities"
    )

    result = run_package_command(
        resolved.lock,
        NAME,
        {},
        install_root=tmp_path / "install",
        capabilities_root=tmp_path / "capabilities",
        interpreter_paths={"node": node},
    )

    assert result.output == {"outcome": "blocked"}


# --- private verified copy ---------------------------------------------------------------------


def test_a_store_swap_after_verification_cannot_change_the_code_that_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = _install(tmp_path, _command_package(_script("print('\"verified\"')")))
    real = command_module.load_verified_package

    def swap_after_verifying(entry: LockedCapability, root: Path) -> object:
        verified = real(entry, root)
        script = installed.directory / "scripts" / "run.py"
        script.unlink()
        script.write_text("print('\"swapped\"')\n")
        return verified

    monkeypatch.setattr(command_module, "load_verified_package", swap_after_verifying)

    result = installed.run({})

    assert result.output == "verified"
    assert (installed.directory / "scripts" / "run.py").read_text() == "print('\"swapped\"')\n"


def test_the_run_directory_is_owner_only_and_removed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = _install(tmp_path, _command_package(ECHO))
    seen: list[tuple[Path, int]] = []
    real = command_module._execute

    def recording_execute(**kwargs: Any) -> CommandResult:
        directory = kwargs["directory"]
        seen.append((directory, stat.S_IMODE(directory.stat().st_mode)))
        return real(**kwargs)

    monkeypatch.setattr(command_module, "_execute", recording_execute)

    installed.run({})

    ((directory, mode),) = seen
    assert mode == 0o700
    assert directory != installed.directory
    assert not directory.exists()


@pytest.mark.parametrize("script", ["scripts/run.py", "scripts/run.js", "scripts/run.mjs"])
def test_a_script_run_through_its_interpreter_cannot_be_executed_directly(script: str) -> None:
    with pytest.raises(InvalidInputError, match="interpreter"):
        CommandSpec.create(argv=(script,), input_schema={"type": "object"})


def test_a_shell_script_may_still_run_directly_from_the_private_copy(tmp_path: Path) -> None:
    spec = CommandSpec.create(argv=("scripts/run.sh",), input_schema={"type": "object"})
    script = b"#!/bin/sh\ncat > /dev/null\nprintf '{\"ok\": true}'\n"
    files = package_files(NAME, kind=PackageKind.COMMAND, command=spec, script=script)
    installed = _install(tmp_path, files)

    result = installed.run({})

    assert result.output == {"ok": True}
    assert result.argv[0].endswith("scripts/run.sh")


class _FakeProcess:
    pid = 4242

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def wait(self) -> int:
        self._events.append("wait")
        return 0


@pytest.mark.parametrize("has_waitid", [True, False])
def test_the_group_is_killed_before_the_leader_is_reaped_where_waitid_exists(
    monkeypatch: pytest.MonkeyPatch, has_waitid: bool
) -> None:
    events: list[str] = []

    def waitid(*arguments: int) -> None:
        events.append("waitid")

    def killpg(pid: int, signal_number: int) -> None:
        events.append("killpg")

    monkeypatch.setattr(command_module, "_WAITID", waitid if has_waitid else None)
    monkeypatch.setattr(command_module.os, "killpg", killpg)

    command_module._end_group(_FakeProcess(events))  # type: ignore[arg-type]

    assert events == (["waitid", "killpg", "wait"] if has_waitid else ["wait", "killpg"])


def _direct_shell_package(script: bytes) -> tuple[PackageFile, ...]:
    spec = CommandSpec.create(argv=("scripts/run.sh",), input_schema={"type": "object"})
    return package_files(NAME, kind=PackageKind.COMMAND, command=spec, script=script)


@pytest.mark.parametrize(
    "first_line",
    [
        b"#!/usr/bin/env python3",
        b"#!/usr/bin/python3",
        b"#!/usr/bin/env node",
        b"#!/bin/sh -x",
        b"#! /bin/sh",
        b"#!/bin/sh\r",
        b"#!/usr/bin/env -S sh",
        b"# no shebang",
        b"",
    ],
)
def test_a_directly_executed_script_needs_an_exact_shell_shebang(first_line: bytes) -> None:
    files = _direct_shell_package(first_line + b"\nprint('x')\n")

    with pytest.raises(CapabilityManifestError, match=r"start with"):
        build_closure(files)


@pytest.mark.parametrize(
    "first_line",
    [b"#!/bin/sh", b"#!/bin/bash", b"#!/usr/bin/env sh", b"#!/usr/bin/env bash"],
)
def test_a_directly_executed_script_with_a_shell_shebang_is_accepted(first_line: bytes) -> None:
    build_closure(_direct_shell_package(first_line + b"\ncat > /dev/null\n"))


def test_the_shebang_is_checked_again_on_the_verified_bytes_before_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = _install(
        tmp_path, _direct_shell_package(b"#!/bin/sh\ncat > /dev/null\nprintf '{}'\n")
    )
    real = command_module.load_verified_package

    def swapped(*arguments: Any, **keywords: Any) -> Any:
        located, closure, verified = real(*arguments, **keywords)
        replaced = [
            dataclasses.replace(item, content=b"#!/usr/bin/env python3\nprint('{}')\n")
            if item.path == "scripts/run.sh"
            else item
            for item in verified
        ]
        return located, closure, replaced

    monkeypatch.setattr(command_module, "load_verified_package", swapped)

    with pytest.raises(CapabilityManifestError):
        installed.run({})


def test_the_command_environment_restricts_child_pythons(tmp_path: Path) -> None:
    script = (
        b"#!/bin/sh\ncat > /dev/null\n"
        b'printf \'{"user_site": "%s", "safe_path": "%s"}\' '
        b'"$PYTHONNOUSERSITE" "$PYTHONSAFEPATH"\n'
    )
    installed = _install(tmp_path, _direct_shell_package(script))

    result = installed.run({})

    assert result.output == {"user_site": "1", "safe_path": "1"}


def test_no_second_killpg_follows_the_group_being_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script(
        "import subprocess, sys",
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'],",
        "                 start_new_session=True)",
        "print('{}')",
    )
    installed = _install(tmp_path, _command_package(script))
    calls: list[int] = []
    real_killpg = os.killpg

    def killpg(pid: int, signal_number: int) -> None:
        calls.append(pid)
        real_killpg(pid, signal_number)

    monkeypatch.setattr(command_module.os, "killpg", killpg)

    installed.run({})

    assert len(calls) == 1
