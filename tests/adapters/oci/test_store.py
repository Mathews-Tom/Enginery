"""Moving a tree into and out of the capped store: nothing a worker wrote can escape or hang."""

from __future__ import annotations

import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from enginery.adapters.oci import store
from enginery.adapters.oci.store import (
    TreeTransferError,
    extract_tree_archive,
    replace_tree,
    store_named_file,
    write_tree_archive,
)

_UID = 65532
_BIG = 1 << 30


def _archive(*members: tuple[tarfile.TarInfo, bytes | None]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, content in members:
            archive.addfile(info, None if content is None else io.BytesIO(content))
    buffer.seek(0)
    return buffer


def _file(name: str, content: bytes = b"x", mode: int = 0o644) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    return info, content


def _directory(name: str, mode: int = 0o755) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    return info, None


def _symlink(name: str, target: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, None


def _special(name: str, kind: bytes) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    return info, None


def _extract(archive: io.BytesIO, destination: Path, **limits: int) -> store.TreeStats:
    destination.mkdir(exist_ok=True)
    return extract_tree_archive(
        archive,
        str(destination),
        max_bytes=limits.get("max_bytes", _BIG),
        max_entries=limits.get("max_entries", 1000),
    )


# --- host tree -> archive ------------------------------------------------------------


def test_a_tree_round_trips_with_symlinks_kept_as_links_and_owners_set(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "a.txt").write_text("hello")
    (source / "sub" / "b.txt").write_text("world")
    (source / "link").symlink_to("a.txt")
    (source / "escape").symlink_to("/etc/passwd")
    buffer = io.BytesIO()

    stats = write_tree_archive(
        str(source), buffer, uid=_UID, gid=_UID, max_bytes=_BIG, max_entries=100
    )

    assert stats.entries == 5 and stats.bytes_used == 10
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive:
        owners = {(member.uid, member.gid) for member in archive.getmembers()}
    assert owners == {(_UID, _UID)}
    buffer.seek(0)
    _extract(buffer, tmp_path / "out")
    assert (tmp_path / "out" / "sub" / "b.txt").read_text() == "world"
    assert os.readlink(tmp_path / "out" / "link") == "a.txt"
    assert os.readlink(tmp_path / "out" / "escape") == "/etc/passwd"


def test_a_fifo_in_the_tree_being_seeded_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "pipe")

    with pytest.raises(TreeTransferError, match="special file"):
        write_tree_archive(
            str(source), io.BytesIO(), uid=_UID, gid=_UID, max_bytes=_BIG, max_entries=10
        )


def test_a_tree_over_its_byte_or_entry_cap_is_not_seeded(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "one").write_bytes(b"x" * 10)
    (source / "two").write_bytes(b"x" * 10)

    with pytest.raises(TreeTransferError, match="more than 15 bytes"):
        write_tree_archive(
            str(source), io.BytesIO(), uid=_UID, gid=_UID, max_bytes=15, max_entries=10
        )
    with pytest.raises(TreeTransferError, match="more than 1 entries"):
        write_tree_archive(
            str(source), io.BytesIO(), uid=_UID, gid=_UID, max_bytes=_BIG, max_entries=1
        )


# --- archive -> host tree ------------------------------------------------------------


@pytest.mark.parametrize("name", ["../escape", "a/../../escape", "/etc/escape"])
def test_a_member_name_that_leaves_the_root_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(TreeTransferError, match="member name"):
        _extract(_archive(_file(name)), tmp_path / "out")

    assert not (tmp_path / "escape").exists()


def test_a_member_cannot_be_written_through_a_symlink_the_archive_planted(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    archive = _archive(_symlink("hop", str(outside)), _file("hop/evil", b"pwned"))

    with pytest.raises(TreeTransferError, match="not a directory"):
        _extract(archive, tmp_path / "out")

    assert list(outside.iterdir()) == []


def test_a_member_cannot_be_written_through_a_relative_symlink_either(tmp_path: Path) -> None:
    (tmp_path / "escape").mkdir()
    archive = _archive(_symlink("hop", "../escape"), _file("hop/evil", b"pwned"))

    with pytest.raises(TreeTransferError):
        _extract(archive, tmp_path / "out")

    assert list((tmp_path / "escape").iterdir()) == []


def test_special_files_are_skipped_and_counted_not_created(tmp_path: Path) -> None:
    archive = _archive(
        _file("keep"),
        _special("pipe", tarfile.FIFOTYPE),
        _special("device", tarfile.CHRTYPE),
    )

    stats = _extract(archive, tmp_path / "out")

    assert stats.skipped == 2
    assert sorted(entry.name for entry in (tmp_path / "out").iterdir()) == ["keep"]


def test_modes_are_made_usable_and_never_carry_setuid_setgid_or_sticky(tmp_path: Path) -> None:
    archive = _archive(
        _directory("locked", 0o000),
        _file("locked/tool", b"x", 0o4755),
        _file("secret", b"x", 0o000),
    )

    _extract(archive, tmp_path / "out")

    tool = (tmp_path / "out" / "locked" / "tool").stat().st_mode
    assert stat.S_IMODE(tool) == 0o755 and not tool & (stat.S_ISUID | stat.S_ISGID)
    assert (tmp_path / "out" / "secret").stat().st_mode & 0o600 == 0o600
    assert (tmp_path / "out" / "locked").stat().st_mode & 0o700 == 0o700


def test_a_hard_link_member_becomes_a_copy_of_the_extracted_file(tmp_path: Path) -> None:
    link = tarfile.TarInfo("copy")
    link.type = tarfile.LNKTYPE
    link.linkname = "original"

    stats = _extract(_archive(_file("original", b"data"), (link, None)), tmp_path / "out")

    assert (tmp_path / "out" / "copy").read_bytes() == b"data"
    assert (tmp_path / "out" / "copy").stat().st_ino != (
        tmp_path / "out" / "original"
    ).stat().st_ino
    assert stats.bytes_used == 8


def test_a_hard_link_to_a_file_outside_the_tree_is_refused(tmp_path: Path) -> None:
    link = tarfile.TarInfo("copy")
    link.type = tarfile.LNKTYPE
    link.linkname = "../outside"
    (tmp_path / "outside").write_text("secret")

    with pytest.raises(TreeTransferError):
        _extract(_archive((link, None)), tmp_path / "out")

    assert not (tmp_path / "out" / "copy").exists()


def test_an_archive_over_its_byte_or_entry_cap_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TreeTransferError, match="more than 5 bytes"):
        _extract(_archive(_file("a", b"123"), _file("b", b"456")), tmp_path / "one", max_bytes=5)
    with pytest.raises(TreeTransferError, match="more than 1 entries"):
        _extract(_archive(_file("a"), _file("b")), tmp_path / "two", max_entries=1)


def test_extraction_keeps_a_free_space_reserve_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "HOST_RESERVE_BYTES", 1 << 62)

    with pytest.raises(TreeTransferError, match="free-space reserve"):
        _extract(_archive(_file("a")), tmp_path / "out")

    assert not (tmp_path / "out" / "a").exists()


