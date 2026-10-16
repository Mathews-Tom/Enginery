"""Local, real-CLI manual factory journey with deterministic lost-client/PR-response windows.

The stand-ins implement only the GitHub and harness subprocess protocols. The product owns
request compilation, dispatch, workspace creation, publication, reconciliation and review.
Every required observation is fail-closed; an interrupted prerequisite never passes its
unexercised dependants. No engine, credentials or hosted inference is used.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enginery.cli.config_locations import private_state_directory, project_identity
from enginery.engine.operator_control import live_workers
from enginery.engine.recovery import assess_worker
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import stage1_request_from_state
from factory_gate_context import GateHoldError, GateOptions
from factory_gate_evidence import EvidenceBuilder
from factory_gate_setup import SetupGate, _changed, snapshot

SCENARIO = "manual-path"
REPOSITORY = "owner/repo"
ISSUE = REPOSITORY + "#1"
SECOND_ISSUE = REPOSITORY + "#4"
UPDATED_AT = "2026-10-13T00:00:00Z"

REQUIRED: dict[str, str] = {
    "init-policy-and-user-executables": "init --apply writes project policy; executables are user-only",
    "dry-collect-zero-effects": "dry collect lists one eligible issue and one risk hold, no PR or write",
    "collect-private-cursor": "collect discloses the private index/lock and persists its issue cursor",
    "run-bound-dispatch": "run compiles the deterministic durable request and dispatches a live worker",
    "tampered-request-refused": "stage1 start refuses a tampered request through configuration binding",
    "client-kill-worker-survives": "SIGKILL of the waiting client leaves the same durable worker alive",
    "client-resume-same-identity": "resume collects the exited worker without a new request/workspace",
    "status-explain-binding": "status/explain expose the bound request, settings, sources and budget",
    "repeat-run-same-identity": "factory run of the same snapshot resumes the existing request and run",
    "terminal-review-approved": "stage1 review accepts a terminal independent approving report",
    "coordinator-kill-after-pr": "SIGKILL occurs after PR creation before its response is delivered",
    "coordinator-resume-single-pr": "resume reconciles the same operation to one PR and the same head",
    "verified-open-pr-outcome": "the run succeeds with verified open PR checks/reviews and no merge",
    "retained-workspace-hold-repair": (
        "a second run holds on the first retained workspace and names its exact release command"
    ),
    "pause-second-issue-hold": "project pause blocks a second bound issue and names pause --lift",
    "drain-releases-resources": "drain waits through the existing path and leaves no live resource",
    "pause-lift-and-list-both": "pause can be lifted and status lists both durable run identities",
    "global-locations-byte-identical": "global locations and non-private home paths stay unchanged",
    "local-only-effect-counts": "all calls are recorded local stand-ins; explicit effects, no hosted/OCI",
}

# This extends the read-only issue fixture protocol in tests/support/github.py. Kept in scripts
# so a copied scripts directory works with an installed wheel, without importing the test tree.
_GH = r'''#!__PYTHON__
import json, os, subprocess, sys, time
from pathlib import Path
from urllib.parse import parse_qs
here = Path(__file__).resolve().parent
args = sys.argv[1:]
with (here / "gh-calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["auth", "status"]:
    raise SystemExit(0)
if args == ["--version"]:
    print("gh version 0.0.0 (manual-path local stand-in)")
    raise SystemExit(0)
if not args or args[0] != "api":
    raise SystemExit("unsupported gh command")
method = args[args.index("--method") + 1] if "--method" in args else "GET"
endpoint = next((arg for arg in args if arg.startswith("repos/")), "")
path, _, query = endpoint.partition("?")
parts = path.split("/")
page = int(parse_qs(query).get("page", ["1"])[0])
fields = dict(arg.split("=", 1) for i, arg in enumerate(args) if i and args[i-1] == "--raw-field")
state = json.loads((here / "gh-state.json").read_text(encoding="utf-8"))
pr_file = here / "gh-pr.json"
pr = json.loads(pr_file.read_text(encoding="utf-8")) if pr_file.exists() else None
def answer(value):
    print(json.dumps(value))
    raise SystemExit(0)
def issue(number):
    labels = ["enginery/work-kind/issue"] + ([] if number == 2 else ["enginery/risk/low"])
    return {"number": number, "url": "https://api.github.com/repos/owner/repo/issues/" + str(number),
            "title": "Bounded local change " + str(number), "body": "Change one bounded behavior.",
            "updated_at": "2026-10-13T00:00:00Z", "labels": [{"name": n} for n in labels]}
if parts[:3] != ["repos", "owner", "repo"]:
    raise SystemExit("unexpected repository")
if method == "GET" and parts[3:] == ["issues"]:
    records = [issue(1), issue(2), {**issue(3), "pull_request": {"url": "local-pr"}}]
    answer(records if page == 1 else [])
comments_file = here / "gh-lifecycle-comments.json"
comments = json.loads(comments_file.read_text()) if comments_file.exists() else []
if method == "GET" and parts[3:] == ["issues", "comments"]:
    answer(comments if page == 1 else [])
if method == "POST" and parts[3:] == ["issues", "1", "comments"]:
    body = fields.get("body", "")
    if not body.startswith("<!-- enginery:lifecycle:"):
        raise SystemExit("only existing Stage 1 lifecycle projection is allowed")
    comment = {"id": len(comments) + 1, "body": body}
    comments.append(comment)
    comments_file.write_text(json.dumps(comments), encoding="utf-8")
    answer(comment)
if method == "GET" and len(parts) in (5, 6) and parts[3] == "issues":
    record = issue(int(parts[4]))
    if len(parts) == 5:
        answer(record)
    if parts[5] == "labels":
        answer(record["labels"] if page == 1 else [])
if method == "GET" and parts[3:] == ["pulls"]:
    filters = parse_qs(query)
    matches = pr is not None and page == 1
    if matches and "head" in filters:
        matches = filters["head"][0] == "owner:" + pr["head"]["ref"]
    if matches and "base" in filters:
        matches = filters["base"][0] == pr["base"]["ref"]
    answer([pr] if matches else [])
if method == "POST" and parts[3:] == ["pulls"]:
    if pr is not None:
        raise SystemExit("a second PR POST is forbidden")
    def revision(branch):
        return subprocess.check_output(["git", "--git-dir", state["origin"], "rev-parse", branch], text=True).strip()
    pr = {"number": 17, "html_url": "https://github.com/owner/repo/pull/17", "state": "open",
          "merged": False, "mergeable": True, "title": fields["title"], "body": fields["body"],
          "head": {"ref": fields["head"], "sha": revision(fields["head"])},
          "base": {"ref": fields["base"], "sha": revision(fields["base"])}}
    temporary = pr_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(pr), encoding="utf-8")
    temporary.replace(pr_file)
    window = here / "pr-created.tmp"
    window.write_text(json.dumps({"pid": os.getpid(), "pr": pr}), encoding="utf-8")
    window.replace(here / "pr-created.json")
    deadline = time.monotonic() + 120
    while not (here / "release-pr").exists():
        if time.monotonic() > deadline:
            raise SystemExit("PR response window timed out")
        time.sleep(0.02)
    answer(pr)
if method == "PATCH" and pr is not None and parts[3:] == ["pulls", str(pr["number"])]:
    for name in ("title", "body"):
        if name in fields:
            pr[name] = fields[name]
    if fields.get("base", pr["base"]["ref"]) != pr["base"]["ref"]:
        raise SystemExit("base changed across retry")
    pr_file.write_text(json.dumps(pr), encoding="utf-8")
    answer(pr)
if method == "GET" and pr is not None:
    if parts[3:] == ["pulls", str(pr["number"])]:
        answer(pr)
    if parts[3:] == ["pulls", str(pr["number"]), "reviews"]:
        answer([{"user": {"login": "independent-reviewer"}, "state": "APPROVED", "commit_id": pr["head"]["sha"]}] if page == 1 else [])
    if parts[3:] == ["commits", pr["head"]["sha"], "check-runs"]:
        answer({"check_runs": [{"name": "CI", "status": "completed", "conclusion": "success", "head_sha": pr["head"]["sha"]}] if page == 1 else []})
sys.stderr.write("unsupported stand-in endpoint: " + method + " " + endpoint + "\n")
raise SystemExit(1)
'''

_OMP = r'''#!__PYTHON__
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
if args in (["--help"], ["--version"]):
    print("omp v18.10.3")
    raise SystemExit(0)
workspace = Path.cwd()
with (here.parent / "harness-launches.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"pid": os.getpid(), "workspace": str(workspace), "argv": args}) + "\n")
print(json.dumps({"type": "session"}), flush=True)
print(json.dumps({"type": "agent_start"}), flush=True)
(workspace / "manual-path-result.txt").write_text("one bounded local change\n", encoding="utf-8")
def git(*arguments):
    subprocess.run(["git", "-c", "user.name=manual path", "-c", "user.email=gate@example.invalid", *arguments], check=True, capture_output=True)
git("checkout", "-b", "enginery/" + workspace.parent.name)
git("add", "manual-path-result.txt")
git("commit", "-q", "-m", "feat: make bounded local change")
deadline = time.monotonic() + 120
while not (here / "release-worker").exists():
    if time.monotonic() > deadline:
        raise SystemExit("worker window timed out")
    time.sleep(0.02)
print(json.dumps({"type": "agent_end"}), flush=True)
'''


def required_assertions(options: GateOptions) -> dict[str, str]:
    return dict(REQUIRED)


def _read(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _wait(condition: Callable[[], bool], description: str, *, seconds: float = 45) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for {description}")
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ManualPathGate(SetupGate):
    """Reuse setup's environment, filesystem snapshots, identities and raw evidence boundary."""

    def __init__(self, options: GateOptions, evidence: EvidenceBuilder) -> None:
        super().__init__(options, evidence)
        self.clients: list[subprocess.Popen[str]] = []
        self.completed: list[dict[str, Any]] = []
        self.recorded: set[str] = set()
        self.worker_groups: set[int] = set()
        self.project = self.projects / "manual-path"
        self.state = private_state_directory(self.project, self.environment())
        self.run_id = ""
        self.second_run_id = ""
        self.request_bytes = b""
        self.workspace: Path | None = None
        self.workspace_identity: tuple[int, int] | None = None
        self.database = self.state / "ledger.sqlite"

    def _write_stand_ins(self) -> None:
        for name, body in (("gh", _GH), ("omp", _OMP)):
            executable = self.bin / name
            executable.write_text(body.replace("__PYTHON__", sys.executable), encoding="utf-8")
            executable.chmod(0o755)

    def cli(
        self, *arguments: str, extra_env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = super().cli(*arguments, extra_env=extra_env)
        self._capture(arguments, result.returncode, result.stdout, result.stderr)
        return result

    def _capture(self, args: tuple[str, ...], code: int, stdout: str, stderr: str) -> None:
        try:
            document = json.loads(stdout)
        except json.JSONDecodeError:
            document = None
        self.completed.append({
            "argv": list(args), "exit_code": code, "stdout": stdout,
            "stderr": stderr, "json": document,
        })

    def factory(self, command: str, *arguments: str) -> tuple[int, dict[str, Any]]:
        result = self.cli("factory", command, *arguments, "--project", str(self.project), "--json")
        if not result.stdout.strip():
            raise RuntimeError(f"factory {command} exited {result.returncode}: {result.stderr}")
        return result.returncode, dict(json.loads(result.stdout))

    def launch(self, command: str, *arguments: str) -> subprocess.Popen[str]:
        args = ("factory", command, *arguments, "--project", str(self.project), "--json")
        self.commands.append("enginery " + shlex.join(args))
        client = subprocess.Popen(
            [sys.executable, "-m", "enginery.cli.main", *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment(), cwd=self.root, start_new_session=True,
        )
        self.clients.append(client)
        return client

    def kill_client(self, client: subprocess.Popen[str]) -> dict[str, Any]:
        if client.poll() is not None:
            stdout, stderr = client.communicate()
            raise RuntimeError(f"client exited before kill window: {client.returncode}: {stdout} {stderr}")
        client.kill()
        stdout, stderr = client.communicate(timeout=10)
        observed = {
            "argv": client.args, "pid": client.pid, "exit_code": client.returncode,
            "stdout": stdout, "stderr": stderr, "json": None, "signal": "SIGKILL",
        }
        self.completed.append(observed)
        return observed

    def record(self, key: str, passed: bool, observed: Any, observer: Any) -> None:
        self.evidence.assertion(
            key, passed=passed, commands=list(self.commands), observed=observed,
            expected=REQUIRED[key], observer=observer,
        )
        self.recorded.add(key)

    def index(self) -> dict[str, Any]:
        return _read(self.state / "intake" / "index.json")

    def request_path(self) -> Path:
        if not self.run_id:
            raise RuntimeError("run not bound")
        return self.state / "runs" / self.run_id / "request.json"

    def same_identity(self) -> bool:
        if self.workspace is None or self.workspace_identity is None:
            return False
        identity = self.workspace.stat()
        return (self.request_path().read_bytes() == self.request_bytes
                and (identity.st_dev, identity.st_ino) == self.workspace_identity
                and self.index()["repositories"][REPOSITORY]["issues"][ISSUE]["run_id"] == self.run_id)

    def gh_calls(self) -> list[list[str]]:
        path = self.bin / "gh-calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def cleanup(self) -> None:
        for name in ("release-pr", "release-worker"):
            (self.bin / name).touch()
        # Kill only the gate's still-owned clients and identity-proven live workers.
        for client in self.clients:
            if client.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(client.pid, signal.SIGKILL)
                client.communicate(timeout=10)
        if self.database.exists():
            ledger = LedgerService.open(self.database)
            try:
                for worker in live_workers(ledger):
                    identity = worker.identity
                    if identity is not None and identity.process_group_id in self.worker_groups:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(identity.process_group_id, signal.SIGKILL)
            finally:
                ledger.close()
        super().cleanup()


def prepare(
    options: GateOptions, evidence: EvidenceBuilder, scrubber: object, engine_factory: object,
    *, need_relay: bool,
) -> ManualPathGate:
    if shutil.which("git") is None:
        raise GateHoldError("git is required for the local manual-path scenario")
    gate = ManualPathGate(options, evidence)
    result = gate.cli("--version")
    if result.returncode:
        gate.cleanup()
        raise GateHoldError(f"enginery CLI unavailable: {result.stderr[:200]}")
    gate.commands.clear()
    gate.completed.clear()
    return gate


def step_prepare(gate: ManualPathGate) -> None:
    project = gate.new_project("manual-path")
    gate.project = project
    origin = gate.root / "origin" / "owner" / "repo.git"
    origin.parent.mkdir(parents=True)
    gate.git(project, "init", "--bare", str(origin))
    gate.git(project, "remote", "add", "origin", str(origin))
    branch = gate.git(project, "symbolic-ref", "--short", "HEAD")
    gate.git(project, "push", "-q", "origin", branch)
    (gate.bin / "gh-state.json").write_text(json.dumps({"origin": str(origin)}), encoding="utf-8")
    gate.write_user(
        "schema_version = 1\n[executables]\n"
        f"github = {json.dumps(str(gate.bin / 'gh'))}\n"
        f"harness = {json.dumps(str(gate.bin / 'omp'))}\n"
    )
    code, report = gate.factory(
        "init", "--apply", "--runner", "trusted-local", "--harness", "omp",
        "--validation-command", "true", "--required-check", "CI",
    )
    policy = project / ".enginery" / "project.toml"
    policy_text = policy.read_text(encoding="utf-8") if policy.exists() else ""
    gate.state = private_state_directory(project, gate.environment())
    gate.database = gate.state / "ledger.sqlite"
    gate.record("init-policy-and-user-executables",
                code == 0 and policy.exists() and "executables" not in policy_text
                and gate.git(project, "rev-list", "--count", "HEAD") == "1",
                {"exit": code, "json": report},
                {"policy": policy_text, "user": gate.user_file().read_text(encoding="utf-8"),
                 "origin": str(origin), "initial_commits": 1})


def step_collect(gate: ManualPathGate) -> None:
    before = snapshot(gate.project, gate.xdg_config, gate.xdg_state, base=gate.root)
    calls_before = len(gate.gh_calls())
    code, dry = gate.factory("collect", "--dry-run")
    rows = dry.get("candidates", [])
    eligible = [row for row in rows if row.get("eligible")]
    held = [row for row in rows if not row.get("eligible")]
    calls = gate.gh_calls()[calls_before:]
    effects = dry.get("external_effects", {})
    mutations = [call for call in calls if "--method" in call and call[call.index("--method") + 1] != "GET"]
    gate.record("dry-collect-zero-effects",
                code == 5 and len(eligible) == len(held) == 1
                and eligible[0]["external_reference"] == ISSUE
                and "risk" in json.dumps(held[0].get("holds", []))
                and eligible[0].get("dedup") == "new"
                and bool(eligible[0].get("workflow_profile"))
                and all(eligible[0].get("selection", {}).get(name, {}).get("winner")
                        for name in ("harness", "model", "effort"))
                and eligible[0].get("budget", {}).get("cost") == "unenforced"
                and effects.get("github_reads", 0) > 0
                and effects.get("github_mutations") == effects.get("hosted_inference_calls") == 0
                and dry.get("local_writes") == [] and not mutations
                and not (gate.state / "intake" / "index.json").exists()
                and before == snapshot(gate.project, gate.xdg_config, gate.xdg_state, base=gate.root),
                {"exit": code, "json": dry}, {"calls": calls, "index_exists": False})
    code, collected = gate.factory("collect")
    index_path = gate.state / "intake" / "index.json"
    index = gate.index()
    cursor = index["repositories"][REPOSITORY]["last_seen_updated_at"]
    writes = collected.get("local_writes", [])
    gate.record("collect-private-cursor",
                code == 5 and cursor == UPDATED_AT and str(index_path) in writes
                and all(Path(path).is_relative_to(gate.state) for path in writes)
                and index["repositories"][REPOSITORY]["issues"][ISSUE]["external_reference"] == ISSUE
                and collected.get("external_effects", {}).get("github_mutations") == 0,
                {"exit": code, "json": collected}, {"index": index, "path": str(index_path)})


def step_client_recovery(gate: ManualPathGate) -> None:
    client = gate.launch("run", ISSUE, "--wait-seconds", "30")
    _wait(lambda: bool(gate.launches()) or client.poll() is not None, "implementation launch")
    if not gate.launches():
        gate.kill_client(client)
        raise RuntimeError("implementation did not launch")
    entry = gate.index()["repositories"][REPOSITORY]["issues"][ISSUE]
    gate.run_id = entry["run_id"]
    canonical = json.dumps({"project": project_identity(gate.project), "issue": ISSUE,
                            "snapshot": entry["bound_digest"]}, sort_keys=True, separators=(",", ":"))
    expected = "fr-" + hashlib.sha256(canonical.encode()).hexdigest()[:20]
    gate.request_bytes = gate.request_path().read_bytes()
    request = _read(gate.request_path())
    gate.workspace = Path(request["workspace_path"])
    identity = gate.workspace.stat()
    gate.workspace_identity = (identity.st_dev, identity.st_ino)
    code, running = gate.factory("status", gate.run_id)
    worker = running.get("worker") or {}
    if worker.get("process_group_id"):
        gate.worker_groups.add(worker["process_group_id"])
    gate.record("run-bound-dispatch",
                code == 0 and gate.run_id == expected and bool(gate.request_bytes)
                and worker.get("liveness") == "alive" and _alive(worker["pid"])
                and Path(gate.launches()[0]["workspace"]) == gate.workspace,
                {"exit": code, "json": running},
                {"expected_run_id": expected, "launch": gate.launches()[0], "request": request})
    original_request = stage1_request_from_state(json.loads(gate.request_bytes))
    changed_request = replace(
        original_request,
        implementation=replace(
            original_request.implementation,
            time_budget_seconds=original_request.implementation.time_budget_seconds + 1,
        ),
    )
    tampered = {
        **changed_request.initial_state(),
        "effective_config": json.loads(gate.request_bytes)["effective_config"],
    }
    tampered_path = gate.root / "tampered-request.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    refused_db = gate.root / "tampered-ledger.sqlite"
    refused = gate.cli("stage1", "start", "--database", str(refused_db), "--owner", "factory",
                       "--request", str(tampered_path))
    ledger = LedgerService.open(refused_db)
    try:
        started = ledger.list_projections(aggregate_type="run")
    finally:
        ledger.close()
    gate.record("tampered-request-refused",
                refused.returncode != 0 and "time_budget_seconds" in refused.stderr
                and "configuration snapshot" in refused.stderr and not started,
                {"exit": refused.returncode, "stderr": refused.stderr},
                {"tampered_field": "implementation.time_budget_seconds", "run_projections": len(started)})
    killed = gate.kill_client(client)
    code, after = gate.factory("status", gate.run_id)
    after_worker = after.get("worker") or {}
    gate.record("client-kill-worker-survives",
                killed["exit_code"] == -signal.SIGKILL and code == 0
                and after.get("run_id") == gate.run_id
                and after_worker.get("pid") == worker.get("pid")
                and after_worker.get("liveness") == "alive" and _alive(worker["pid"]),
                {"killed": killed, "status": after}, {"harness_pid_alive": _alive(gate.launches()[0]["pid"])})
    (gate.bin / "release-worker").touch()
    # Probe the supervisor identity read-only: completion is not persisted by the dead
    # coordinator. Resume must itself observe/reconcile the now-exited worker.
    def completed() -> bool:
        ledger = LedgerService.open(gate.database)
        try:
            record = ledger.read_process_manager_state(process_manager_name="worker-supervisor",
                                                       state_key=f"{gate.run_id}:implement")
            return (
                record is not None
                and assess_worker(record.state).liveness.value in {"exited", "pid_reused"}
            )
        finally:
            ledger.close()
    _wait(completed, "supervised worker exit")
    wait_code, waited = gate.factory("drain", "--wait-seconds", "5")
    lift_code, lifted = gate.factory("pause", "--lift")
    code, resumed = gate.factory("resume", gate.run_id, "--wait-seconds", "30")
    commits = gate.git(
        gate.workspace, "rev-list", "--count",
        f"{request['run']['data']['base_revision']}..HEAD",
    )
    states = {node["node_id"]: node["status"] for node in resumed.get("nodes", [])}
    gate.record("client-resume-same-identity",
                code == wait_code == lift_code == 0
                and waited.get("drained") is True
                and waited.get("released") == "by resume/reconcile"
                and lifted.get("paused") is False
                and resumed.get("run_id") == gate.run_id
                and resumed.get("next_action") == "await_human_review"
                and states.get("implement") == "passed" and states.get("validate") == "passed"
                and gate.same_identity() and len(gate.launches()) == 1 and commits == "1",
                {"exit": code, "json": resumed, "drain_wait": waited, "pause_lift": lifted},
                {"request_sha256": hashlib.sha256(gate.request_bytes).hexdigest(),
                 "workspace_identity": gate.workspace_identity, "worker_commits": commits,
                 "launches": gate.launches()})


def step_inspection_review(gate: ManualPathGate) -> None:
    code, status = gate.factory("status", gate.run_id)
    explain_code, explained = gate.factory("explain", gate.run_id)
    request = _read(gate.request_path())
    gate.record("status-explain-binding",
                code == explain_code == 0
                and status.get("request_digest") == explained.get("request_digest")
                and bool(status.get("request_digest"))
                and status.get("configuration_snapshot_digest")
                == request["run"]["data"]["configuration_snapshot_digest"]
                and explained.get("configuration_snapshot_digest") == status.get("configuration_snapshot_digest")
                and explained.get("selection") == request["execution_selection"]
                and explained.get("runner") == request["runner"]
                and explained.get("permitted_capabilities") == request["implementation"]["permitted_capabilities"]
                and bool(explained.get("fields")) and bool(explained.get("nodes"))
                and status.get("budget", {}).get("time_budget_seconds") == request["implementation"]["time_budget_seconds"]
                and status.get("budget", {}).get("cost") == "unenforced",
                {"status_exit": code, "status": status, "explain_exit": explain_code, "explain": explained},
                {"request": request})
    code, repeated = gate.factory("run", ISSUE)
    gate.record("repeat-run-same-identity",
                code == 0 and repeated.get("run_id") == gate.run_id and gate.same_identity()
                and len(gate.launches()) == 1,
                {"exit": code, "json": repeated}, {"intake": gate.index(), "launches": gate.launches()})
    report_path = gate.root / "approving-review.json"
    report_path.write_text(json.dumps({"producer": "local-implementation", "reviewer": "human-terminal",
                                      "findings": []}), encoding="utf-8")
    result = gate.cli("stage1", "review", "--database", str(gate.database), "--owner", "factory",
                      "--run-id", gate.run_id, "--report", str(report_path), "--repair-attempt", "0")
    if not result.stdout.strip():
        raise RuntimeError(f"stage1 review exited {result.returncode}: {result.stderr}")
    report = dict(json.loads(result.stdout))
    gate.record("terminal-review-approved",
                result.returncode == 0 and report.get("review_outcome") == "approved",
                {"exit": result.returncode, "json": report}, {"report": _read(report_path)})


def step_pr_recovery(gate: ManualPathGate) -> None:
    coordinator = gate.launch("resume", gate.run_id, "--wait-seconds", "30")
    window = gate.bin / "pr-created.json"
    _wait(lambda: window.exists() or coordinator.poll() is not None, "PR creation response window")
    if not window.exists():
        gate.kill_client(coordinator)
        raise RuntimeError("coordinator never reached PR creation")
    persisted = _read(window)
    pr = persisted["pr"]
    killed = gate.kill_client(coordinator)
    # The stand-in remains blocked until the coordinator has been killed. No PR response
    # can have reached it; terminate only this known child after recording the window.
    still_waiting = _alive(persisted["pid"]) and not (gate.bin / "release-pr").exists()
    with contextlib.suppress(ProcessLookupError):
        os.kill(persisted["pid"], signal.SIGKILL)
    (gate.bin / "release-pr").touch()
    gate.record("coordinator-kill-after-pr",
                killed["exit_code"] == -signal.SIGKILL and still_waiting
                and _read(gate.bin / "gh-pr.json") == pr,
                {"killed": killed, "pr": pr}, {"response_still_blocked": still_waiting, "hook": persisted})
    code, resumed = gate.factory("resume", gate.run_id, "--wait-seconds", "30")
    status_code, status = gate.factory("status", gate.run_id)
    calls = gate.gh_calls()
    creates = [call for call in calls if "--method" in call
               and call[call.index("--method") + 1] == "POST"
               and f"repos/{REPOSITORY}/pulls" in call]
    node = next((node for node in status.get("nodes", []) if node["node_id"] == "open_pr"), {})
    bound_pr = status.get("pull_request") or {}
    operation = node.get("operation_id")
    matching_posts = [call for call in creates if operation and any(
        f"enginery-operation:{operation}" in value or str(operation) in value for value in call)]
    first_post = calls.index(creates[0]) if creates else len(calls)
    searches = [
        index for index, call in enumerate(calls)
        if index > first_post and "GET" in call
        and any(value.startswith(f"repos/{REPOSITORY}/pulls?") for value in call)
    ]
    patches = [
        index for index, call in enumerate(calls)
        if "PATCH" in call and f"repos/{REPOSITORY}/pulls/{pr['number']}" in call
    ]
    gate.record("coordinator-resume-single-pr",
                code == status_code == 0 and resumed.get("run_id") == gate.run_id
                and gate.same_identity() and len(creates) == len(matching_posts) == 1
                and bool(searches) and (not patches or min(searches) < min(patches))
                and bound_pr.get("number") == pr["number"]
                and bound_pr.get("head_revision") == pr["head"]["sha"]
                and _read(gate.bin / "gh-pr.json") == pr and len(gate.launches()) == 1,
                {"resume_exit": code, "resume": resumed, "status_exit": status_code, "status": status},
                {"pr_posts": creates, "operation_id": operation, "persisted_pr": pr,
                 "reconciliation_search_indexes": searches, "patch_indexes": patches})
    # A final retry also proves completion is idempotent, rather than only surviving one retry.
    retry_code, retried = gate.factory("resume", gate.run_id)
    endpoints = [next((value for value in call if value.startswith("repos/")), "") for call in calls]
    gate.record("verified-open-pr-outcome",
                retry_code == 0 and retried.get("workflow_complete") is True
                and retried.get("terminal_decision") == "merge_ready"
                and bool(retried.get("outcome_observation"))
                and status.get("workflow_complete") is True
                and status.get("terminal_evidence", {}).get("verify_status") == "passed"
                and pr["state"] == "open" and not pr["merged"]
                and any("/check-runs?" in path for path in endpoints)
                and any("/reviews?" in path for path in endpoints)
                and len([call for call in gate.gh_calls() if "POST" in call
                         and f"repos/{REPOSITORY}/pulls" in call]) == 1,
                {"exit": retry_code, "json": retried}, {"persisted_pr": pr, "endpoints": endpoints})


def step_control(gate: ManualPathGate) -> None:
    (gate.bin / "release-worker").unlink()
    bound_code, bound = gate.factory("run", SECOND_ISSUE, "--once")
    gate.second_run_id = bound.get("run_id", "")
    release_arguments = (
        "workspace", "release", "--database", str(gate.database), "--owner", "factory",
        "--repository-id", REPOSITORY, "--run-id", gate.run_id, "--json",
    )
    release_command = shlex.join(("enginery", *release_arguments))
    retained = bound.get("blocked_workspace") or bound.get("workspace") or {}
    gate.record(
        "retained-workspace-hold-repair",
        bound_code == 5 and bool(gate.second_run_id) and gate.second_run_id != gate.run_id
        and retained.get("status") == "retained" and retained.get("run_id") == gate.run_id
        and any(
            hold.get("prerequisite") == "retained workspace"
            and hold.get("resolve") == release_command for hold in bound.get("holds", [])
        ) and len(gate.launches()) == 1,
        {"exit": bound_code, "json": bound},
        {"release_command": release_command, "launches": gate.launches()},
    )
    released = gate.cli(*release_arguments)
    if released.returncode != 0:
        raise RuntimeError(f"retained workspace release refused: {released.stderr}")
    code, second = gate.factory("run", SECOND_ISSUE)
    gate.second_run_id = second.get("run_id", "")
    _wait(lambda: len(gate.launches()) == 2, "second sleeping implementation")
    second_code, second_status = gate.factory("status", gate.second_run_id)
    second_worker = second_status.get("worker") or {}
    if second_worker.get("process_group_id"):
        gate.worker_groups.add(second_worker["process_group_id"])
    second_request = _read(gate.state / "runs" / gate.second_run_id / "request.json")
    second_workspace = Path(second_request["workspace_path"])
    base_revision = second_request["run"]["data"]["base_revision"]
    _wait(
        lambda: gate.git(
            second_workspace, "rev-list", "--count", f"{base_revision}..HEAD"
        ) == "1",
        "second workspace's single commit",
    )
    pause_code, paused = gate.factory("pause", "--reason", "manual gate")
    hold_code, held = gate.factory("run", SECOND_ISSUE)
    repair = f"enginery factory pause --lift --project {gate.project}"
    gate.record(
        "pause-second-issue-hold",
        code == second_code == pause_code == 0 and bool(gate.second_run_id)
        and gate.second_run_id != gate.run_id and hold_code == 5
        and any(item.get("resolve") == repair for item in held.get("holds", []))
        and "project ledger" in str(paused.get("scope"))
        and len(gate.launches()) == 2 and second_worker.get("liveness") == "alive"
        and _alive(second_worker["pid"]),
        {"second_exit": code, "second": second, "second_status": second_status,
         "pause_exit": pause_code, "pause": paused, "hold_exit": hold_code, "hold": held},
        {"launches": gate.launches(), "repair": repair, "second_workspace_commits": 1,
         "first_workspace_release": json.loads(released.stdout)},
    )
    wait_code, waited = gate.factory("drain", "--wait-seconds", "5")
    drain_code, drained = gate.factory("drain", "--stop")
    ledger = LedgerService.open(gate.database)
    try:
        live = [worker.to_state() for worker in live_workers(ledger)]
        reservations = [
            dict(record.state) for record in ledger.list_process_manager_states(
                process_manager_name="workspace-reservations")
        ]
        nodes = [
            dict(record.state)
            for record in ledger.list_projections(aggregate_type="runtime_node")
        ]
        leases = {
            node["node_id"]: ledger.read_lease(
                run_id=gate.second_run_id, node_id=node["node_id"])
            for node in second_status.get("nodes", [])
        }
    finally:
        ledger.close()
    active = [
        record for record in reservations
        if record.get("status") in {"reserved", "materialized", "retained"}
    ]
    implementation_lease = leases.get("implement")
    lease_released = (
        implementation_lease is not None
        and implementation_lease.expires_at is not None
        and datetime.fromisoformat(implementation_lease.expires_at) <= datetime.now(UTC)
    )
    gate.record(
        "drain-releases-resources",
        wait_code != 0 and waited.get("paused") is True
        and waited.get("drained") is False and bool(waited.get("remaining"))
        and waited.get("released") == "by resume/reconcile"
        and drain_code == 0 and drained.get("paused") is True
        and drained.get("released") == "now" and drained.get("drained") is True
        and drained.get("remaining") == [] and bool(drained.get("stopped"))
        and not live and not active and lease_released
        and not any(node.get("status") == "running" for node in nodes),
        {"wait_exit": wait_code, "wait": waited, "stop_exit": drain_code, "stop": drained},
        {"live_workers": live, "reservations": reservations, "nodes": nodes,
         "leases": {key: str(value) for key, value in leases.items()},
         "isolated_containers": 0, "runner_mode": "trusted-local"},
    )
    lift_code, lifted = gate.factory("pause", "--lift")
    list_code, listed = gate.factory("status")
    runs = listed.get("runs", [])
    gate.record(
        "pause-lift-and-list-both",
        lift_code == list_code == 0 and lifted.get("paused") is False
        and listed.get("paused") is False
        and {item.get("run_id") for item in runs} == {gate.run_id, gate.second_run_id},
        {"lift_exit": lift_code, "lift": lifted, "list_exit": list_code, "list": listed},
        {"intake": gate.index(), "run_ids": [gate.run_id, gate.second_run_id]},
    )


def step_boundaries(gate: ManualPathGate, globals_before: dict[str, str],
                    home_before: dict[str, str]) -> None:
    globals_after = gate.global_snapshot()
    outside = [path for path in _changed(home_before, gate.home_snapshot())
               if not any(path == prefix or path.startswith(prefix + "/") for prefix in (
                   "home/.config/enginery", "home/.local/state/enginery"))]
    global_changes = _changed(globals_before, globals_after)
    gate.record("global-locations-byte-identical", not global_changes and not outside,
                {"global_changes": global_changes, "outside_private": outside},
                {"before": globals_before, "after": globals_after})
    calls = gate.gh_calls()
    reads = [call for call in calls if "--method" in call and call[call.index("--method") + 1] == "GET"]
    mutations = [call for call in calls if "--method" in call and call[call.index("--method") + 1] != "GET"]
    posts = [call for call in mutations if "POST" in call and f"repos/{REPOSITORY}/pulls" in call]
    updates = [
        call for call in mutations if "PATCH" in call
        and f"repos/{REPOSITORY}/pulls/17" in call
    ]
    lifecycle = [
        call for call in mutations if "POST" in call
        and f"repos/{REPOSITORY}/issues/1/comments" in call
        and any(value.startswith("body=<!-- enginery:lifecycle:") for value in call)
    ]
    unexpected = [
        call for call in mutations
        if call not in posts and call not in updates and call not in lifecycle
    ]
    launches = gate.launches()
    gate.effects.update(
        github_reads=len(reads), github_mutations=len(mutations), pull_request_creations=len(posts),
        pull_request_updates=len(updates),
        issue_comments=sum(any("/comments" in value for value in call) for call in mutations),
        issue_lifecycle_comments=len(lifecycle),
        issue_label_mutations=sum(any("/labels" in value for value in call) for call in mutations),
        merges=sum(any("/merge" in value for value in call) for call in mutations),
        releases=sum(any("/releases" in value for value in call) for call in mutations),
        deployments=sum(any("/deployments" in value for value in call) for call in mutations),
        hosted_inference_calls=0, hosted_sessions=0, container_engine_calls=0,
        harness_launches_recorded=len(launches), global_location_changes=len(global_changes),
    )
    gate.record("local-only-effect-counts",
                shutil.which("gh", path=gate.environment()["PATH"]) == str(gate.bin / "gh")
                and shutil.which("omp", path=gate.environment()["PATH"]) == str(gate.bin / "omp")
                and len(launches) == 2 and len(posts) == 1 and not unexpected
                and len(lifecycle) == 1
                and all(Path(launch["workspace"]).is_relative_to(gate.state) for launch in launches),
                gate.effects, {"gh_calls": calls, "launches": launches,
                               "stand_in_sha256": {name: hashlib.sha256((gate.bin / name).read_bytes()).hexdigest()
                                                   for name in ("gh", "omp")}})
    gate.raw_write("commands.json", gate.completed)
    gate.raw_write("gh-calls.json", calls)
    gate.raw_write("harness-launches.json", launches)
    pr = gate.bin / "gh-pr.json"
    if pr.exists():
        gate.raw_write("pull-request.json", _read(pr))
    if gate.run_id:
        gate.raw_write("request.json", _read(gate.request_path()))
        ledger = LedgerService.open(gate.database)
        try:
            gate.raw_write("runtime-nodes.json", [
                {"id": row.aggregate_id, "state": dict(row.state)}
                for row in ledger.list_projections(aggregate_type="runtime_node")
            ])
            gate.raw_write("supervisors.json", [
                {"key": row.state_key, "state": dict(row.state)}
                for row in ledger.list_process_manager_states(
                    process_manager_name="worker-supervisor"
                )
            ])
        finally:
            ledger.close()
    gate.evidence.details["commands_raw"] = f"{gate.options.raw_dir.name}/commands.json"


def run(gate: ManualPathGate) -> None:
    globals_before = gate.global_snapshot()
    home_before = gate.home_snapshot()
    try:
        for step in (step_prepare, step_collect, step_client_recovery, step_inspection_review,
                     step_pr_recovery, step_control):
            step(gate)
    except Exception as error:
        # Unreachable dependants must be not-run failures, never apparent successes.
        gate.evidence.details["scenario_error"] = f"{type(error).__name__}: {error}"
        for key in REQUIRED:
            if key not in gate.recorded and key not in {
                "global-locations-byte-identical", "local-only-effect-counts",
            }:
                gate.evidence.fail_assertion(key, f"{type(error).__name__}: {error}",
                                             commands=gate.commands)
                gate.recorded.add(key)
    try:
        step_boundaries(gate, globals_before, home_before)
    except Exception as error:
        for key in ("global-locations-byte-identical", "local-only-effect-counts"):
            if key not in gate.recorded:
                gate.evidence.fail_assertion(key, f"{type(error).__name__}: {error}",
                                             commands=gate.commands)


def finalize(gate: ManualPathGate) -> None:
    """All observations and raw references are captured before disposable-state cleanup."""
