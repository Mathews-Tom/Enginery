"""The ``setup`` scenario: guided setup and layered configuration, end to end, with no hosted call.

Everything runs against a disposable home: ``HOME`` and both XDG directories name a fresh
temporary tree, ``gh`` and the ``omp`` harness are recording stand-ins first on ``PATH``, and
every step drives the real ``enginery`` CLI in a subprocess. The scenario asserts that

* ``factory init`` previews without writing, applies exactly its preview, is idempotent, and
  reports a differing user-owned policy as a conflict instead of overwriting it;
* the run, project and user layers each reach the selection the real Stage 1 adapter *submits*
  to the harness (the recorded ``--model`` and ``--thinking`` argv), not just ``explain``;
* a project hard constraint refuses a run-layer override and an unknown key is refused, each
  before any request is written;
* ``factory doctor`` holds an unsatisfied authentication with the exact resolving command;
* ``factory uninstall`` previews without removing, removes only unmodified setup-owned files,
  keeps a user-modified one, never touches bystanders, and is idempotent; and
* the disposable home's global locations (``~/.config/gh``, ``~/.omp``, ``~/.claude``,
  ``~/.claude.json``, MCP configuration, launchd and systemd directories) are byte-identical
  before and after, and nothing outside the project and Enginery's private locations changed.

No required assertion is ever skipped: one that could not run is recorded as a failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from factory_gate_context import (
    GateHoldError,
    GateOptions,
    evaluator_identity,
    host_identity,
    subject_identity,
)
from factory_gate_evidence import EvidenceBuilder

SCENARIO = "setup"

REQUIRED: dict[str, str] = {
    "setup-preview-writes-nothing": (
        "factory init without --apply lists every file, install, network access and auth "
        "requirement and writes nothing"
    ),
    "setup-apply-performs-previewed-plan": (
        "factory init --apply performs exactly the previewed plan"
    ),
    "setup-repeat-init-no-change": "repeating factory init with the same choices changes nothing",
    "setup-conflict-user-owned-not-overwritten": (
        "a differing existing project policy is a user-owned conflict and is never overwritten"
    ),
    "layer-user-reaches-submission": (
        "a user-layer model and effort are what the Stage 1 adapter submits to the harness"
    ),
    "layer-project-over-user-reaches-submission": (
        "a project-layer model and effort beat the user layer in the submitted harness argv"
    ),
    "layer-run-over-project-reaches-submission": (
        "run-layer flags beat the project and user layers in the submitted harness argv"
    ),
    "hard-policy-refuses-run-override": (
        "a project hard constraint refuses a run-layer override before any request is written"
    ),
    "unknown-key-refused": "an unknown key in project policy is refused naming the file and key",
    "doctor-hold-names-resolving-command": (
        "factory doctor holds an unsatisfied authentication with the exact resolving command "
        "and changes nothing"
    ),
    "uninstall-preview-writes-nothing": "factory uninstall without --apply removes nothing",
    "uninstall-removes-only-owned-unmodified": (
        "factory uninstall --apply removes only setup-owned files that still match the record "
        "and never a bystander"
    ),
    "uninstall-keeps-user-modified-and-is-idempotent": (
        "a user-modified policy is kept as user-owned and a second uninstall is a no-op"
    ),
    "global-locations-byte-identical": (
        "the disposable home's global locations are byte-identical and nothing outside the "
        "project and Enginery's private locations changed"
    ),
    "no-hosted-session": (
        "the only harness that ran was the local recording stand-in: zero hosted sessions"
    ),
}

_GLOBAL_SENTINELS: dict[str, bytes] = {
    ".config/gh/hosts.yml": b"github.com:\n  user: sentinel\n",
    ".omp/agent/config.yml": b"sentinel: omp\n",
    ".claude/settings.json": b'{"sentinel": "claude"}\n',
    ".claude.json": b'{"mcpServers": {"sentinel": {}}}\n',
    ".config/mcp/servers.json": b'{"sentinel": true}\n',
    "Library/LaunchAgents/com.example.sentinel.plist": b"<plist/>\n",
    ".config/systemd/user/sentinel.service": b"[Unit]\nDescription=sentinel\n",
}
_GLOBAL_ROOTS = (
    ".config/gh",
    ".omp",
    ".claude",
    ".claude.json",
    ".config/mcp",
    "Library/LaunchAgents",
    ".config/systemd",
)
_PRIVATE_PREFIXES = (".config/enginery", ".local/state/enginery")

_GH_STAND_IN = """#!{python}
import json, os, sys
from pathlib import Path