def test_a_truncated_archive_is_refused(tmp_path: Path) -> None:
    whole = _archive(_file("a", b"x" * 4096)).getvalue()

    with pytest.raises(TreeTransferError):
        _extract(io.BytesIO(whole[:700]), tmp_path / "out")


class _ShortWriter:
    """``os`` as the store sees it, with ``write`` taking at most three bytes per call."""

    def __init__(self) -> None:
        self.calls = 0

    def write(self, descriptor: int, data: bytes | memoryview) -> int:
        self.calls += 1
        return os.write(descriptor, bytes(data[:3]))

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)


def test_a_file_is_written_whole_when_the_host_takes_only_part_of_each_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = bytes(range(256)) * 40
    writer = _ShortWriter()
    monkeypatch.setattr(store, "os", writer)

    _extract(_archive(_file("big.bin", content)), tmp_path / "out")

    assert (tmp_path / "out" / "big.bin").read_bytes() == content
    assert writer.calls >= len(content) // 3


def test_a_write_that_makes_no_progress_is_refused_rather_than_spun_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Stuck(_ShortWriter):
        def write(self, descriptor: int, data: bytes | memoryview) -> int:
            return 0

    monkeypatch.setattr(store, "os", Stuck())

    with pytest.raises(TreeTransferError, match="refused to take more"):
        _extract(_archive(_file("a", b"data")), tmp_path / "out")


