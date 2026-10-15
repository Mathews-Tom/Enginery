"""The in-image worker entrypoint, exercised as the real script against temp dirs.

``/workspace`` and ``/out`` are stood in for by the script's ``--workspace``/``--out`` flags.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(str(resources.files("enginery.adapters.oci").joinpath("assets/worker_entry.py")))
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Worker",
    "GIT_AUTHOR_EMAIL": "worker@enginery.invalid",
    "GIT_COMMITTER_NAME": "Worker",
    "GIT_COMMITTER_EMAIL": "worker@enginery.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@dataclass(frozen=True)
class Outcome:
    process: subprocess.CompletedProcess[str]
    out: Path
    elapsed: float

    def result(self, name: str = "result.json") -> dict[str, object]:
        loaded = json.loads((self.out / name).read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        return loaded


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def out(tmp_path: Path) -> Path:
    path = tmp_path / "out"
    path.mkdir()
    return path


def run_entry(
    workspace: Path,
    out: Path,
    command: list[str],
    *wrapper: str,
    deadline: float = 60,
    umask: int | None = None,
    env: dict[str, str] | None = None,
) -> Outcome:
    arguments = [
        sys.executable,
        str(SCRIPT),
        "--operation-id",
        "op-1",
        "--result",
        "result.json",
        "--deadline-seconds",
        str(deadline),
        "--workspace",
        str(workspace),
        "--out",
        str(out),
        *wrapper,
        "--",
        *command,
    ]
    previous = os.umask(umask) if umask is not None else None
    started = time.monotonic()
    try:
        process = subprocess.run(
            arguments,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, **GIT_ENV, **(env or {})},
            check=False,
        )
    finally:
        if previous is not None:
            os.umask(previous)
    return Outcome(process, out, time.monotonic() - started)


def python(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def process_exists(pid: int) -> bool:
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def wait_gone(pid: int, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not process_exists(pid):
            return True
        time.sleep(0.05)
    return False


def git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )
    return completed.stdout.strip()


def test_success_writes_the_exact_result_document(workspace: Path, out: Path) -> None:
    outcome = run_entry(workspace, out, python("print('hello')"))

    assert outcome.process.returncode == 0
    assert outcome.result() == {
        "operation_id": "op-1",
        "output": "hello\n",
        "schema_version": 1,
        "terminal_status": "succeeded",
    }
    assert sorted(path.name for path in out.iterdir()) == ["result.json"]
    mode = stat.S_IMODE((out / "result.json").stat().st_mode)
    assert mode == 0o666


def test_output_merges_stderr_and_gives_the_command_no_stdin(workspace: Path, out: Path) -> None:
    source = (
        "import sys\n"
        "print('out', flush=True)\n"
        "print('err', file=sys.stderr, flush=True)\n"
        "print(repr(sys.stdin.read()))\n"
    )

    outcome = run_entry(workspace, out, python(source))

    assert outcome.result()["output"] == "out\nerr\n''\n"


def test_command_runs_in_the_current_directory_and_its_own_process_group(
    workspace: Path, out: Path
) -> None:
    source = "import os; print(os.getcwd()); print(os.getpgrp() == os.getpid())"

    outcome = run_entry(workspace, out, python(source))

    lines = str(outcome.result()["output"]).splitlines()
    assert Path(lines[0]).resolve() == workspace.resolve()
    assert lines[1] == "True"


@pytest.mark.parametrize(
    ("source", "exit_code"),
    [
        ("import sys; sys.exit(3)", 3),
        ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", 128 + signal.SIGKILL),
    ],
)
def test_exit_code_follows_the_command_and_maps_signals(
    workspace: Path, out: Path, source: str, exit_code: int
) -> None:
    outcome = run_entry(workspace, out, python(source))

    assert outcome.process.returncode == exit_code
    result = outcome.result()
    assert result["terminal_status"] == "failed"
    assert result["schema_version"] == 1


def test_an_unlaunchable_command_fails_with_exit_one_and_a_result(
    workspace: Path, out: Path
) -> None:
    outcome = run_entry(workspace, out, [str(workspace / "no-such-binary")])

    assert outcome.process.returncode == 1
    result = outcome.result()
    assert result["terminal_status"] == "failed"
    assert "no-such-binary" in str(result["output"])


def test_flag_like_tokens_after_the_separator_belong_to_the_command(
    workspace: Path, out: Path
) -> None:
    tokens = ["--operation-id", "--", "--result", "-x", "--deadline-seconds", "1"]

    outcome = run_entry(workspace, out, python("import sys; print(sys.argv[1:])") + tokens)

    assert outcome.result()["output"] == f"{tokens!r}\n"
    assert outcome.result()["operation_id"] == "op-1"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--operation-id", "x", "--result", "r.json", "--deadline-seconds", "5"],
        ["--operation-id", "x", "--result", "r.json", "--deadline-seconds", "5", "--"],
        ["--operation-id", "x", "--result", "r.json", "--deadline-seconds", "0", "--", "true"],
        ["--operation-id", " ", "--result", "r.json", "--deadline-seconds", "5", "--", "true"],
        ["--operation-id", "x", "--result", "r.json", "--deadline-seconds", "5", "--", "true", ""],
        [
            "--operation-id",
            "x",
            "--result",
            "r.json",
            "--deadline-seconds",
            "5",
            "--bundle-branch",
            "b",
            "--",
            "true",
        ],
    ],
)
def test_malformed_invocations_are_rejected_without_a_result(
    workspace: Path, out: Path, arguments: list[str]
) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out), *arguments],
        capture_output=True,
        text=True,
        check=False,
        cwd=workspace,
    )

    assert completed.returncode == 2
    assert list(out.iterdir()) == []


def test_deadline_terminates_the_whole_process_group_including_grandchildren(
    workspace: Path, out: Path
) -> None:
    pid_file = workspace / "grandchild.pid"
    source = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print('started', flush=True)\n"
        "time.sleep(300)\n"
    )

    outcome = run_entry(workspace, out, python(source), "--kill-grace-seconds", "5", deadline=1)

    assert outcome.process.returncode == 128 + signal.SIGTERM
    result = outcome.result()
    assert result["terminal_status"] == "failed"
    output = str(result["output"])
    assert output.startswith("started\n")
    assert (
        output.rstrip("\n")
        .splitlines()[-1]
        .startswith("[enginery worker: deadline of 1 seconds exceeded")
    )
    assert outcome.elapsed < 30
    assert wait_gone(int(pid_file.read_text()))


def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period(
    workspace: Path, out: Path
) -> None:
    pid_file = workspace / "stubborn.pid"
    source = (
        "import os, signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "ignore = 'import signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'\n"
        "child = subprocess.Popen([sys.executable, '-c', ignore])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(300)\n"
    )

    outcome = run_entry(workspace, out, python(source), "--kill-grace-seconds", "1", deadline=1)

    assert outcome.process.returncode == 128 + signal.SIGKILL
    assert outcome.result()["terminal_status"] == "failed"
    assert outcome.elapsed < 30
    assert wait_gone(int(pid_file.read_text()))


def test_a_silent_command_killed_at_the_deadline_yields_only_the_deadline_note(
    workspace: Path, out: Path
) -> None:
    outcome = run_entry(workspace, out, python("import time; time.sleep(300)"), deadline=1)

    assert outcome.result()["output"] == (
        "[enginery worker: deadline of 1 seconds exceeded; process group terminated]\n"
    )


def test_a_deadline_that_ends_in_a_clean_exit_is_still_a_failure(
    workspace: Path, out: Path
) -> None:
    source = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "time.sleep(300)\n"
    )

    outcome = run_entry(workspace, out, python(source), deadline=1)

    assert outcome.process.returncode == 124
    assert outcome.result()["terminal_status"] == "failed"


def test_a_leftover_background_process_does_not_hold_the_result_hostage(
    workspace: Path, out: Path
) -> None:
    pid_file = workspace / "daemon.pid"
    source = (
        "import subprocess, sys\n"
        "daemon = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(daemon.pid))\n"
        "print('done')\n"
    )

    outcome = run_entry(workspace, out, python(source))

    assert outcome.process.returncode == 0
    assert outcome.result()["output"] == "done\n"
    assert outcome.elapsed < 30
    assert wait_gone(int(pid_file.read_text()))


def test_seed_files_are_written_before_the_command_with_mode_0600_despite_the_callers_umask(
    workspace: Path, out: Path, tmp_path: Path
) -> None:
    seed = tmp_path / "home" / ".config" / "tool" / "settings.json"
    payload = b'{"model": "x"}\n\xff'
    encoded = base64.b64encode(payload).decode()
    source = f"import sys; sys.stdout.buffer.write(open({str(seed)!r}, 'rb').read())"

    outcome = run_entry(
        workspace, out, python(source), "--seed-file", f"{seed}={encoded}", umask=0o077
    )

    assert outcome.process.returncode == 0
    assert seed.read_bytes() == payload
    assert stat.S_IMODE(seed.stat().st_mode) == 0o600
    # umask 000 inside the worker keeps created directories host-deletable.
    assert stat.S_IMODE(seed.parent.stat().st_mode) == 0o777
    assert outcome.result()["output"] == payload.decode("utf-8", errors="replace")


def test_files_the_command_creates_follow_umask_zero(workspace: Path, out: Path) -> None:
    source = "import os; open('made.txt', 'w').close(); os.mkdir('made-dir')"

    outcome = run_entry(workspace, out, python(source), umask=0o077)

    assert outcome.process.returncode == 0
    assert stat.S_IMODE((workspace / "made.txt").stat().st_mode) == 0o666
    assert stat.S_IMODE((workspace / "made-dir").stat().st_mode) == 0o777


def test_an_invalid_seed_fails_before_the_command_and_never_echoes_the_seed(
    workspace: Path, out: Path
) -> None:
    marker = workspace / "ran"
    secret = "not-base64-SECRETVALUE!!"

    outcome = run_entry(
        workspace,
        out,
        python(f"open({str(marker)!r}, 'w').close()"),
        "--seed-file",
        f"{workspace / 'seed'}={secret}",
    )

    assert outcome.process.returncode == 1
    result = outcome.result()
    assert result["terminal_status"] == "failed"
    assert "not valid base64" in str(result["output"])
    assert secret not in outcome.process.stdout + outcome.process.stderr + str(result["output"])
    assert not marker.exists()


def test_a_relative_seed_path_is_refused(workspace: Path, out: Path) -> None:
    outcome = run_entry(
        workspace,
        out,
        python("print(1)"),
        "--seed-file",
        f"relative.txt={base64.b64encode(b'x').decode()}",
    )

    assert outcome.process.returncode == 1
    assert outcome.result()["terminal_status"] == "failed"
    assert not (workspace / "relative.txt").exists()


def _init_repo(workspace: Path) -> str:
    git(workspace, "init", "-q", "-b", "main")
    (workspace / "README.md").write_text("base\n", encoding="utf-8")
    git(workspace, "add", "README.md")
    git(workspace, "commit", "-q", "-m", "base")
    return git(workspace, "rev-parse", "HEAD")


def test_bundle_of_the_worker_branch_unbundles_into_an_independent_repository(
    workspace: Path, out: Path, tmp_path: Path
) -> None:
    base = _init_repo(workspace)
    # A host-side intake repository that holds only the base, as the host's will.
    intake = tmp_path / "intake"
    subprocess.run(
        ["git", "clone", "-q", str(workspace), str(intake)],
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    command = [
        "sh",
        "-ceu",
        "git checkout -q -b work/feature && echo change > feature.txt && git add feature.txt"
        " && git commit -q -m 'add feature' && echo done",
    ]

    outcome = run_entry(
        workspace,
        out,
        command,
        "--bundle-branch",
        "work/feature",
        "--bundle-base",
        base,
        "--bundle-path",
        "branch.bundle",
    )

    assert outcome.process.returncode == 0
    assert outcome.result()["terminal_status"] == "succeeded"
    bundle = out / "branch.bundle"
    assert bundle.is_file()
    head = git(workspace, "rev-parse", "work/feature")
    git(intake, "bundle", "verify", str(bundle))
    git(intake, "fetch", "-q", str(bundle), "refs/heads/work/feature:refs/heads/work/feature")
    assert git(intake, "rev-parse", "work/feature") == head
    assert git(intake, "log", "--format=%s", f"{base}..work/feature") == "add feature"


def test_the_bundle_is_taken_even_when_the_deadline_killed_the_command(
    workspace: Path, out: Path
) -> None:
    base = _init_repo(workspace)
    command = [
        "sh",
        "-ceu",
        "git checkout -q -b work/slow && echo c > c.txt && git add c.txt && git commit -q -m c"
        " && exec sleep 300",
    ]

    outcome = run_entry(
        workspace,
        out,
        command,
        "--bundle-branch",
        "work/slow",
        "--bundle-base",
        base,
        "--kill-grace-seconds",
        "2",
        deadline=5,
    )

    assert outcome.result()["terminal_status"] == "failed"
    assert (out / "branch.bundle").is_file()


def test_a_bundle_failure_leaves_no_bundle_and_does_not_change_the_status(
    workspace: Path, out: Path
) -> None:
    base = _init_repo(workspace)
    stale = out / "branch.bundle"
    stale.write_bytes(b"left over from an earlier attempt")

    outcome = run_entry(
        workspace,
        out,
        python("print('ok')"),
        "--bundle-branch",
        "work/missing",
        "--bundle-base",
        base,
    )

    assert outcome.process.returncode == 0
    assert outcome.result()["terminal_status"] == "succeeded"
    assert not stale.exists()
    assert "bundle was not created" in outcome.process.stderr


def test_a_bundle_with_no_new_commits_is_not_produced(workspace: Path, out: Path) -> None:
    base = _init_repo(workspace)

    outcome = run_entry(
        workspace,
        out,
        python("print('nothing changed')"),
        "--bundle-branch",
        "main",
        "--bundle-base",
        base,
    )

    assert outcome.result()["terminal_status"] == "succeeded"
    assert not (out / "branch.bundle").exists()


@pytest.fixture
def entry_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("worker_entry_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    previous = os.umask(0o022)
    try:
        yield module
    finally:
        os.umask(previous)


def test_oversized_output_keeps_the_head_and_tail_and_marks_the_gap(
    entry_module: ModuleType, workspace: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entry_module, "MAX_OUTPUT_BYTES", 2000)
    source = "import sys; sys.stdout.write('H' * 3000 + 'M' * 50000 + 'T' * 3000 + 'END\\n')"

    code = entry_module.main(
        [
            "--operation-id",
            "op",
            "--result",
            "r.json",
            "--deadline-seconds",
            "30",
            "--workspace",
            str(workspace),
            "--out",
            str(out),
            "--",
            *python(source),
        ]
    )

    assert code == 0
    output = json.loads((out / "r.json").read_text())["output"]
    assert output.startswith("H" * 1000)
    assert output.endswith("T" * 996 + "END\n")
    assert "bytes of output omitted]" in output
    assert len(output) < 2200


def test_the_result_file_is_shrunk_to_fit_the_host_size_cap(
    entry_module: ModuleType, workspace: Path, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entry_module, "MAX_RESULT_BYTES", 4000)
    source = "import sys; sys.stdout.write('\"\\n' * 3000)"

    code = entry_module.main(
        [
            "--operation-id",
            "op",
            "--result",
            "r.json",
            "--deadline-seconds",
            "30",
            "--workspace",
            str(workspace),
            "--out",
            str(out),
            "--",
            *python(source),
        ]
    )

    assert code == 0
    raw = (out / "r.json").read_bytes()
    assert len(raw) <= 4000
    document = json.loads(raw)
    assert document["terminal_status"] == "succeeded"
    assert "characters of output omitted" in document["output"]