here = Path(__file__).resolve().parent
arguments = sys.argv[1:]
with (here / "gh-calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(arguments) + "\\n")
if arguments[:2] == ["auth", "status"]:
    raise SystemExit(int(os.environ.get("GATE_GH_AUTH_EXIT", "0")))
if arguments == ["--version"]:
    print("gh version 0.0.0 (setup gate stand-in)")
    raise SystemExit(0)
endpoint = arguments[-1] if arguments[:1] == ["api"] else ""
path, _, query = endpoint.partition("?")
parts = path.split("/")
if len(parts) in (5, 6) and parts[0] == "repos" and parts[3] == "issues" and parts[4].isdecimal():
    if len(parts) == 5:
        print(json.dumps({{"url": "https://api.github.com/" + path, "number": int(parts[4]),
            "title": "Bounded change", "body": "Change one bounded behavior.",
            "updated_at": "2026-10-13T00:00:00Z"}}))
        raise SystemExit(0)
    if parts[5] == "labels":
        page = int(dict(p.split("=") for p in query.split("&") if p).get("page", "1"))
        labels = ["enginery/work-kind/issue", "enginery/risk/low"]
        print(json.dumps([{{"name": n}} for n in labels] if page == 1 else []))
        raise SystemExit(0)
sys.stderr.write("HTTP 404: Not Found\\n")
raise SystemExit(1)
"""

_OMP_STAND_IN = """#!{python}
import json, os, sys

arguments = sys.argv[1:]
if arguments[:1] == ["--help"]:
    print("omp v18.10.3")
    raise SystemExit(0)
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{
        "argv": arguments,
        "env": sorted(key for key in os.environ if key.startswith(("OMP_", "ENGINERY_"))),
    }}) + "\\n")
for event in ({{"type": "session"}}, {{"type": "agent_start"}}, {{"type": "agent_end"}}):
    print(json.dumps(event))
