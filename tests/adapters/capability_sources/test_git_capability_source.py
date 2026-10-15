"""The Git capability source reads the tracked tree of one pinned commit, never the worktree."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from tests.support.capabilities import HOST, FakeCapabilitySource, package_files

from enginery.adapters.git_capability import GitCapabilitySource, git_environment
from enginery.application.adapter_types import AdapterAvailability
from enginery.application.delivery_ports import PackageFile
from enginery.application.git_environment import repository_isolated_environment
from enginery.capabilities.closure import MAX_FILE_BYTES, MAX_TOTAL_BYTES, build_closure
from enginery.capabilities.errors import (
    CapabilityAssetTypeError,
    CapabilityClosureLimitError,
    CapabilityManifestError,
    CapabilitySourceError,
    CapabilitySymlinkError,
)
from enginery.capabilities.lock import ProvenanceStatus
from enginery.capabilities.manifest import CommandSpec, PackageKind
from enginery.capabilities.resolver import CapabilityRequest, CapabilityResolver
from enginery.capabilities.sources import CapabilitySource, SourceKind
from enginery.domain.errors import InvalidInputError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_UNKNOWN_SHA = "1" * 40


def _env() -> dict[str, str]:
    environment = repository_isolated_environment()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Tester",
            "GIT_AUTHOR_EMAIL": "tester@example.invalid",
            "GIT_COMMITTER_NAME": "Tester",
            "GIT_COMMITTER_EMAIL": "tester@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    return environment


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        check=True,
        cwd=repo,
        env=_env(),
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    return path


def _write_package(base: Path, directory: str, files: Sequence[PackageFile]) -> Path:
    package = base / directory
    for item in files:
        target = package / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)
        target.chmod(0o755 if item.executable else 0o644)
    return package


def _commit(repo: Path, message: str = "commit") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _as_set(files: Sequence[PackageFile]) -> set[tuple[str, bytes, bool]]:
    return {(item.path, item.content, item.executable) for item in files}


def _packaged_repo(
    tmp_path: Path,
    files: Sequence[PackageFile] | None = None,
    *,
    directory: str = "packages/skill-a",
) -> tuple[Path, str, tuple[PackageFile, ...]]:
    repo = _init_repo(tmp_path / "repo")
    committed = tuple(files) if files is not None else package_files("skill-a")
    _write_package(repo, directory, committed)
    return repo, _commit(repo), committed


def _source(repo: Path, sha: str, *, root: str = "packages") -> GitCapabilitySource:
    return GitCapabilitySource(repository=str(repo), revision=sha, package_root=root)


def test_discover_resolve_and_fetch_read_a_package_under_a_subdirectory(tmp_path: Path) -> None:
    repo, sha, files = _packaged_repo(tmp_path)
    source = _source(repo, sha)

    listings = source.discover()
    descriptor = source.resolve("skill-a", "1")
    fetched = source.fetch_closure("skill-a", "1")

    assert [(item.name, item.version, item.revision) for item in listings] == [
        ("skill-a", "1", sha)
    ]
    assert descriptor is not None
    assert descriptor.revision == sha
    assert descriptor.digest == build_closure(files).digest
    assert _as_set(fetched) == _as_set(files)


def test_a_repository_root_layout_needs_no_package_root(tmp_path: Path) -> None:
    repo, sha, files = _packaged_repo(tmp_path, directory="skill-a")
    source = GitCapabilitySource(repository=str(repo), revision=sha)

    descriptor = source.resolve("skill-a", "1")

    assert [item.name for item in source.discover()] == ["skill-a"]
    assert descriptor is not None
    assert descriptor.digest == build_closure(files).digest


def test_resolve_returns_none_for_unknown_name_or_version(tmp_path: Path) -> None:
    repo, sha, _ = _packaged_repo(tmp_path)
    source = _source(repo, sha)

    assert source.resolve("ghost", "1") is None
    assert source.resolve("skill-a", "2") is None
    with pytest.raises(CapabilitySourceError):
        source.fetch_closure("skill-a", "2")


def test_untracked_and_uncommitted_worktree_content_is_not_part_of_the_closure(
    tmp_path: Path,
) -> None:
    repo, sha, files = _packaged_repo(tmp_path)
    package = repo / "packages" / "skill-a"
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "x.pyc").write_bytes(b"\x00compiled")
    (package / "SKILL.md").write_bytes(b"uncommitted edit\n")
    (package / "references" / "extra.md").write_bytes(b"untracked file\n")
    source = _source(repo, sha)

    descriptor = source.resolve("skill-a", "1")

    assert descriptor is not None
    assert descriptor.digest == build_closure(files).digest
    assert _as_set(source.fetch_closure("skill-a", "1")) == _as_set(files)


def test_a_later_commit_does_not_change_what_the_older_sha_resolves_to(tmp_path: Path) -> None:
    repo, first, files = _packaged_repo(tmp_path)
    (repo / "packages" / "skill-a" / "SKILL.md").write_bytes(b"second revision\n")
    second = _commit(repo, "second")

    older = _source(repo, first).resolve("skill-a", "1")
    newer = _source(repo, second).resolve("skill-a", "1")

    assert older is not None
    assert newer is not None
    assert older.digest == build_closure(files).digest
    assert older.revision == first
    assert newer.revision == second
    assert newer.digest != older.digest


@pytest.mark.parametrize("revision", ["abc1234", "main", "HEAD", "A" * 40, "a" * 39, "a" * 41])
def test_only_a_full_lowercase_sha_is_accepted(tmp_path: Path, revision: str) -> None:
    repo, sha, _ = _packaged_repo(tmp_path)

    with pytest.raises(InvalidInputError):
        GitCapabilitySource(repository=str(repo), revision=revision)
    with pytest.raises(InvalidInputError):
        GitCapabilitySource(repository=str(repo), revision=sha.upper())


def test_an_unknown_but_well_formed_sha_fails_on_first_use(tmp_path: Path) -> None:
    repo, _, _ = _packaged_repo(tmp_path)
    source = _source(repo, _UNKNOWN_SHA)

    with pytest.raises(CapabilitySourceError):
        source.discover()
    with pytest.raises(CapabilitySourceError):
        source.resolve("skill-a", "1")


def test_a_symlink_entry_in_a_package_is_refused_with_its_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    package = _write_package(repo, "packages/skill-a", package_files("skill-a"))
    os.symlink("../SKILL.md", package / "references" / "link.md")
    sha = _commit(repo)
    source = _source(repo, sha)

    with pytest.raises(CapabilitySymlinkError) as resolved:
        source.resolve("skill-a", "1")
    with pytest.raises(CapabilitySymlinkError):
        source.fetch_closure("skill-a", "1")

    assert resolved.value.details["path"] == "packages/skill-a/references/link.md"
    assert resolved.value.details["mode"] == "120000"


def test_a_submodule_entry_in_a_package_is_refused(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_package(repo, "packages/skill-a", package_files("skill-a"))
    _git(repo, "add", "-A")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{_UNKNOWN_SHA},packages/skill-a/vendor",
    )
    _git(repo, "commit", "-q", "-m", "submodule")
    sha = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(CapabilitySymlinkError) as caught:
        _source(repo, sha).resolve("skill-a", "1")

    assert caught.value.details["path"] == "packages/skill-a/vendor"
    assert caught.value.details["mode"] == "160000"


def test_a_package_path_that_is_itself_a_symlink_is_refused(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_package(repo, "packages/real-package", package_files("skill-a"))
    os.symlink("real-package", repo / "packages" / "skill-a")
    sha = _commit(repo)
    source = _source(repo, sha)

    with pytest.raises(CapabilitySymlinkError) as caught:
        source.resolve("skill-a", "1")
    with pytest.raises(CapabilitySymlinkError):
        source.fetch_closure("skill-a", "1")

    assert caught.value.details["path"] == "packages/skill-a"


def test_an_unreviewed_asset_type_tracked_in_the_tree_is_refused_on_resolve(
    tmp_path: Path,
) -> None:
    repo, sha, _ = _packaged_repo(
        tmp_path, package_files("skill-a", extra={"payload.bin": b"\x00\x01"})
    )
    source = _source(repo, sha)

    assert [item.name for item in source.discover()] == ["skill-a"]
    with pytest.raises(CapabilityAssetTypeError):
        source.resolve("skill-a", "1")


def test_the_executable_bit_of_a_tracked_script_is_preserved(tmp_path: Path) -> None:
    files = package_files(
        "tool-a",
        kind=PackageKind.COMMAND,
        command=CommandSpec.create(argv=("scripts/run.sh",), input_schema={"type": "object"}),
    )
    assert any(item.executable for item in files)
    repo, sha, _ = _packaged_repo(tmp_path, files, directory="packages/tool-a")
    source = _source(repo, sha)

    fetched = source.fetch_closure("tool-a", "1")
    descriptor = source.resolve("tool-a", "1")

    assert _as_set(fetched) == _as_set(files)
    assert descriptor is not None
    assert descriptor.digest == build_closure(files).digest


def test_a_manifest_named_differently_from_its_directory_is_refused(tmp_path: Path) -> None:
    repo, sha, _ = _packaged_repo(tmp_path, package_files("other-name"))
    source = _source(repo, sha)

    with pytest.raises(CapabilityManifestError):
        source.discover()
    with pytest.raises(CapabilityManifestError):
        source.resolve("skill-a", "1")


def test_a_directory_without_a_manifest_is_not_listed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_package(repo, "packages/skill-a", package_files("skill-a"))
    (repo / "packages" / "notes").mkdir()
    (repo / "packages" / "notes" / "readme.txt").write_bytes(b"not a package")
    sha = _commit(repo)

    assert [item.name for item in _source(repo, sha).discover()] == ["skill-a"]


def test_git_environment_drops_redirecting_and_config_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hostile = [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
    ]
    for key in hostile:
        monkeypatch.setenv(key, str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global-config"))

    environment = git_environment()

    assert not [key for key in hostile if key in environment]
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CEILING_DIRECTORIES" not in environment


def test_an_inherited_git_dir_cannot_redirect_the_source_to_another_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, sha, files = _packaged_repo(tmp_path)
    decoy = _init_repo(tmp_path / "decoy")
    _write_package(decoy, "packages/decoy-pkg", package_files("decoy-pkg"))
    _commit(decoy)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    source = _source(repo, sha)

    descriptor = source.resolve("skill-a", "1")

    assert [item.name for item in source.discover()] == ["skill-a"]
    assert source.resolve("decoy-pkg", "1") is None
    assert descriptor is not None
    assert descriptor.digest == build_closure(files).digest


def test_hostile_repository_configuration_does_not_execute_code(tmp_path: Path) -> None:
    repo, sha, files = _packaged_repo(tmp_path)
    marker = tmp_path / "marker"
    command = f"touch {marker}"
    config = repo / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"[core]\n\tfsmonitor = {command}\n\tpager = {command}\n\tsshCommand = {command}\n"
        + f"[alias]\n\tls-tree = !{command}\n"
        + f"[diff]\n\texternal = {command}\n",
        encoding="utf-8",
    )
    source = _source(repo, sha)

    descriptor = source.resolve("skill-a", "1")
    source.discover()
    fetched = source.fetch_closure("skill-a", "1")
    source.probe()

    assert descriptor is not None
    assert _as_set(fetched) == _as_set(files)
    assert not marker.exists()


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for path in sorted(directory.rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(directory))] = (info.st_size, info.st_mtime_ns)
    return result


def test_the_source_never_writes_into_the_repository_it_reads(tmp_path: Path) -> None:
    repo, sha, _ = _packaged_repo(tmp_path)
    (repo / "packages" / "skill-a" / "SKILL.md").write_bytes(b"dirty worktree\n")
    status_before = _git(repo, "status", "--porcelain")
    before = _snapshot(repo / ".git")
    source = _source(repo, sha)

    source.probe()
    source.discover()
    source.resolve("skill-a", "1")
    source.fetch_closure("skill-a", "1")

    assert _snapshot(repo / ".git") == before
    assert not (repo / ".git" / "index.lock").exists()
    assert _git(repo, "status", "--porcelain") == status_before


def test_a_url_source_matches_the_local_descriptor_and_leaves_no_mirror_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, files = _packaged_repo(tmp_path)
    mirrors: list[Path] = []
    modes: list[int] = []
    real_populate = GitCapabilitySource._populate

    def recording_populate(self: GitCapabilitySource, mirror: Path) -> None:
        mirrors.append(mirror)
        modes.append(stat.S_IMODE(mirror.stat().st_mode))
        real_populate(self, mirror)

    monkeypatch.setattr(GitCapabilitySource, "_populate", recording_populate)
    remote = GitCapabilitySource(repository=f"file://{repo}", revision=sha, package_root="packages")

    descriptor = remote.resolve("skill-a", "1")

    assert descriptor == _source(repo, sha).resolve("skill-a", "1")
    assert descriptor is not None
    assert descriptor.digest == build_closure(files).digest
    assert _as_set(remote.fetch_closure("skill-a", "1")) == _as_set(files)
    assert len(mirrors) == 3  # one fresh mirror per port call, including the local comparison
    assert modes == [0o700] * 3
    assert not [mirror for mirror in mirrors if mirror.exists()]
    assert not (repo / ".git" / "index.lock").exists()


def test_git_never_runs_inside_the_repository_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha, _ = _packaged_repo(tmp_path)
    calls: list[tuple[list[str], str, dict[str, str]]] = []
    real_run = subprocess.run

    def recording_run(command: list[str], **kwargs: Any) -> Any:
        calls.append((command, str(kwargs["cwd"]), kwargs["env"]))
        return real_run(command, **kwargs)

    monkeypatch.setattr("enginery.adapters.git_capability.subprocess.run", recording_run)
    source = _source(repo, sha)

    source.discover()
    source.fetch_closure("skill-a", "1")

    assert calls
    fetches = [call for call in calls if "fetch" in call[0]]
    assert len(fetches) == 2
    for command, cwd, environment in calls:
        assert not Path(cwd).resolve().is_relative_to(repo.resolve())
        assert str(repo / ".git") not in command
        assert environment["GIT_NO_LAZY_FETCH"] == "1"
        expected = "file" if "fetch" in command else None
        assert (
            environment["GIT_ALLOW_PROTOCOL"]
            == git_environment(protocols=expected)["GIT_ALLOW_PROTOCOL"]
        )
    for command, _, _ in fetches:
        assert f"file://{repo.resolve()}" in command


def _git_stdin(repo: Path, data: str, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        check=True,
        cwd=repo,
        env=_env(),
        input=data,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _partial_clone_repo(tmp_path: Path, sentinel: Path) -> tuple[Path, str]:
    """A repository whose pinned tree names a blob that is not in its object store.

    Its configuration declares a promisor remote and an ssh command that touches
    ``sentinel``; any git command run inside it that needs the missing blob would
    lazily fetch from that remote and run the command.
    """

    repo = _init_repo(tmp_path / "promisor-source")
    manifest = package_files("skill-a")[0]
    stored = _git_stdin(repo, manifest.content.decode(), "hash-object", "-w", "--stdin")
    missing = _git_stdin(repo, "never stored\n", "hash-object", "--stdin")
    references = _git_stdin(repo, f"100644 blob {missing}\tguide.md\n", "mktree", "--missing")
    package = _git_stdin(
        repo,
        f"100644 blob {stored}\tcapability.json\n040000 tree {references}\treferences\n",
        "mktree",
        "--missing",
    )
    packages = _git_stdin(repo, f"040000 tree {package}\tskill-a\n", "mktree")
    root = _git_stdin(repo, f"040000 tree {packages}\tpackages\n", "mktree")
    commit = _git(repo, "commit-tree", "-m", "partial", root)
    config = repo / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"[core]\n\trepositoryformatversion = 1\n\tsshCommand = touch {sentinel}; false\n"
        + "[extensions]\n\tpartialClone = origin\n"
        + '[remote "origin"]\n\turl = ssh://promisor.invalid/repo\n\tpromisor = true\n',
        encoding="utf-8",
    )
    return repo, commit


def test_a_partial_clone_source_cannot_lazily_fetch_and_run_its_configured_command(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "sentinel"
    repo, commit = _partial_clone_repo(tmp_path, sentinel)
    source = _source(repo, commit)

    with pytest.raises(CapabilitySourceError):
        source.resolve("skill-a", "1")
    with pytest.raises(CapabilitySourceError):
        source.fetch_closure("skill-a", "1")
    with pytest.raises(CapabilitySourceError):
        source.discover()
    assert source.probe().availability is AdapterAvailability.MISCONFIGURED

    assert not sentinel.exists()


@pytest.mark.parametrize(
    "repository",
    [
        "--upload-pack=x",
        "ext::sh -c x",
        "-c",
        "ftp://example.invalid/repo.git",
        "relative/missing",
    ],
)
def test_a_repository_that_is_neither_a_directory_nor_an_allowed_url_is_rejected(
    tmp_path: Path, repository: str
) -> None:
    with pytest.raises(InvalidInputError):
        GitCapabilitySource(repository=repository, revision=_UNKNOWN_SHA)


def test_probe_reports_available_for_a_good_source_and_misconfigured_for_an_unknown_commit(
    tmp_path: Path,
) -> None:
    repo, sha, _ = _packaged_repo(tmp_path)

    good = _source(repo, sha).probe()
    bad = _source(repo, _UNKNOWN_SHA).probe()

    assert good.availability is AdapterAvailability.AVAILABLE
    assert bad.availability is AdapterAvailability.MISCONFIGURED


def test_a_signature_claim_is_carried_into_the_descriptor(tmp_path: Path) -> None:
    repo, sha, _ = _packaged_repo(tmp_path)
    source = GitCapabilitySource(
        repository=str(repo),
        revision=sha,
        package_root="packages",
        signatures={("skill-a", "1"): (b"sig", "key-1")},
    )

    descriptor = source.resolve("skill-a", "1")

    assert descriptor is not None
    assert descriptor.signature == b"sig"
    assert descriptor.signer_key_id == "key-1"


def test_a_git_registered_package_is_unauthenticated_run_introduced_and_needs_approval(
    tmp_path: Path,
) -> None:
    repo, sha, _ = _packaged_repo(tmp_path)
    resolver = CapabilityResolver(
        [CapabilitySource(SourceKind.GIT, "git", _source(repo, sha))], host=HOST
    )

    lock = resolver.resolve([CapabilityRequest(name="skill-a", version="1")])

    entry = lock.entries[0]
    assert entry.revision == sha
    assert entry.provenance.status is ProvenanceStatus.UNAUTHENTICATED
    assert entry.introduced_by_run is True
    assert entry.requires_human_approval()


def test_a_git_replace_of_the_pinned_commit_does_not_change_the_closure_read(
    tmp_path: Path,
) -> None:
    repo, pinned, files = _packaged_repo(tmp_path)
    _write_package(repo, "packages/skill-a", package_files("skill-a", body=b"# swapped body\n"))
    impostor = _commit(repo, "impostor")
    _git(repo, "replace", pinned, impostor)
    source = _source(repo, pinned)

    descriptor = source.resolve("skill-a", "1")
    fetched = source.fetch_closure("skill-a", "1")

    assert descriptor is not None
    assert descriptor.revision == pinned
    assert descriptor.digest == build_closure(files).digest
    assert _as_set(fetched) == _as_set(files)


def test_git_environment_is_built_from_an_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inherited = {"SSH_AUTH_SOCK": str(tmp_path / "agent.sock"), "LC_CTYPE": "en_US.UTF-8"}
    ambient = {
        "GIT_SSH_COMMAND": "ssh -o ProxyCommand=evil",
        "GIT_ASKPASS": "/tmp/askpass",
        "GIT_EXEC_PATH": "/tmp/exec",
        "GIT_PROXY_COMMAND": "/tmp/proxy",
        "GITHUB_TOKEN": "secret",
        "HTTPS_PROXY": "http://proxy.invalid",
    }
    for key, value in {**inherited, **ambient}.items():
        monkeypatch.setenv(key, value)

    environment = git_environment()

    assert not [key for key in ambient if key in environment]
    assert all(environment[key] == value for key, value in inherited.items())
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_ALLOW_PROTOCOL"] not in {"file", "https", "ssh"}
    assert not {"file", "https", "ssh"} & set(environment["GIT_ALLOW_PROTOCOL"].split(":"))
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_LITERAL_PATHSPECS"] == "1"
    assert git_environment(protocols="file")["GIT_ALLOW_PROTOCOL"] == "file"
    assert "PATH" in environment


def test_the_package_wide_byte_limit_is_enforced_before_any_blob_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = MAX_TOTAL_BYTES // MAX_FILE_BYTES + 1
    bulk = {f"references/bulk-{index}.md": b"x" * MAX_FILE_BYTES for index in range(count)}
    repo, sha, _ = _packaged_repo(tmp_path, package_files("skill-a", extra=bulk))
    source = _source(repo, sha)
    read: list[str] = []
    real_blob = GitCapabilitySource._blob

    def recording_blob(self: GitCapabilitySource, item: Any) -> bytes:
        read.append(item.path)
        return real_blob(self, item)

    monkeypatch.setattr(GitCapabilitySource, "_blob", recording_blob)

    with pytest.raises(CapabilityClosureLimitError, match="too large"):
        source.fetch_closure("skill-a", "1")

    assert read == ["packages/skill-a/capability.json"]


def test_a_project_source_over_a_pinned_commit_is_local_trusted_at_that_revision(
    tmp_path: Path,
) -> None:
    repo, pinned, files = _packaged_repo(tmp_path)
    _write_package(repo, "packages/skill-a", package_files("skill-a", body=b"# later commit\n"))
    _commit(repo, "a later commit")
    (repo / "packages" / "skill-a" / "SKILL.md").write_bytes(b"# uncommitted edit\n")
    source = CapabilitySource(SourceKind.PROJECT, f"project:{repo}", _source(repo, pinned))

    lock = CapabilityResolver([source], host=HOST).resolve(
        [CapabilityRequest(name="skill-a", version="1")]
    )

    (entry,) = lock.entries
    assert entry.revision == pinned
    assert entry.digest == build_closure(files).digest
    assert entry.provenance.status is ProvenanceStatus.LOCAL_TRUSTED
    assert entry.introduced_by_run is False
    assert not entry.requires_human_approval()


@pytest.mark.parametrize("pinned_commit", [None, "", "main", "abc123", "A" * 40])
def test_a_project_source_must_be_pinned_to_a_full_lowercase_commit(
    pinned_commit: str | None,
) -> None:
    port = FakeCapabilitySource.of(pinned_commit=pinned_commit)

    with pytest.raises(InvalidInputError, match="never a reviewed base"):
        CapabilitySource(SourceKind.PROJECT, "project", port)
