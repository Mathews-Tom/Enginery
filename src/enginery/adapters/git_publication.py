"""Host-side publication of a verified branch revision with plain ``git push``.

This is the operator-side authority a worker never holds in isolation. The worker commits locally;
the host verifies the branch in a repository the worker cannot write to (the host-owned intake
repository of a task-local workspace) and only then does this adapter push exactly one revision to
exactly one branch of the remote the *host* repository's ``origin`` names. It never overwrites a
remote branch it did not expect, never pushes anything else, and is idempotent: a remote already at
the revision is success without a push.

A trusted-local run has no such separation: its verified repository is the worker's own workspace
and its ``origin`` is read from the repository configuration the worker could write. Every git
command here therefore overrides the command-line-reachable settings a worker could plant there
(hooks, fsmonitor, credential helpers, ssh command, askpass) with the operator's own user and
system configuration, but a worker-writable repository remains a weaker publication source than
the host-owned intake: per-url credential helpers and url rewrites in repository configuration
cannot be neutralised from the command line.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from enginery.application.adapter_types import (
    ADAPTER_API_VERSION,
    AdapterAvailability,
    AdapterCapability,
    AdapterFingerprint,
    AdapterStatus,
    ProviderKind,
)
from enginery.application.branch_publication import (
    BranchPublicationRequest,
    PublishedBranch,
)
from enginery.domain.errors import (
    AmbiguousExternalSideEffectError,
    ExternalConflictError,
    InvalidInputError,
    MissingPrerequisiteError,
)
from enginery.domain.node_attempt import ReconciliationResult
from enginery.ledger.redaction import redact_credential_shaped_text

PUBLICATION_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "SSH_AUTH_SOCK",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)
"""The only host variables a publication push inherits.

This is the broker-side authority workers never receive: source-host credentials live here, in
the process that pushes, and in no worker environment.
"""

_GIT_TIMEOUT_SECONDS = 300.0
_OPERATOR_CONFIG_DIRECTORY = Path("/")
"""Where the operator's own git configuration is read from: a directory that is no repository.

Git evaluates ``includeIf "gitdir:..."`` against the repository it runs in, so reading from the
process's start directory would make the result depend on where the host happened to be started.
"""
_STDERR_DETAIL_CHARS = 2000
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def publication_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The sanitized environment of a publication git invocation."""
    environ = os.environ if source is None else source
    environment = {key: environ[key] for key in PUBLICATION_ENVIRONMENT_KEYS if key in environ}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


