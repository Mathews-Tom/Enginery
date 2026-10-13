"""The host-side branch publisher, against real git and a real local remote.

Every test pushes to a bare repository on disk. A ``pre-receive`` hook in that remote records each
accepted ref update, so "pushed nothing" and "pushed exactly once" are observed on the remote, not
inferred from the adapter. A recording wrapper around ``git`` captures the exact command lines and
environments the adapter used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import enginery.adapters.git_publication as git_publication
from enginery.adapters.git_publication import (
    PUBLICATION_ENVIRONMENT_KEYS,
    GitBranchPublisher,
    publication_environment,
)
from enginery.application.adapter_types import AdapterAvailability
from enginery.application.branch_publication import BranchPublicationRequest, PublishedBranch
from enginery.domain.errors import (
    AmbiguousExternalSideEffectError,
    ExternalConflictError,
)
from enginery.domain.ids import OperationId
from enginery.domain.node_attempt import ReconciliationResult

BRANCH = "enginery/run-1"
_QUIET = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_QUIET},
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def commit(repository: Path, name: str, content: str, message: str) -> str:
    (repository / name).write_text(content, encoding="utf-8")
    git("add", name, cwd=repository)
    git("commit", "-q", "-m", message, cwd=repository)
    return git("rev-parse", "HEAD", cwd=repository)


def remote_refs(remote: Path) -> dict[str, str]:
    output = git("for-each-ref", "--format=%(refname) %(objectname)", cwd=remote)
    return {line.split()[0]: line.split()[1] for line in output.splitlines()}


@dataclass
class Rig:
    root: Path
    host: Path
    remote: Path
    source: Path
    decoy: Path
    wrapper: Path
    log: Path
    pushes: Path
    home: Path

    def publisher(self, *, wrapped: bool = False) -> GitBranchPublisher:
        return GitBranchPublisher(
            repository_path=self.host,
            source_path=self.source,
            executable=str(self.wrapper) if wrapped else "git",
        )

    def accepted_pushes(self) -> list[str]:
        return self.pushes.read_text(encoding="utf-8").splitlines() if self.pushes.exists() else []

    def invocations(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def pushes_issued(self) -> list[list[str]]:
        return [
            [str(argument) for argument in entry["argv"]]  # type: ignore[attr-defined]
            for entry in self.invocations()
            if "push" in entry["argv"]  # type: ignore[operator]
        ]

    def request(
        self,
        head: str,
        *,
        expected: str | None = None,
        operation: str = "publish:run-1:0",
        branch: str = BRANCH,
    ) -> BranchPublicationRequest:
        return BranchPublicationRequest(
            repository_id="org/repo",
            branch=branch,
            head_revision=head,
            expected_remote_revision=expected,
            operation_id=OperationId(operation),
        )

    def rival_push(self, name: str, content: str) -> str:
        """Push an unrelated commit to the same remote branch from another clone."""
        clone = self.root / f"rival-{name}"
        git("clone", "-q", str(self.remote), str(clone), cwd=self.root)
        git("config", "user.email", "rival@example.test", cwd=clone)
        git("config", "user.name", "Rival", cwd=clone)
        revision = commit(clone, name, content, "rival")
        git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=clone)
        return revision


_WRAPPER = """#!{python}
import json, os, subprocess, sys
LOG = {log!r}
BEFORE_PUSH = {before_push!r}
SLOW_PUSH = {slow_push!r}
with open(LOG, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ),
        "terminal_prompt": os.environ.get("GIT_TERMINAL_PROMPT")}}) + "\\n")
if "push" in sys.argv[1:]:
    if os.path.exists(BEFORE_PUSH):
        command = json.load(open(BEFORE_PUSH))
        os.unlink(BEFORE_PUSH)
        subprocess.run(command, check=True, capture_output=True)
    if os.path.exists(SLOW_PUSH):
        import time
        time.sleep(30)
