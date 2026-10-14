"""Move a task tree into and out of a kernel-capped container volume, safely.

A worker's workspace and result directory live in tmpfs volumes whose size and inode count the
kernel enforces. The host never mounts a host path into the worker: it seeds the workspace by
streaming a tar archive into the volume, and after the worker exits it reads back the workspace
and the named result files. The volume content is written by an untrusted worker, so every byte
that returns to the host goes through the extraction here:

* names are relative, never empty, ``.`` or ``..``; every directory component is opened with
  ``O_NOFOLLOW`` from a descriptor on the extraction root, so a symlink the archive itself
  planted can never redirect a later member out of the tree;
* only directories, regular files, symlinks (stored verbatim, never followed) and hard links
  (stored as copies of an already extracted regular file) are materialised; FIFOs, sockets,
  devices and every other member type are skipped and counted;
* total bytes and entries are capped before anything is written, and the host disk keeps a
  free-space reserve;
* extracted files and directories are always owner-readable and -writable and never carry
  setuid, setgid or sticky bits, so the host can always delete what the worker made.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tarfile
import uuid
from dataclasses import dataclass
from typing import IO

from enginery.domain.errors import EngineryError

HOST_RESERVE_BYTES = 1 << 30
"""Free host disk space that extraction must never eat into."""

MAX_PATH_DEPTH = 256
MAX_LINK_TARGET_BYTES = 4096
_CHUNK = 1024 * 1024
_FILE_BITS = 0o777
_PRIVATE = os.O_CLOEXEC | os.O_NOFOLLOW


class TreeTransferError(EngineryError):
    """A tree could not be moved into or out of the capped store; the reason is the message."""


@dataclass(frozen=True, slots=True)
class TreeStats:
    """What one transfer moved: entries, regular-file bytes, and members it skipped."""

    entries: int = 0
    bytes_used: int = 0
    skipped: int = 0


# --- host tree -> archive ------------------------------------------------------------


def _owned(info: tarfile.TarInfo, uid: int, gid: int) -> tarfile.TarInfo:
    info.uid = uid
    info.gid = gid
    info.uname = ""
    info.gname = ""
    info.mtime = int(info.mtime)
    return info


def write_tree_archive(
    root: str, stream: IO[bytes], *, uid: int, gid: int, max_bytes: int, max_entries: int
) -> TreeStats:
    """Write the tree under ``root`` to ``stream`` as a tar archive owned by ``uid:gid``.

    Nothing is followed: symlinks are archived as links. A FIFO, socket or device inside the
    tree is refused, as is a tree over ``max_bytes`` of file content or ``max_entries`` entries
    (a worker would not have room for it in its capped volume). Hard links are archived as
    separate copies and counted as such.
    """
    entries = 0
    total = 0
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        pending = [("", root)]
        while pending:
            relative, directory = pending.pop()
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
            for child in children:
                name = f"{relative}{child.name}"
                info = child.stat(follow_symlinks=False)
                entries += 1
                if entries > max_entries:
                    raise TreeTransferError(
                        f"the workspace has more than {max_entries} entries; it cannot be bounded"
                    )
                header = tarfile.TarInfo(name)
                header.mode = stat.S_IMODE(info.st_mode) & _FILE_BITS
                header.mtime = info.st_mtime
                if stat.S_ISDIR(info.st_mode):
                    header.type = tarfile.DIRTYPE
                    header.mode |= 0o700
                    archive.addfile(_owned(header, uid, gid))
                    pending.append((f"{name}/", child.path))
                elif stat.S_ISLNK(info.st_mode):
                    header.type = tarfile.SYMTYPE
                    header.linkname = os.readlink(child.path)
                    archive.addfile(_owned(header, uid, gid))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > max_bytes:
                        raise TreeTransferError(
                            f"the workspace holds more than {max_bytes} bytes; it cannot be bounded"
                        )
                    header.size = info.st_size
                    header.mode |= 0o600
                    descriptor = os.open(child.path, os.O_RDONLY | _PRIVATE | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as handle:
                        opened = os.fstat(handle.fileno())
                        if not stat.S_ISREG(opened.st_mode) or opened.st_size != info.st_size:
                            raise TreeTransferError(f"{child.path} changed while it was read")
                        archive.addfile(_owned(header, uid, gid), handle)
                else:
                    raise TreeTransferError(
                        f"{child.path} is a special file (FIFO, socket or device); "
                        "a workspace holds only files, directories and symlinks"
                    )
    return TreeStats(entries=entries, bytes_used=total)


# --- archive -> host tree ------------------------------------------------------------


def _parts(name: str) -> list[str]:
    """The path components of a member name, or the refusal that explains why it is unusable."""
    if "\x00" in name or name.startswith("/"):
        raise TreeTransferError(f"archive member name {name!r} is not a relative path")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise TreeTransferError(f"archive member name {name!r} leaves the extraction root")
    if len(parts) > MAX_PATH_DEPTH:
        raise TreeTransferError(f"archive member name {name!r} is nested too deeply")
    return parts


def _open_directory(root: int, parts: list[str]) -> int:
    """A descriptor on the directory ``parts`` below ``root``, refusing any symlink on the way."""
    descriptor = os.dup(root)
    try:
        for part in parts:
            try:
                following = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | _PRIVATE, dir_fd=descriptor
                )
            except OSError as error:
                raise TreeTransferError(
                    f"archive member path component {part!r} is missing or not a directory"
                ) from error
            os.close(descriptor)
            descriptor = following
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _require_free_space(descriptor: int, needed: int) -> None:
    status = os.fstatvfs(descriptor)
    free = status.f_bavail * status.f_frsize
    if free - needed < HOST_RESERVE_BYTES:
        raise TreeTransferError(
            f"the host disk would fall below its {HOST_RESERVE_BYTES}-byte free-space reserve"
        )


def _write_file(parent: int, name: str, source: IO[bytes], size: int, mode: int) -> None:
    _require_free_space(parent, size)
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _PRIVATE, 0o600, dir_fd=parent
    )
    try:
        written = 0
        while written < size:
            chunk = source.read(min(_CHUNK, size - written))
            if not chunk:
                raise TreeTransferError("the archive ended inside a file")
            os.write(descriptor, chunk)
            written += len(chunk)
        os.fchmod(descriptor, (mode & _FILE_BITS) | 0o600)
    finally:
        os.close(descriptor)


def _copy_regular(root: int, parts: list[str], parent: int, name: str, *, budget: int) -> int:
    """Store ``name`` as a copy of the regular file already extracted at ``parts``."""
    directory = _open_directory(root, parts[:-1])
    try:
        source = os.open(parts[-1], os.O_RDONLY | _PRIVATE | os.O_NONBLOCK, dir_fd=directory)
    except OSError as error:
        raise TreeTransferError("a hard link names a file that was not extracted") from error
    finally:
        os.close(directory)
    with os.fdopen(source, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise TreeTransferError("a hard link names something that is not a regular file")
        if info.st_size > budget:
            raise TreeTransferError("the archive exceeds its byte cap")
        _write_file(parent, name, handle, info.st_size, info.st_mode)
        return info.st_size


def extract_tree_archive(
    stream: IO[bytes], destination: str, *, max_bytes: int, max_entries: int
) -> TreeStats:
    """Extract a tar archive read from ``stream`` into the empty directory ``destination``."""
    entries = 0
    total = 0
    skipped = 0
    root = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | _PRIVATE)
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                parts = _parts(member.name)
                if not parts:
                    continue  # the archive's own root directory
                entries += 1
                if entries > max_entries:
                    raise TreeTransferError(f"the archive has more than {max_entries} entries")
                parent = _open_directory(root, parts[:-1])
                try:
                    name = parts[-1]
                    if member.isdir():
                        with contextlib.suppress(FileExistsError):
                            os.mkdir(name, 0o700, dir_fd=parent)
                        created = os.open(
                            name, os.O_RDONLY | os.O_DIRECTORY | _PRIVATE, dir_fd=parent
                        )
                        try:
                            os.fchmod(created, (member.mode & _FILE_BITS) | 0o700)
                        finally:
                            os.close(created)
                    elif member.isreg():
                        total += member.size
                        if total > max_bytes:
                            raise TreeTransferError(
                                f"the archive holds more than {max_bytes} bytes"
                            )
                        source = archive.extractfile(member)
                        assert source is not None
                        _write_file(parent, name, source, member.size, member.mode)
                    elif member.issym():
                        if (
                            "\x00" in member.linkname
                            or len(member.linkname) > MAX_LINK_TARGET_BYTES
                        ):
                            raise TreeTransferError("a symlink target is unusable")
                        os.symlink(member.linkname, name, dir_fd=parent)
                    elif member.islnk():
                        total += _copy_regular(
                            root, _parts(member.linkname), parent, name, budget=max_bytes - total
                        )
                    else:
                        skipped += 1
                finally:
                    os.close(parent)
    except tarfile.TarError as error:
        raise TreeTransferError(f"the archive is unreadable: {error}") from error
    except OSError as error:
        raise TreeTransferError(f"the archive could not be extracted: {error.strerror}") from error
    finally:
        os.close(root)
    return TreeStats(entries=entries, bytes_used=total, skipped=skipped)


def replace_tree(
    stream: IO[bytes], destination: str, *, max_bytes: int, max_entries: int
) -> TreeStats:
    """Replace the directory ``destination`` with the tree in the archive on ``stream``.

    The archive is extracted beside ``destination`` first; only a complete extraction replaces
    it, so a refused or failed archive leaves the old tree untouched.
    """
    parent = os.path.dirname(destination)
    staging = os.path.join(parent, f".incoming-{uuid.uuid4().hex}")
    retired = os.path.join(parent, f".retired-{uuid.uuid4().hex}")
    os.mkdir(staging, 0o700)
    try:
        stats = extract_tree_archive(stream, staging, max_bytes=max_bytes, max_entries=max_entries)
        os.rename(destination, retired)
        try:
            os.rename(staging, destination)
        except BaseException:
            os.rename(
                retired, destination
            )  # keep the old tree if the new one cannot take its place
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(retired, ignore_errors=True)
    return stats


def store_named_file(stream: IO[bytes], directory: str, name: str, *, limit: int) -> bool:
    """Store the regular file ``name`` from a one-member archive in ``directory``.

    Returns ``False`` when the archive does not carry a regular file of that name (a symlink,
    FIFO or directory planted by the worker is never stored). Content over ``limit`` bytes is
    refused. The file is written beside its destination and renamed into place, so a reader
    never sees a partial one.
    """
    if "/" in name or name in ("", ".", ".."):
        raise TreeTransferError(f"{name!r} is not a plain file name")
    stored = False
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                if _parts(member.name) != [name] or not member.isreg():
                    continue
                if member.size > limit:
                    raise TreeTransferError(f"{name} exceeds the {limit}-byte limit")
                source = archive.extractfile(member)
                assert source is not None
                temporary = f".{name}.{uuid.uuid4().hex}.part"
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | _PRIVATE)
                try:
                    try:
                        _write_file(directory_fd, temporary, source, member.size, 0o600)
                        os.rename(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    except BaseException:
                        with contextlib.suppress(OSError):
                            os.unlink(temporary, dir_fd=directory_fd)
                        raise
                finally:
                    os.close(directory_fd)
                stored = True
    except tarfile.TarError as error:
        raise TreeTransferError(f"the archive is unreadable: {error}") from error
    except OSError as error:
        raise TreeTransferError(f"{name} could not be stored: {error.strerror}") from error
    return stored