@dataclass(slots=True)
class GitBranchPublisher:
    """Push one verified revision to ``origin`` of the host repository, expected-head constrained.

    ``repository_path`` is the host repository: only its ``origin`` url is read from it.
    ``source_path`` is the verified repository the revision is pushed from. The remote url is
    never taken from the source repository, which a worker may have influenced.
    """

    repository_path: Path
    source_path: Path
    executable: str = "git"
    _hardening_arguments: tuple[str, ...] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.repository_path.is_absolute() or not self.source_path.is_absolute():
            raise InvalidInputError("branch publication paths must be absolute")
        if not self.executable.strip():
            raise InvalidInputError("branch publication git executable must be non-blank")

    def probe(self) -> AdapterStatus:
        try:
            version = self._run(["--version"], cwd=None)
        except (OSError, subprocess.SubprocessError, MissingPrerequisiteError):
            return _status(
                AdapterAvailability.UNAVAILABLE, None, "git is not available to publish branches"
            )
        if version.returncode != 0 or not version.stdout.strip():
            return _status(
                AdapterAvailability.UNAVAILABLE, None, "git is not available to publish branches"
            )
        try:
            self._origin_url()  # presence is what is probed; the url is never reported
        except ExternalConflictError:
            return _status(
                AdapterAvailability.MISCONFIGURED,
                None,
                "the host repository has no readable origin remote to publish to",
            )
        return _status(
            AdapterAvailability.AVAILABLE,
            version.stdout.strip(),
            "git branch publication is available",
        )

    def publish(self, request: BranchPublicationRequest) -> PublishedBranch:
        """Push ``request.head_revision`` to ``request.branch`` unless the remote already has it.

        The remote branch must be absent (``expected_remote_revision`` None) or at exactly the
        expected revision; any other state is an external conflict and nothing is pushed. The
        push itself is lease-constrained to the same expectation, so a branch that moves between
        the check and the push is still never overwritten. The branch is re-read afterwards: a
        push that did not leave it at the verified revision is an error, not a success.
        """
        self._require_commit(request.head_revision)
        url = self._origin_url()
        remote = self._remote_revision(url, request.branch)
        if remote == request.head_revision:
            return PublishedBranch(branch=request.branch, revision=request.head_revision)
        if remote != request.expected_remote_revision:
            raise ExternalConflictError(
                "the remote branch is not at the revision this publication expects; "
                "it is not overwritten",
                details={
                    "branch": request.branch,
                    "expected_remote_revision": request.expected_remote_revision,
                    "remote_revision": remote,
                },
            )
        expected = request.expected_remote_revision or ""
        lease = f"--force-with-lease=refs/heads/{request.branch}:{expected}"
        try:
            completed = self._run(
                [
                    "-c",
                    "push.followTags=false",
                    "push",
                    "--no-verify",
                    "--porcelain",
                    lease,
                    "--",
                    url,
                    f"{request.head_revision}:refs/heads/{request.branch}",
                ],
                cwd=self.source_path,
            )
        except subprocess.TimeoutExpired as error:
            raise AmbiguousExternalSideEffectError(
                "branch publication timed out and its effect is unknown; retry open_pr: "
                "publication is idempotent and constrained to the expected remote head",
                details={"branch": request.branch, "operation_id": str(request.operation_id)},
            ) from error
        if completed.returncode != 0:
            raise ExternalConflictError(
                "git refused the branch publication",
                details={
                    "branch": request.branch,
                    "stderr": _scrub(completed.stderr, url),
                },
            )
        observed = self._remote_revision(url, request.branch)
        if observed != request.head_revision:
            raise ExternalConflictError(
                "the remote branch is not at the verified revision after publication",
                details={
                    "branch": request.branch,
                    "head_revision": request.head_revision,
                    "remote_revision": observed,
                },
            )
        return PublishedBranch(branch=request.branch, revision=request.head_revision)

    def changed_paths(self, *, base_revision: str, head_revision: str) -> tuple[str, ...]:
        """List every path ``base_revision..head_revision`` changes, with renames split in two.

        Read by the host from the verified repository with external diff drivers and textconv
        disabled, rename detection off and submodule changes always reported (a worker-planted
        ``diff.ignoreSubmodules`` or ``submodule.*.ignore`` never hides a gitlink change), so
        a path moved out of a protected directory is reported at its old location as well as
        its new one. NUL-separated output keeps paths with newlines or quotes exact; bytes
        that are not UTF-8 are replaced, which can only make a path look different, never hide
        it from a pattern.
        """
        if _OBJECT_ID.fullmatch(head_revision) is None:
            raise InvalidInputError(
                "head_revision must be a full object id", details={"head_revision": head_revision}
            )
        if not base_revision.strip() or base_revision.startswith("-"):
            raise InvalidInputError(
                "base_revision must be a revision", details={"base_revision": base_revision}
            )
        self._require_commit(head_revision)
        resolved = self._run(
            ["rev-parse", "--verify", "--end-of-options", f"{base_revision}^{{commit}}"],
            cwd=self.source_path,
        )
        base = resolved.stdout.strip()
        if resolved.returncode != 0 or _OBJECT_ID.fullmatch(base) is None:
            raise ExternalConflictError(
                "the verified repository does not contain the base revision",
                details={"base_revision": base_revision},
            )
        completed = self._invoke_hardened(
            [
                "diff",
                "--name-only",
                "-z",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--ignore-submodules=none",
                f"{base}..{head_revision}",
                "--",
            ],
            cwd=self.source_path,
        )
        if completed.returncode != 0:
            raise ExternalConflictError(
                "the changed paths of the verified branch could not be read",
                details={"stderr": completed.stderr.decode("utf-8", "replace").strip()[-2000:]},
            )
        return tuple(
            entry.decode("utf-8", "replace") for entry in completed.stdout.split(b"\x00") if entry
        )

    def reconcile(self, request: BranchPublicationRequest) -> ReconciliationResult:
        """Compare the remote with what ``request`` would have left, from the request alone.

        The branch, head and expected head all come from the ledger-recorded request, so this
        holds across a coordinator restart. The remote branch at the head is ``FOUND_MATCHING``;
        absent, or still at the revision the publication expected, is ``NOT_FOUND`` (nothing was
        pushed, retrying is safe); anything else is an external conflict, never an overwrite.
        """
        remote = self._remote_revision(self._origin_url(), request.branch)
        if remote == request.head_revision:
            return ReconciliationResult.FOUND_MATCHING
        if remote is None or remote == request.expected_remote_revision:
            return ReconciliationResult.NOT_FOUND
        raise ExternalConflictError(
            "the remote branch holds a revision this publication neither pushed nor expected",
            details={
                "branch": request.branch,
                "expected_remote_revision": request.expected_remote_revision,
                "remote_revision": remote,
            },
        )

    def _origin_url(self) -> str:
        completed = self._run(
            ["-C", str(self.repository_path), "remote", "get-url", "origin"], cwd=None
        )
        url = completed.stdout.strip()
        if completed.returncode != 0 or not url:
            raise ExternalConflictError(
                "the host repository has no origin remote to publish to",
                details={"repository_path": str(self.repository_path)},
            )
        if url.startswith("-"):
            raise ExternalConflictError("the host repository's origin url is not a valid url")
        if not url.startswith("/") and ":" not in url.partition("/")[0]:
            # A relative local path means relative to the host repository, not to wherever the
            # verified source repository happens to live.
            return str((self.repository_path / url).resolve())
        return url

    def _require_commit(self, revision: str) -> None:
        completed = self._run(
            ["cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=self.source_path,
        )
        if completed.returncode != 0:
            raise ExternalConflictError(
                "the verified repository does not contain the revision to publish",
                details={"head_revision": revision},
            )

    def _remote_revision(self, url: str, branch: str) -> str | None:
        completed = self._run(
            ["ls-remote", "--", url, f"refs/heads/{branch}"],
            cwd=self.source_path,
        )
        if completed.returncode != 0:
            raise ExternalConflictError(
                "the remote branch could not be read",
                details={"branch": branch, "stderr": _scrub(completed.stderr, url)},
            )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        revision, _, reference = lines[0].partition("\t")
        if len(lines) != 1 or reference != f"refs/heads/{branch}":
            raise ExternalConflictError(
                "the remote branch listing is ambiguous", details={"branch": branch}
            )
        return revision

    def _run(
        self, arguments: Sequence[str], *, cwd: Path | None
    ) -> subprocess.CompletedProcess[str]:
        """Run hardened git and decode its output as text."""
        completed = self._invoke_hardened(arguments, cwd=cwd)
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            completed.stdout.decode("utf-8", "replace"),
            completed.stderr.decode("utf-8", "replace"),
        )

    def _invoke_hardened(
        self, arguments: Sequence[str], *, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        return self._invoke([*self._hardening(), *arguments], cwd=cwd)

    def _hardening(self) -> tuple[str, ...]:
        """Command-line settings that outrank anything a worker could plant in repository config.

        Hooks, fsmonitor, askpass and the ``ext``/``fd`` transports are switched off. The
        credential helper list and the ssh command are reset and then set to exactly what the
        operator's own user and system configuration says, so a worker-written
        ``credential.helper=!...`` or ``core.sshCommand`` in a shared repository config never
        runs with the operator's credentials while the operator's own helpers keep working.
        The reset also clears URL-scoped helpers (``credential.https://host.helper``, the form
        ``gh auth setup-git`` writes), so those entries are re-emitted too, in configuration
        order and from the system and user files only.
        """
        if self._hardening_arguments is None:
            ssh_commands = self._operator_values("core.sshCommand")
            settings = [
                "core.hooksPath=/dev/null",
                "core.fsmonitor=false",
                "core.askPass=",
                "protocol.ext.allow=never",
                "protocol.fd.allow=never",
                f"core.sshCommand={ssh_commands[-1] if ssh_commands else 'ssh'}",
                "credential.helper=",
                *(f"{key}={value}" for key, value in self._operator_credential_helpers()),
            ]
            self._hardening_arguments = tuple(
                argument for setting in settings for argument in ("-c", setting)
            )
        return self._hardening_arguments

    def _operator_credential_helpers(self) -> list[tuple[str, str]]:
        """Every ``credential.helper`` and ``credential.<url>.helper`` of the operator, in order.

        Read from the system then user configuration only, never a repository, and from a fixed
        directory that is not a repository, so a helper a worker planted next to the code is not
        among them and conditional includes keyed on a repository never apply. An empty value is
        kept: it is how the operator's own configuration clears helpers collected before it.
        """
        entries: list[tuple[str, str]] = []
        for scope in ("--system", "--global"):
            completed = self._invoke(
                [
                    "config",
                    "--includes",
                    scope,
                    "--null",
                    "--get-regexp",
                    r"^credential\.(.+\.)?helper$",
                ],
                cwd=_OPERATOR_CONFIG_DIRECTORY,
            )
            if completed.returncode != 0:
                continue
            for record in completed.stdout.split(b"\x00"):
                key, separator, value = record.decode("utf-8", "replace").partition("\n")
                if key and separator:
                    entries.append((key, value))
        return entries

    def _operator_values(self, key: str) -> list[str]:
        """Read ``key`` from the operator's system then user configuration only, never a repo.

        Run from a fixed directory that is not a repository, like the credential helper reads.
        """
        values: list[str] = []
        for scope in ("--system", "--global"):
            completed = self._invoke(
                ["config", "--includes", scope, "--null", "--get-all", key],
                cwd=_OPERATOR_CONFIG_DIRECTORY,
            )
            if completed.returncode == 0:
                values.extend(
                    value.decode("utf-8", "replace")
                    for value in completed.stdout.split(b"\x00")
                    if value
                )
        return values

    def _invoke(
        self, arguments: Sequence[str], *, cwd: Path | None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.executable, *arguments],
                cwd=cwd,
                env=publication_environment(),
                capture_output=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as error:
            if cwd is not None and not cwd.is_dir():
                raise ExternalConflictError(
                    "the verified repository to publish from does not exist",
                    details={"source_path": str(cwd)},
                ) from error
            raise MissingPrerequisiteError(
                "git is not available to publish branches",
                details={"executable": self.executable},
            ) from error


def _status(availability: AdapterAvailability, version: str | None, detail: str) -> AdapterStatus:
    return AdapterStatus(
        kind=ProviderKind.SOURCE_CONTROL,
        availability=availability,
        fingerprint=(
            None
            if version is None
            else AdapterFingerprint(
                provider_id="git-branch-publication",
                provider_version=version,
                api_version=ADAPTER_API_VERSION,
                capabilities=(
                    AdapterCapability("branch_publication", 1),
                    AdapterCapability("branch_publication_reconciliation", 1),
                    AdapterCapability("expected_head_lease", 1),
                    AdapterCapability("changed_paths", 1),
                ),
            )
        ),
        detail=detail,
    )


def _scrub(text: str, url: str) -> str:
    """Redact credentials from git's own words, including a url that embeds them."""
    cleaned = text.strip()[-_STDERR_DETAIL_CHARS:]
    for candidate in {url, _without_userinfo(url)}:
        cleaned = cleaned.replace(candidate, "<origin>")
    parts = urlsplit(url)
    if parts.password:
        cleaned = cleaned.replace(parts.password, "[REDACTED]")
    if parts.username:
        cleaned = cleaned.replace(parts.username, "[REDACTED]")
    return redact_credential_shaped_text(cleaned)


def _without_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if "@" not in parts.netloc or not parts.scheme:
        return url
    return urlunsplit(
        (parts.scheme, parts.netloc.rpartition("@")[2], parts.path, parts.query, parts.fragment)
    )


__all__ = ["PUBLICATION_ENVIRONMENT_KEYS", "GitBranchPublisher", "publication_environment"]