os.execvp("git", ["git", *sys.argv[1:]])
"""


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LANG", "C.UTF-8")
    root = tmp_path / "rig"
    root.mkdir()
    remote = root / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=root)
    pushes = root / "accepted-pushes.log"
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        f'#!/bin/sh\nwhile read old new ref; do echo "$ref" >> {pushes}; done\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)

    host = root / "host"
    host.mkdir()
    git("init", "-q", "-b", "main", cwd=host)
    git("config", "user.email", "host@example.test", cwd=host)
    git("config", "user.name", "Host", cwd=host)
    commit(host, "README", "baseline\n", "baseline")
    git("remote", "add", "origin", str(remote), cwd=host)
    git("push", "-q", "origin", "main", cwd=host)
    pushes.unlink()  # only what the publisher pushes is counted

    decoy = root / "decoy.git"
    git("init", "-q", "--bare", "-b", "main", str(decoy), cwd=root)
    source = root / "source"
    git("clone", "-q", str(remote), str(source), cwd=root)
    git("config", "user.email", "worker@example.test", cwd=source)
    git("config", "user.name", "Worker", cwd=source)
    git("remote", "set-url", "origin", str(decoy), cwd=source)
    git("checkout", "-q", "-b", BRANCH, cwd=source)

    log = root / "git-invocations.jsonl"
    wrapper = root / "git-wrapper"
    wrapper.write_text(
        _WRAPPER.format(
            python=sys.executable,
            log=str(log),
            before_push=str(root / "before-push.json"),
            slow_push=str(root / "slow-push"),
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return Rig(
        root=root,
        host=host,
        remote=remote,
        source=source,
        decoy=decoy,
        wrapper=wrapper,
        log=log,
        pushes=pushes,
        home=home,
    )


def _implement(rig: Rig, name: str = "feature.txt", content: str = "implemented\n") -> str:
    return commit(rig.source, name, content, f"implement {name}")


# --- publication ---------------------------------------------------------------------------


def test_the_verified_revision_is_pushed_to_an_absent_branch_and_reported(rig: Rig) -> None:
    head = _implement(rig)

    published = rig.publisher().publish(rig.request(head))

    assert published == PublishedBranch(branch=BRANCH, revision=head)
    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == head
    assert rig.accepted_pushes() == [f"refs/heads/{BRANCH}"]


def test_publishing_the_same_revision_again_pushes_nothing(rig: Rig) -> None:
    head = _implement(rig)
    publisher = rig.publisher(wrapped=True)
    publisher.publish(rig.request(head))
    pushes_after_first = len(rig.pushes_issued())

    again = publisher.publish(rig.request(head, operation="publish:run-1:retry"))

    assert again == PublishedBranch(branch=BRANCH, revision=head)
    assert len(rig.pushes_issued()) == pushes_after_first == 1, "no second push was issued"
    assert rig.accepted_pushes() == [f"refs/heads/{BRANCH}"], "the remote accepted exactly one"


def test_a_repair_publication_advances_the_branch_from_exactly_the_expected_head(rig: Rig) -> None:
    first = _implement(rig, "one.txt", "one\n")
    rig.publisher().publish(rig.request(first))
    second = _implement(rig, "two.txt", "two\n")

    published = rig.publisher().publish(
        rig.request(second, expected=first, operation="publish:run-1:1")
    )

    assert published.revision == second
    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == second
    assert rig.accepted_pushes() == [f"refs/heads/{BRANCH}"] * 2


def test_a_rewritten_branch_is_published_when_the_remote_is_at_the_expected_head(
    rig: Rig,
) -> None:
    first = _implement(rig, "one.txt", "one\n")
    rig.publisher().publish(rig.request(first))
    git("reset", "-q", "--hard", "HEAD~1", cwd=rig.source)
    rewritten = _implement(rig, "other.txt", "rewritten\n")

    published = rig.publisher().publish(
        rig.request(rewritten, expected=first, operation="publish:run-1:1")
    )

    assert published.revision == rewritten
    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == rewritten


# --- never overwrite --------------------------------------------------------------------------


def test_a_remote_branch_that_exists_when_none_was_expected_is_never_overwritten(rig: Rig) -> None:
    head = _implement(rig)
    rival = rig.rival_push("rival.txt", "someone else\n")
    pushes_before = rig.accepted_pushes()

    with pytest.raises(ExternalConflictError, match="not overwritten"):
        rig.publisher().publish(rig.request(head))

    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == rival
    assert rig.accepted_pushes() == pushes_before


def test_a_remote_branch_that_moved_off_the_expected_head_is_never_overwritten(rig: Rig) -> None:
    first = _implement(rig, "one.txt", "one\n")
    rig.publisher().publish(rig.request(first))
    second = _implement(rig, "two.txt", "two\n")
    rival = rig.rival_push("rival.txt", "moved\n")
    pushes_before = rig.accepted_pushes()

    with pytest.raises(ExternalConflictError) as caught:
        rig.publisher().publish(rig.request(second, expected=first, operation="publish:run-1:1"))

    assert caught.value.details["remote_revision"] == rival
    assert caught.value.details["expected_remote_revision"] == first
    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == rival
    assert rig.accepted_pushes() == pushes_before


def test_an_expected_head_that_is_gone_from_the_remote_is_a_conflict_not_a_fresh_push(
    rig: Rig,
) -> None:
    first = _implement(rig, "one.txt", "one\n")
    second = _implement(rig, "two.txt", "two\n")

    with pytest.raises(ExternalConflictError, match="not overwritten"):
        rig.publisher().publish(rig.request(second, expected=first))

    assert f"refs/heads/{BRANCH}" not in remote_refs(rig.remote)
    assert rig.accepted_pushes() == []


def test_a_branch_that_moves_between_the_check_and_the_push_is_still_not_overwritten(
    rig: Rig,
) -> None:
    head = _implement(rig)
    rival_clone = rig.root / "rival-clone"
    git("clone", "-q", str(rig.remote), str(rival_clone), cwd=rig.root)
    git("config", "user.email", "rival@example.test", cwd=rival_clone)
    git("config", "user.name", "Rival", cwd=rival_clone)
    rival = commit(rival_clone, "race.txt", "won the race\n", "race")
    # The wrapper runs this just before the adapter's push executes, after its own check passed.
    (rig.root / "before-push.json").write_text(
        json.dumps(
            [
                "git",
                "-C",
                str(rival_clone),
                "push",
                "-q",
                "origin",
                f"HEAD:refs/heads/{BRANCH}",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExternalConflictError, match="refused the branch publication"):
        rig.publisher(wrapped=True).publish(rig.request(head))

    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == rival
    assert rig.accepted_pushes() == [f"refs/heads/{BRANCH}"], "only the rival's push landed"


def test_only_the_one_branch_is_pushed_never_other_refs_or_tags(rig: Rig) -> None:
    (rig.home / ".gitconfig").write_text("[push]\n\tfollowTags = true\n", encoding="utf-8")
    head = _implement(rig)
    git("tag", "-a", "-m", "release", "v1", head, cwd=rig.source)
    git("branch", "unrelated", cwd=rig.source)
    git("update-ref", "refs/notes/leak", head, cwd=rig.source)

    rig.publisher().publish(rig.request(head))

    assert set(remote_refs(rig.remote)) == {"refs/heads/main", f"refs/heads/{BRANCH}"}
    assert rig.accepted_pushes() == [f"refs/heads/{BRANCH}"]


# --- the command line and environment ----------------------------------------------------------


@pytest.mark.parametrize("with_expected", [False, True])
def test_the_push_is_one_hardened_lease_constrained_refspec_to_the_host_origin(
    rig: Rig, with_expected: bool
) -> None:
    first = _implement(rig, "one.txt", "one\n")
    expected: str | None = None
    head = first
    if with_expected:
        rig.publisher().publish(rig.request(first))
        head = _implement(rig, "two.txt", "two\n")
        expected = first

    rig.publisher(wrapped=True).publish(rig.request(head, expected=expected, operation="publish:x"))

    (push,) = rig.pushes_issued()
    assert "-c" in push and "core.hooksPath=/dev/null" in push
    assert "--no-verify" in push
    assert f"--force-with-lease=refs/heads/{BRANCH}:{expected or ''}" in push
    assert "push.followTags=false" in push
    assert "protocol.ext.allow=never" in push
    assert push[-3:] == ["--", str(rig.remote), f"{head}:refs/heads/{BRANCH}"]
    forbidden = {"--force", "-f", "--all", "--mirror", "--tags", "--delete", "--prune", "-d"}
    assert forbidden.isdisjoint(push)
    assert [argument for argument in push if ":" in argument and "refs/heads" in argument] == [
        f"--force-with-lease=refs/heads/{BRANCH}:{expected or ''}",
        f"{head}:refs/heads/{BRANCH}",
    ]
    assert str(rig.decoy) not in " ".join(push), "the source's own origin is never used"


def test_a_hook_in_the_verified_repository_never_runs(rig: Rig) -> None:
    marker = rig.root / "hook-ran"
    hooks = rig.root / "evil-hooks"
    hooks.mkdir()
    for name in ("pre-push", "reference-transaction", "post-checkout"):
        script = hooks / name
        script.write_text(f"#!/bin/sh\necho {name} >> {marker}\n", encoding="utf-8")
        script.chmod(0o755)
    git("config", "core.hooksPath", str(hooks), cwd=rig.source)
    default_hook = rig.source / ".git" / "hooks" / "pre-push"
    default_hook.write_text(f"#!/bin/sh\necho default >> {marker}\n", encoding="utf-8")
    default_hook.chmod(0o755)
    head = _implement(rig)
    marker.unlink(missing_ok=True)  # committing in the source may run its own hooks

    rig.publisher().publish(rig.request(head))

    assert not marker.exists()
    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == head


def test_git_runs_with_the_publication_environment_and_no_other_credential(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _implement(rig)
    for name, value in {
        "GH_TOKEN": "gh-token-value",
        "GITHUB_TOKEN": "github-token-value",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "UV_PUBLISH_TOKEN": "pypi-secret",
        "NPM_TOKEN": "npm-secret",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "ENGINERY_AMBIENT_MARKER": "marker",
        "GIT_DIR": str(rig.decoy),
        "GIT_WORK_TREE": str(rig.decoy),
        "GIT_SSH_COMMAND": "touch /tmp/never",
        "GIT_CONFIG_COUNT": "1",
    }.items():
        monkeypatch.setenv(name, value)

    rig.publisher(wrapped=True).publish(rig.request(head))

    invocations = rig.invocations()
    assert invocations, "the wrapper observed every git invocation"
    allowed = {*PUBLICATION_ENVIRONMENT_KEYS, "GIT_TERMINAL_PROMPT"}
    tolerated_platform_additions = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    for entry in invocations:
        names = set(entry["env"])  # type: ignore[call-overload]
        assert names <= allowed | tolerated_platform_additions, names - allowed
        assert {"AWS_SECRET_ACCESS_KEY", "UV_PUBLISH_TOKEN", "NPM_TOKEN"}.isdisjoint(names)
        assert {
            "ANTHROPIC_API_KEY",
            "ENGINERY_AMBIENT_MARKER",
            "GIT_DIR",
            "GIT_SSH_COMMAND",
        }.isdisjoint(names)
        assert entry["terminal_prompt"] == "0"
        # The broker-side authority a worker never holds is exactly what the push carries.
        assert {"GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"} <= names


def test_the_publication_environment_is_exactly_the_allowlist_plus_no_prompt() -> None:
    source = {
        "PATH": "/bin",
        "HOME": "/home/x",
        "GH_TOKEN": "a",
        "OTHER_SECRET": "b",
        "GIT_ASKPASS": "/bin/true",
    }

    assert publication_environment(source) == {
        "PATH": "/bin",
        "HOME": "/home/x",
        "GH_TOKEN": "a",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert PUBLICATION_ENVIRONMENT_KEYS == (
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "SSH_AUTH_SOCK",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    )


def test_the_remote_url_is_read_from_the_host_repository_not_the_verified_one(rig: Rig) -> None:
    head = _implement(rig)

    rig.publisher().publish(rig.request(head))

    assert remote_refs(rig.decoy) == {}, "the source repository's own origin is never pushed to"
    assert f"refs/heads/{BRANCH}" in remote_refs(rig.remote)


def test_a_relative_origin_is_resolved_against_the_host_repository(rig: Rig) -> None:
    git("remote", "set-url", "origin", "../remote.git", cwd=rig.host)
    head = _implement(rig)

    rig.publisher().publish(rig.request(head))

    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == head


def test_a_url_that_embeds_a_credential_never_appears_in_an_error(rig: Rig) -> None:
    git(
        "remote",
        "set-url",
        "origin",
        "https://operator:hunter2hunter2hunter2@127.0.0.1:9/org/repo.git",
        cwd=rig.host,
    )
    head = _implement(rig)

    with pytest.raises(ExternalConflictError) as caught:
        rig.publisher().publish(rig.request(head))

    rendered = f"{caught.value} {dict(caught.value.details)}"
    assert "hunter2hunter2hunter2" not in rendered
    assert "operator" not in rendered


# --- failures that must be loud ------------------------------------------------------------------


def test_a_revision_the_verified_repository_lacks_is_refused_before_any_remote_contact(
    rig: Rig,
) -> None:
    git("remote", "set-url", "origin", "https://127.0.0.1:9/never.git", cwd=rig.host)

    with pytest.raises(ExternalConflictError, match="does not contain the revision"):
        rig.publisher().publish(rig.request("c" * 40))


def test_a_missing_verified_repository_is_refused(rig: Rig) -> None:
    head = _implement(rig)
    publisher = GitBranchPublisher(repository_path=rig.host, source_path=rig.root / "gone")

    with pytest.raises(ExternalConflictError, match="does not exist"):
        publisher.publish(rig.request(head))


def test_a_host_repository_without_an_origin_cannot_publish(rig: Rig) -> None:
    git("remote", "remove", "origin", cwd=rig.host)
    head = _implement(rig)

    with pytest.raises(ExternalConflictError, match="no origin remote"):
        rig.publisher().publish(rig.request(head))


def test_an_origin_url_that_would_be_an_option_is_refused(rig: Rig) -> None:
    git("config", "remote.origin.url", "--upload-pack=touch /tmp/never", cwd=rig.host)
    head = _implement(rig)

    with pytest.raises(ExternalConflictError, match="not a valid url"):
        rig.publisher().publish(rig.request(head))


def test_a_push_that_times_out_is_an_ambiguous_effect_to_reconcile_not_a_failure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_publication, "_GIT_TIMEOUT_SECONDS", 1.0)
    (rig.root / "slow-push").write_text("", encoding="utf-8")
    head = _implement(rig)

    with pytest.raises(AmbiguousExternalSideEffectError, match="reconcile before retrying"):
        rig.publisher(wrapped=True).publish(rig.request(head))

    assert f"refs/heads/{BRANCH}" not in remote_refs(rig.remote)


# --- reconciliation ------------------------------------------------------------------------------


def test_reconcile_finds_a_matching_publication_and_nothing_for_an_unseen_operation(
    rig: Rig,
) -> None:
    head = _implement(rig)
    publisher = rig.publisher()
    request = rig.request(head)

    assert publisher.reconcile(operation_id=request.operation_id) is ReconciliationResult.NOT_FOUND
    publisher.publish(request)

    assert (
        publisher.reconcile(operation_id=request.operation_id)
        is ReconciliationResult.FOUND_MATCHING
    )
    assert (
        publisher.reconcile(operation_id=OperationId("publish:never-attempted"))
        is ReconciliationResult.NOT_FOUND
    )


def test_reconcile_is_not_found_while_the_branch_is_absent_or_still_at_the_expected_head(
    rig: Rig,
) -> None:
    first = _implement(rig, "one.txt", "one\n")
    publisher = rig.publisher()
    # An attempt recorded and then refused before any push: the revision is not in the source.
    unpublishable = rig.request("c" * 40, operation="publish:absent")
    with pytest.raises(ExternalConflictError):
        publisher.publish(unpublishable)
    assert (
        publisher.reconcile(operation_id=unpublishable.operation_id)
        is ReconciliationResult.NOT_FOUND
    )

    publisher.publish(rig.request(first))
    repair = rig.request("d" * 40, expected=first, operation="publish:at-expected")
    with pytest.raises(ExternalConflictError):
        publisher.publish(repair)
    assert publisher.reconcile(operation_id=repair.operation_id) is ReconciliationResult.NOT_FOUND


def test_reconcile_never_resolves_a_foreign_head_it_raises(rig: Rig) -> None:
    head = _implement(rig)
    publisher = rig.publisher()
    request = rig.request(head)
    rival = rig.rival_push("rival.txt", "foreign\n")
    with pytest.raises(ExternalConflictError):
        publisher.publish(request)

    with pytest.raises(ExternalConflictError, match="neither pushed nor expected"):
        publisher.reconcile(operation_id=request.operation_id)

    assert remote_refs(rig.remote)[f"refs/heads/{BRANCH}"] == rival


# --- probe ----------------------------------------------------------------------------------------


def test_probe_reports_an_available_publisher_with_a_fingerprint(rig: Rig) -> None:
    status = rig.publisher().probe()

    assert status.availability is AdapterAvailability.AVAILABLE
    assert status.fingerprint is not None
    assert {capability.name for capability in status.fingerprint.capabilities} == {
        "branch_publication",
        "branch_publication_reconciliation",
        "expected_head_lease",
    }
    assert str(rig.remote) not in status.detail


def test_probe_reports_a_host_repository_without_an_origin_as_misconfigured(rig: Rig) -> None:
    git("remote", "remove", "origin", cwd=rig.host)

    status = rig.publisher().probe()

    assert status.availability is AdapterAvailability.MISCONFIGURED
    assert status.fingerprint is None


def test_probe_reports_a_missing_git_as_unavailable(rig: Rig) -> None:
    publisher = GitBranchPublisher(
        repository_path=rig.host, source_path=rig.source, executable="git-that-does-not-exist"
    )

    status = publisher.probe()

    assert status.availability is AdapterAvailability.UNAVAILABLE
    assert status.fingerprint is None