"""


def required_assertions(options: GateOptions) -> dict[str, str]:
    return dict(REQUIRED)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot(*roots: Path, base: Path) -> dict[str, str]:
    """Every entry under ``roots`` (relative to ``base``) with its content digest."""
    entries: dict[str, str] = {}
    for root in roots:
        candidates = [root, *sorted(root.rglob("*"))] if root.exists() or root.is_symlink() else []
        for path in candidates:
            key = str(path.relative_to(base))
            if path.is_symlink():
                entries[key] = f"link:{os.readlink(path)}"
            elif path.is_dir():
                entries[key] = "dir"
            else:
                entries[key] = _sha(path.read_bytes())
    return entries


def _changed(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


class SetupGate:
    """A disposable home, recording executables and a driver for the real ``enginery`` CLI."""

    def __init__(self, options: GateOptions, evidence: EvidenceBuilder) -> None:
        self.options = options
        self.evidence = evidence
        self.root = Path(tempfile.mkdtemp(prefix="enginery-setup-gate-")).resolve()
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.projects = self.root / "projects"
        self.runs = self.root / "runs"
        self.xdg_config = self.home / ".config"
        self.xdg_state = self.home / ".local" / "state"
        for directory in (self.home, self.bin, self.projects, self.runs, self.xdg_config):
            directory.mkdir(parents=True)
        self.xdg_state.mkdir(parents=True)
        self.commands: list[str] = []
        self.effects: dict[str, Any] = {"hosted_inference_calls": 0}
        self.launch_log = self.root / "harness-launches.jsonl"
        self._write_stand_ins()
        for relative, content in _GLOBAL_SENTINELS.items():
            path = self.home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        evidence.raw_writer = self.raw_write

    # --- plumbing ---------------------------------------------------------------------------

    def _write_stand_ins(self) -> None:
        for name, body in (
            ("gh", _GH_STAND_IN.format(python=sys.executable)),
            ("omp", _OMP_STAND_IN.format(python=sys.executable, log=str(self.launch_log))),
        ):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg_config),
            "XDG_STATE_HOME": str(self.xdg_state),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        if "PYTHONPATH" in os.environ:
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        for key, value in os.environ.items():
            if key == "GIT_CONFIG_COUNT" or key.startswith(
                ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
            ):
                env[key] = value
        env.update(extra or {})
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        for key, value in (("maintenance.auto", "false"), ("gc.auto", "0")):
            env[f"GIT_CONFIG_KEY_{count}"] = key
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
        return env

    def cli(
        self, *arguments: str, extra_env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the real CLI in a subprocess against the disposable home."""
        self.commands.append("enginery " + " ".join(shlex.quote(item) for item in arguments))
        return subprocess.run(
            [sys.executable, "-m", "enginery.cli.main", *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(extra_env),
            cwd=self.root,
            timeout=180,
        )

    def git(self, project: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
            env=self.environment(),
            cwd=project,
        ).stdout.strip()

    def new_project(self, name: str) -> Path:
        project = self.projects / name
        project.mkdir()
        self.git(project, "init", "-q")
        self.git(project, "config", "user.email", "gate@example.invalid")
        self.git(project, "config", "user.name", "setup gate")
        (project / "README").write_text("disposable project\n", encoding="utf-8")
        self.git(project, "add", "README")
        self.git(project, "commit", "-q", "-m", "initial")
        return project.resolve()

    def user_file(self) -> Path:
        return self.xdg_config / "enginery" / "user.toml"

    def write_user(self, text: str | None) -> None:
        path = self.user_file()
        if text is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def home_snapshot(self) -> dict[str, str]:
        return snapshot(self.home, base=self.root)

    def global_snapshot(self) -> dict[str, str]:
        return snapshot(*(self.home / relative for relative in _GLOBAL_ROOTS), base=self.root)

    def private_snapshot(self) -> dict[str, str]:
        return snapshot(self.xdg_state, self.xdg_config, base=self.root)

    def take_commands(self, since: int) -> list[str]:
        return self.commands[since:]

    def raw_write(self, name: str, content: Any) -> str:
        directory = self.options.raw_dir
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (
            content
            if isinstance(content, str)
            else json.dumps(content, indent=2, sort_keys=True, default=str)
        )
        target.write_text(text.replace(str(self.root), "<disposable-root>"), encoding="utf-8")
        return f"{directory.name}/{name}"

    def launches(self) -> list[dict[str, Any]]:
        if not self.launch_log.exists():
            return []
        return [
            json.loads(line) for line in self.launch_log.read_text(encoding="utf-8").splitlines()
        ]

    def identities(self, required: dict[str, str]) -> dict[str, Any]:
        import enginery.config as packaged

        config_dir = Path(packaged.__file__).parent
        digests = {
            path.name: "sha256:" + _sha(path.read_bytes())
            for path in sorted(config_dir.glob("*.toml"))
            if path.name.startswith(
                ("factory-defaults", "workflow-profile", "execution-catalog-v3")
            )
        }
        return {
            "subject": subject_identity(self.options.subject_commit),
            "build": {"packaged_configuration_digests": digests},
            "config": {
                "disposable_home": True,
                "stand_ins": ["gh", "omp"],
                "xdg_config_home": "<disposable-root>/home/.config",
                "xdg_state_home": "<disposable-root>/home/.local/state",
            },
            "host": host_identity(),
            "engine": None,
            "image": None,
            "profile": None,
            "evaluator": evaluator_identity(required),
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def prepare(
    options: GateOptions,
    evidence: EvidenceBuilder,
    scrubber: object,
    engine_factory: object,
    *,
    need_relay: bool,
) -> SetupGate:
    """Build the disposable home, or hold when git or the enginery CLI is unavailable."""
    if shutil.which("git") is None:
        raise GateHoldError("git is not on PATH; the setup scenario builds disposable repositories")
    gate = SetupGate(options, evidence)
    probe = gate.cli("--version")
    if probe.returncode != 0:
        gate.cleanup()
        raise GateHoldError(
            f"`python -m enginery.cli.main --version` failed: {probe.stderr.strip()[:200]}"
        )
    gate.commands.clear()
    return gate


# --- steps --------------------------------------------------------------------------------------


def _json(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return dict(json.loads(process.stdout)) if process.stdout.strip() else {}


def _init_args(project: Path, *extra: str) -> tuple[str, ...]:
    return (
        "factory",
        "init",
        "--project",
        str(project),
        "--json",
        "--runner",
        "trusted-local",
        "--validation-command",
        "true",
        *extra,
    )


def _record(
    gate: SetupGate,
    assertion_id: str,
    *,
    passed: bool,
    since: int,
    observed: Any,
    expected: str,
    observer: Any,
) -> None:
    gate.evidence.assertion(
        assertion_id,
        passed=passed,
        commands=gate.take_commands(since) or ["(no command)"],
        observed=observed,
        expected=expected,
        observer=observer,
    )


def step_init(gate: SetupGate) -> None:
    project = gate.new_project("init")
    watch = (project, gate.xdg_config, gate.xdg_state)

    def tree() -> dict[str, str]:
        return snapshot(*watch, base=gate.root)

    start = len(gate.commands)
    before = tree()
    preview_process = gate.cli(*_init_args(project, "--required-check", "CI"))
    preview = _json(preview_process)
    after = tree()
    files = {item["role"]: item for item in preview.get("files", [])}
    policy = files.get("project-policy", {})
    record = files.get("ownership-record", {})
    _record(
        gate,
        "setup-preview-writes-nothing",
        passed=(
            preview_process.returncode == 0
            and before == after
            and preview.get("mode") == "preview"
            and policy.get("action") == "create"
            and policy.get("tracking") == "tracked"
            and record.get("action") == "create"
            and record.get("tracking") == "private"
            and preview.get("network_access") == []
            and preview.get("installs") == []
            and {item["name"] for item in preview.get("auth_requirements", [])}
            == {"github", "harness"}
            and preview.get("performed") == []
        ),
        since=start,
        observed={
            "exit": preview_process.returncode,
            "changed_paths": _changed(before, after),
            "files": preview.get("files"),
            "network_access": preview.get("network_access"),
            "installs": preview.get("installs"),
            "auth_requirements": [i["name"] for i in preview.get("auth_requirements", [])],
        },
        expected="exit 0; no path changed; policy tracked create; record private create; "
        "no network access; no installs; github and harness auth requirements",
        observer={
            "filesystem_before_after": {"paths": len(before), "changed": _changed(before, after)}
        },
    )

    start = len(gate.commands)
    apply_process = gate.cli(*_init_args(project, "--required-check", "CI"), "--apply")
    applied = _json(apply_process)
    previewed = {item["path"]: item["sha256"] for item in preview.get("files", [])}
    written_ok = set(applied.get("performed", [])) == set(previewed) and all(
        (Path(path).is_dir() if not digest else _sha(Path(path).read_bytes()) == digest)
        for path, digest in previewed.items()
    )
    after_apply = tree()
    created = sorted(set(after_apply) - set(before))
    previewed_keys = {str(Path(path).relative_to(gate.root)) for path in previewed}
    allowed = set(previewed_keys)
    for key in previewed_keys:
        allowed.update(str(parent) for parent in Path(key).parents if parent != Path("."))
    changed = _changed(before, after_apply)
    exact_creation = (
        set(created) <= allowed and set(changed) <= allowed and previewed_keys <= set(after_apply)
    )
    _record(
        gate,
        "setup-apply-performs-previewed-plan",
        passed=apply_process.returncode == 0 and written_ok and exact_creation,
        since=start,
        observed={
            "exit": apply_process.returncode,
            "performed": applied.get("performed"),
            "previewed": sorted(previewed),
            "created_paths": created,
            "changed_paths": changed,
        },
        expected="exit 0; performed paths equal the previewed paths; every previewed path exists; "
        "only previewed paths and their ancestor directories change; file sha256s match",
        observer={"filesystem_before_after": {"created": created, "changed": changed}},
    )

    start = len(gate.commands)
    repeat_process = gate.cli(*_init_args(project, "--required-check", "CI"), "--apply")
    repeated = _json(repeat_process)
    after_repeat = tree()
    _record(
        gate,
        "setup-repeat-init-no-change",
        passed=(
            repeat_process.returncode == 0
            and repeated.get("performed") == []
            and after_repeat == after_apply
        ),
        since=start,
        observed={
            "exit": repeat_process.returncode,
            "performed": repeated.get("performed"),
            "changed_paths": _changed(after_apply, after_repeat),
        },
        expected="exit 0; nothing performed; no path changed",
        observer={"filesystem_before_after": {"changed": _changed(after_apply, after_repeat)}},
    )

    start = len(gate.commands)
    policy_path = Path(policy["path"]) if policy else project / ".enginery" / "project.toml"
    original = policy_path.read_bytes()
    conflict_process = gate.cli(*_init_args(project, "--required-check", "OTHER"), "--apply")
    conflict = _json(conflict_process)
    _record(
        gate,
        "setup-conflict-user-owned-not-overwritten",
        passed=(
            conflict_process.returncode == 9
            and conflict.get("status") == "conflict"
            and policy_path.read_bytes() == original
            and tree() == after_apply
        ),
        since=start,
        observed={
            "exit": conflict_process.returncode,
            "status": conflict.get("status"),
            "conflicts": conflict.get("conflicts"),
        },
        expected="exit 9; status conflict; the policy bytes and the whole tree are unchanged",
        observer={
            "policy_sha256_before": _sha(original),
            "policy_sha256_after": _sha(policy_path.read_bytes()),
        },
    )


def _submit(
    gate: SetupGate,
    name: str,
    *,
    user: str | None,
    init_extra: Sequence[str],
    run_flags: Sequence[str],
) -> dict[str, Any]:
    """Set up a project, build a request, start it and observe what the adapter submitted."""
    gate.write_user(user)
    project = gate.new_project(name)
    init = gate.cli(*_init_args(project, *init_extra), "--apply")
    if init.returncode != 0:
        raise RuntimeError(f"factory init failed: {init.stderr.strip()[:300]}")
    # New runs require operator-owned behavioral checks bound at the base revision.
    policy = project / ".enginery" / "project.toml"
    with policy.open("a", encoding="utf-8") as stream:
        stream.write(
            '\n[[acceptance.checks]]\nname = "project-content"\nobserver = "command"\n'
            'argv = ["cat", "README"]\nfault_classes = ["constant_result"]\n'
            '[[acceptance.checks.assertions]]\nkind = "stdout"\n'
            'contains = "disposable project"\n'
        )
    gate.git(project, "add", ".enginery/project.toml")
    gate.git(project, "commit", "-q", "-m", "declare project behavioral acceptance")
    run_dir = gate.runs / name
    run_dir.mkdir()
    request = run_dir / "request.json"
    build = gate.cli(
        "stage1",
        "build-request",
        "--output",
        str(request),
        "--run-id",
        f"run-{name}",
        "--repository",
        "owner/repo",
        "--external-reference",
        "owner/repo#1",
        "--base-revision",
        gate.git(project, "rev-parse", "HEAD"),
        "--repository-path",
        str(project),
        "--workspace-path",
        str(run_dir / "workspace"),
        "--artifact-root",
        str(run_dir / "artifacts"),
        *run_flags,
    )
    if build.returncode != 0:
        raise RuntimeError(f"build-request failed: {build.stderr.strip()[:300]}")
    document = json.loads(request.read_text(encoding="utf-8"))
    fields = document["effective_config"]["fields"]
    database = str(run_dir / "ledger.db")
    common = ("--database", database, "--owner", "gate")
    started = gate.cli("stage1", "start", *common, "--request", str(request))
    if started.returncode != 0:
        raise RuntimeError(f"stage1 start failed: {started.stderr.strip()[:300]}")
    already = len(gate.launches())
    for _ in range(20):
        step = gate.cli("stage1", "watch", *common, "--run-id", f"run-{name}", "--advance")
        if step.returncode != 0:
            raise RuntimeError(f"stage1 watch failed: {step.stderr.strip()[:300]}")
        state = json.loads(step.stdout.strip().splitlines()[-1])
        gate.raw_write(f"submission/{name}-state.json", state)
        implement = next((n for n in state["nodes"] if n["node_id"] == "implement"), None)
        if implement is not None and implement["status"] in ("failed", "passed"):
            break
        time.sleep(0.4)
    launches = gate.launches()[already:]
    if not launches:
        raise RuntimeError("the Stage 1 adapter never submitted a worker to the harness")
    argv = launches[0]["argv"]

    def flag(name_: str) -> str | None:
        return argv[argv.index(name_) + 1] if name_ in argv else None

    return {
        "case": name,
        "submitted": {"model": flag("--model"), "effort": flag("--thinking")},
        "request_fields": {
            "model": [fields["model"]["value"], fields["model"]["winner"]],
            "effort": [fields["effort"]["value"], fields["effort"]["winner"]],
        },
        "harness_argv_flags": [item for item in argv if item.startswith("--")],
    }


_USER = '[execution]\nmodel = "openai/gpt-user"\neffort = "low"\n'


def step_layers(gate: SetupGate) -> None:
    cases = (
        (
            "layer-user-reaches-submission",
            "user",
            {"user": "schema_version = 1\n" + _USER, "init_extra": (), "run_flags": ()},
            ("openai/gpt-user", "low", "user"),
        ),
        (
            "layer-project-over-user-reaches-submission",
            "project",
            {
                "user": "schema_version = 1\n" + _USER,
                "init_extra": ("--model", "openai/gpt-project", "--effort", "medium"),
                "run_flags": (),
            },
            ("openai/gpt-project", "medium", "project"),
        ),
        (
            "layer-run-over-project-reaches-submission",
            "run",
            {
                "user": "schema_version = 1\n" + _USER,
                "init_extra": ("--model", "openai/gpt-project", "--effort", "medium"),
                "run_flags": ("--model", "openai/gpt-run", "--effort", "high"),
            },
            ("openai/gpt-run", "high", "run"),
        ),
    )
    for assertion_id, name, arguments, (model, effort, winner) in cases:
        start = len(gate.commands)
        try:
            observed = _submit(gate, name, **arguments)
        except Exception as error:
            gate.evidence.fail_assertion(
                assertion_id, f"{type(error).__name__}: {error}", commands=gate.take_commands(start)
            )
            continue
        passed = (
            observed["submitted"] == {"model": model, "effort": effort}
            and observed["request_fields"]["model"] == [model, winner]
            and observed["request_fields"]["effort"] == [effort, winner]
        )
        _record(
            gate,
            assertion_id,
            passed=passed,
            since=start,
            observed=observed,
            expected=f"the harness argv carries --model {model} --thinking {effort}, and both "
            f"compiled fields name the {winner} layer as winner",
            observer={
                "recorded_harness_invocation": observed["harness_argv_flags"],
                "submitted": observed["submitted"],
            },
        )
    gate.write_user(None)


def step_refusals(gate: SetupGate) -> None:
    def write_policy(name: str, body: str) -> Path:
        project = gate.new_project(name)
        policy = project / ".enginery" / "project.toml"
        policy.parent.mkdir()
        policy.write_text(f"schema_version = 1\n{body}", encoding="utf-8")
        return project

    def build(
        project: Path, name: str, *extra: str
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        run_dir = gate.runs / name
        run_dir.mkdir()
        request = run_dir / "request.json"
        process = gate.cli(
            "stage1",
            "build-request",
            "--output",
            str(request),
            "--run-id",
            f"run-{name}",
            "--repository",
            "owner/repo",
            "--external-reference",
            "owner/repo#1",
            "--base-revision",
            gate.git(project, "rev-parse", "HEAD"),
            "--repository-path",
            str(project),
            "--workspace-path",
            str(run_dir / "workspace"),
            "--artifact-root",
            str(run_dir / "artifacts"),
            *extra,
        )
        return process, request

    start = len(gate.commands)
    constrained = write_policy(
        "constrained",
        '[runner]\nmode = "trusted-local"\n[execution]\neffort = "low"\n'
        '[constraints]\nallowed_efforts = ["low", "medium"]\n',
    )
    policy_file = str(constrained / ".enginery" / "project.toml")
    refused, refused_request = build(constrained, "refused", "--effort", "high")
    allowed, allowed_request = build(constrained, "allowed", "--effort", "medium")
    gate.evidence.control(
        "hard-policy-allows-a-value-inside-the-constraint",
        description="the same run layer with an allowed effort builds a request",
        passed=allowed.returncode == 0 and allowed_request.exists(),
        observed={"exit": allowed.returncode},
        expected="exit 0 and a request file",
        commands=gate.take_commands(start)[-1:],
    )
    _record(
        gate,
        "hard-policy-refuses-run-override",
        passed=(
            refused.returncode == 2
            and not refused_request.exists()
            and policy_file in refused.stderr
            and "violates" in refused.stderr
        ),
        since=start,
        observed={
            "exit": refused.returncode,
            "stderr": refused.stderr.strip()[:400],
            "request_written": refused_request.exists(),
        },
        expected="exit 2; no request file; the refusal names the project policy file",
        observer={"request_exists_on_disk": refused_request.exists()},
    )

    start = len(gate.commands)
    unknown = write_policy(
        "unknown-key", '[runner]\nmode = "trusted-local"\n[execution]\nplanet = 1\n'
    )
    process, request = build(unknown, "unknown")
    _record(
        gate,
        "unknown-key-refused",
        passed=(
            process.returncode == 2
            and not request.exists()
            and str(unknown / ".enginery" / "project.toml") in process.stderr
            and "execution.planet" in process.stderr
        ),
        since=start,
        observed={"exit": process.returncode, "stderr": process.stderr.strip()[:400]},
        expected="exit 2; no request file; the error names the file and execution.planet",
        observer={"request_exists_on_disk": request.exists()},
    )


def step_doctor(gate: SetupGate) -> None:
    start = len(gate.commands)
    project = gate.new_project("doctor")
    gate.cli(*_init_args(project), "--apply")
    tree_before = snapshot(project, gate.xdg_config, gate.xdg_state, base=gate.root)
    ok = gate.cli("factory", "doctor", "--project", str(project), "--json")
    held = gate.cli(
        "factory",
        "doctor",
        "--project",
        str(project),
        "--json",
        extra_env={"GATE_GH_AUTH_EXIT": "1"},
    )
    report = _json(held)
    holds = {item["name"]: item for item in report.get("holds", [])}
    tree_after = snapshot(project, gate.xdg_config, gate.xdg_state, base=gate.root)
    _record(
        gate,
        "doctor-hold-names-resolving-command",
        passed=(
            ok.returncode == 0
            and _json(ok).get("status") == "ok"
            and held.returncode == 5
            and report.get("status") == "hold"
            and holds.get("github-auth", {}).get("resolve") == "gh auth login"
            and tree_before == tree_after
        ),
        since=start,
        observed={
            "satisfied_exit": ok.returncode,
            "unauthenticated_exit": held.returncode,
            "holds": [{"name": k, "resolve": v.get("resolve")} for k, v in holds.items()],
            "changed_paths": _changed(tree_before, tree_after),
        },
        expected="exit 0 when satisfied; exit 5 with a github-auth hold whose resolving command "
        "is `gh auth login` when unauthenticated; nothing changed",
        observer={"stand_in_gh_calls": [c for c in gate.commands if "doctor" in c]},
    )


def step_uninstall(gate: SetupGate) -> None:
    gate.write_user("schema_version = 1\n")
    user_bytes = gate.user_file().read_bytes()
    clean = gate.new_project("uninstall-clean")
    modified = gate.new_project("uninstall-modified")
    for project in (clean, modified):
        gate.cli(*_init_args(project), "--apply")
        bystander = project / ".enginery" / "capabilities.lock.json"
        bystander.write_text('{"entries": []}', encoding="utf-8")
        (project / "ledger.db").write_bytes(b"ledger")
    policy_modified = modified / ".enginery" / "project.toml"
    policy_modified.write_text(
        policy_modified.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8"
    )
    edited = policy_modified.read_bytes()
    bystanders = {
        path: path.read_bytes()
        for project in (clean, modified)
        for path in (project / ".enginery" / "capabilities.lock.json", project / "ledger.db")
    }

    def tree() -> dict[str, str]:
        return snapshot(clean, modified, gate.xdg_config, gate.xdg_state, base=gate.root)

    start = len(gate.commands)
    before = tree()
    preview = gate.cli("factory", "uninstall", "--project", str(clean), "--json")
    _record(
        gate,
        "uninstall-preview-writes-nothing",
        passed=preview.returncode == 0
        and tree() == before
        and _json(preview).get("mode") == "preview",
        since=start,
        observed={
            "exit": preview.returncode,
            "changed_paths": _changed(before, tree()),
            "files": _json(preview).get("files"),
        },
        expected="exit 0; mode preview; no path changed",
        observer={"filesystem_before_after": {"changed": _changed(before, tree())}},
    )

    start = len(gate.commands)
    applied = gate.cli("factory", "uninstall", "--project", str(clean), "--apply", "--json")
    plan = _json(applied)
    policy_clean = clean / ".enginery" / "project.toml"
    _record(
        gate,
        "uninstall-removes-only-owned-unmodified",
        passed=(
            applied.returncode == 0
            and not policy_clean.exists()
            and all(
                path.read_bytes() == content
                for path, content in bystanders.items()
                if path.exists()
            )
            and all(path.exists() for path in bystanders)
            and gate.user_file().read_bytes() == user_bytes
            and plan.get("ownership_record", {}).get("action") == "remove"
        ),
        since=start,
        observed={
            "exit": applied.returncode,
            "removed": plan.get("removed"),
            "bystanders_intact": all(
                p.read_bytes() == c for p, c in bystanders.items() if p.exists()
            ),
        },
        expected="exit 0; the unmodified policy and the record are removed; the capability "
        "lockfile, ledger and user.toml are byte-identical",
        observer={
            "policy_exists": policy_clean.exists(),
            "bystander_sha256": {
                str(p.relative_to(gate.root)): _sha(c) for p, c in bystanders.items()
            },
        },
    )

    start = len(gate.commands)
    kept = gate.cli("factory", "uninstall", "--project", str(modified), "--apply", "--json")
    kept_plan = _json(kept)
    after_first = tree()
    again_clean = gate.cli("factory", "uninstall", "--project", str(clean), "--apply", "--json")
    again_modified = gate.cli(
        "factory", "uninstall", "--project", str(modified), "--apply", "--json"
    )
    _record(
        gate,
        "uninstall-keeps-user-modified-and-is-idempotent",
        passed=(
            kept.returncode == 0
            and policy_modified.read_bytes() == edited
            and str(policy_modified) in kept_plan.get("kept_user_owned", [])
            and again_clean.returncode == 0
            and again_modified.returncode == 0
            and _json(again_clean).get("status") == "nothing-to-uninstall"
            and _json(again_modified).get("status") == "nothing-to-uninstall"
            and tree() == after_first
        ),
        since=start,
        observed={
            "kept_exit": kept.returncode,
            "kept_user_owned": kept_plan.get("kept_user_owned"),
            "second_run_status": [
                _json(again_clean).get("status"),
                _json(again_modified).get("status"),
            ],
            "changed_by_second_run": _changed(after_first, tree()),
        },
        expected="the edited policy is kept byte-identical and reported user-owned; a second "
        "uninstall of either project is a no-op",
        observer={
            "policy_sha256": _sha(policy_modified.read_bytes()),
            "edited_sha256": _sha(edited),
        },
    )
    gate.write_user(None)


def step_boundaries(
    gate: SetupGate, globals_before: dict[str, str], home_before: dict[str, str]
) -> None:
    start = len(gate.commands)
    globals_after = gate.global_snapshot()
    home_after = gate.home_snapshot()
    outside = [
        key
        for key in _changed(home_before, home_after)
        if not any(key.startswith(f"home/{prefix}") for prefix in _PRIVATE_PREFIXES)
    ]
    gate.effects["global_location_changes"] = len(_changed(globals_before, globals_after))
    _record(
        gate,
        "global-locations-byte-identical",
        passed=globals_before == globals_after and not outside,
        since=start,
        observed={
            "global_paths_checked": len(globals_before),
            "global_changes": _changed(globals_before, globals_after),
            "changed_outside_private_locations": outside,
        },
        expected="every global location is byte-identical; every changed path is inside "
        "Enginery's private config or state directory",
        observer={
            "checked_roots": list(_GLOBAL_ROOTS),
            "private_prefixes": list(_PRIVATE_PREFIXES),
        },
    )
    launches = gate.launches()
    resolved = shutil.which("omp", path=gate.environment()["PATH"])
    gate.effects.update(hosted_sessions=0, harness_launches_recorded=len(launches))
    _record(
        gate,
        "no-hosted-session",
        passed=resolved == str(gate.bin / "omp") and len(launches) >= 3,
        since=start,
        observed={
            "omp_resolves_to_stand_in": resolved == str(gate.bin / "omp"),
            "recorded_stand_in_launches": len(launches),
            "hosted_sessions": 0,
        },
        expected="`omp` resolves to the local recording stand-in and every harness launch was "
        "recorded by it",
        observer={"stand_in_launch_count": len(launches)},
    )


def run(gate: SetupGate) -> None:
    globals_before = gate.global_snapshot()
    home_before = gate.home_snapshot()
    steps: tuple[tuple[Callable[[SetupGate], None], tuple[str, ...]], ...] = (
        (step_init, tuple(i for i in REQUIRED if i.startswith("setup-"))),
        (step_layers, tuple(i for i in REQUIRED if i.startswith("layer-"))),
        (step_refusals, ("hard-policy-refuses-run-override", "unknown-key-refused")),
        (step_doctor, ("doctor-hold-names-resolving-command",)),
        (step_uninstall, tuple(i for i in REQUIRED if i.startswith("uninstall-"))),
    )
    for step, ids in steps:
        mark = len(gate.commands)
        try:
            step(gate)
        except Exception as error:
            for assertion_id in ids:
                gate.evidence.fail_assertion(
                    assertion_id,
                    f"{type(error).__name__}: {error}",
                    commands=gate.take_commands(mark),
                )
    try:
        step_boundaries(gate, globals_before, home_before)
    except Exception as error:
        for assertion_id in ("global-locations-byte-identical", "no-hosted-session"):
            gate.evidence.fail_assertion(assertion_id, f"{type(error).__name__}: {error}")


def finalize(gate: SetupGate) -> None:
    """Nothing to verify after cleanup: every assertion was observed while the home existed."""


__all__ = ["REQUIRED", "SCENARIO", "SetupGate", "finalize", "prepare", "required_assertions", "run"]