def test_replace_tree_swaps_only_a_complete_extraction(tmp_path: Path) -> None:
    destination = tmp_path / "workspace"
    destination.mkdir()
    (destination / "old").write_text("old")

    replace_tree(_archive(_file("new", b"new")), str(destination), max_bytes=_BIG, max_entries=10)

    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["workspace"]
    assert [entry.name for entry in destination.iterdir()] == ["new"]

    with pytest.raises(TreeTransferError):
        replace_tree(
            _archive(_file("later", b"x"), _file("../bad")),
            str(destination),
            max_bytes=_BIG,
            max_entries=10,
        )
    assert [entry.name for entry in destination.iterdir()] == ["new"]
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["workspace"]


def test_staging_trees_are_named_after_the_workspace_and_logged_when_they_outlive_the_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from enginery.engine.task_repository import remove_task_local_workspace, staging_trees_for

    destination = tmp_path / "task-7"
    destination.mkdir()
    (destination / "old").write_text("old")
    monkeypatch.setattr(store, "remove_trees", lambda roots: tuple(roots))
    messages: list[str] = []

    replace_tree(
        _archive(_file("new", b"new")),
        str(destination),
        max_bytes=_BIG,
        max_entries=10,
        log=messages.append,
    )
    with pytest.raises(TreeTransferError):
        replace_tree(
            _archive(_file("../bad")),
            str(destination),
            max_bytes=_BIG,
            max_entries=10,
            log=messages.append,
        )

    left = sorted(entry.name for entry in tmp_path.iterdir() if entry.name != "task-7")
    assert len(left) == 2
    assert left[0].startswith("task-7.incoming-") and left[1].startswith("task-7.retired-")
    assert len(messages) == 2 and all("could not remove" in message for message in messages)
    assert {path.name for path in staging_trees_for(destination)} == set(left)

    # The workspace's ordinary cleanup takes the stragglers with it.
    monkeypatch.undo()
    assert remove_task_local_workspace(workspace_path=destination) == ()
    assert list(tmp_path.iterdir()) == []


# --- named result files --------------------------------------------------------------


def test_a_named_regular_file_is_stored_atomically(tmp_path: Path) -> None:
    stored = store_named_file(
        _archive(_file("result.json", b"{}")), str(tmp_path), "result.json", limit=100
    )

    assert stored
    assert (tmp_path / "result.json").read_bytes() == b"{}"
    assert [entry.name for entry in tmp_path.iterdir()] == ["result.json"]


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE])
def test_a_symlink_fifo_or_directory_is_never_stored_as_the_result(
    tmp_path: Path, kind: bytes
) -> None:
    info = tarfile.TarInfo("result.json")
    info.type = kind
    info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""

    assert not store_named_file(_archive((info, None)), str(tmp_path), "result.json", limit=100)
    assert list(tmp_path.iterdir()) == []


def test_a_member_with_another_name_is_not_stored(tmp_path: Path) -> None:
    assert not store_named_file(
        _archive(_file("other.json", b"{}")), str(tmp_path), "result.json", limit=100
    )
    assert list(tmp_path.iterdir()) == []


def test_a_named_file_over_its_limit_is_refused_without_a_partial_file(tmp_path: Path) -> None:
    with pytest.raises(TreeTransferError, match="exceeds the 3-byte limit"):
        store_named_file(
            _archive(_file("result.json", b"toolong")), str(tmp_path), "result.json", limit=3
        )

    assert list(tmp_path.iterdir()) == []


def test_a_stale_result_is_replaced_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("keep")
    directory = tmp_path / "out"
    directory.mkdir()
    (directory / "result.json").symlink_to(outside)

    store_named_file(_archive(_file("result.json", b"new")), str(directory), "result.json", limit=9)

    assert outside.read_text() == "keep"
    assert not (directory / "result.json").is_symlink()
    assert (directory / "result.json").read_text() == "new"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b"])
def test_only_a_plain_file_name_can_be_stored(tmp_path: Path, name: str) -> None:
    with pytest.raises(TreeTransferError, match="plain file name"):
        store_named_file(_archive(), str(tmp_path), name, limit=1)
