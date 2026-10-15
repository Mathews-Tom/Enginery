"""Atomic, link-safe file writes shared by the lockfile and the install records.

A write goes to a fresh, exclusively created file in the destination directory
(``mkstemp`` picks an unpredictable name and creates it with ``O_EXCL``, so a
planted file or symlink is never written through), is synced, and is renamed
over the destination, which replaces a link at the destination rather than
following it. The directory is synced afterwards.

:func:`require_no_link_chain` refuses a symlink planted where a write would be
redirected, so ``.enginery -> /elsewhere`` cannot move a lockfile, install root,
owned manifest or store into another tree. The rule: the anchor is the current
directory (``os.getcwd()``, which the operating system reports without links),
the directory the command line resolves relative paths against. A path inside
the anchor, relative or absolute, has every existing component strictly below
the anchor checked with ``lstat``; "inside" is decided by real location, so a path
spelled through a symbolic link to the current directory counts. A path outside
the anchor is operator-chosen:
its ancestors are never checked (system links such as macOS ``/tmp`` and
``/var`` are legitimate), only the named root itself, and only when the caller
asks for it; what the tool owns beneath that root is guarded by the install
and lockfile code. The check precedes the write, so it narrows rather than
eliminates a race with a process that can already write the workspace.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from enginery.capabilities.errors import CapabilitySymlinkError


def require_no_link_chain(path: Path, *, what: str, check_named_root: bool = True) -> None:
    """Refuse ``path`` when a symbolic link redirects it, by the rule in the module docstring.

    ``check_named_root`` is ``False`` for a directory that is not itself owned by the tool
    (the directory holding a lockfile), so only the part below the anchor is checked.
    """

    anchor = Path(os.path.realpath(os.getcwd()))
    absolute = Path(os.path.abspath(path))
    base = _anchor_prefix(absolute, anchor)
    if base is None:
        if check_named_root:
            _refuse_link(absolute, path, what)
        return
    current = base
    for part in absolute.relative_to(base).parts:
        current = current / part
        if not _refuse_link(current, path, what):
            return


def _anchor_prefix(absolute: Path, anchor: Path) -> Path | None:
    """The shortest prefix of ``absolute`` whose real location is ``anchor``, if any.

    Classifying by real location rather than spelling covers a path written through a
    symbolic link to the current directory (the ``$PWD`` form under a linked workspace).
    """

    prefix = Path(absolute.anchor)
    candidates = [prefix]
    for part in absolute.relative_to(prefix).parts:
        prefix = prefix / part
        candidates.append(prefix)
    for candidate in candidates:
        if os.path.realpath(candidate) == str(anchor):
            return candidate
    return None


def _refuse_link(candidate: Path, path: Path, what: str) -> bool:
    """Raise if ``candidate`` is a link; ``False`` once the chain stops existing."""

    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise CapabilitySymlinkError(
            f"{what} is reached through a symbolic link",
            details={"path": str(path), "link": str(candidate)},
        )
    return True


def fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_temporary(directory: Path, payload: bytes, *, prefix: str) -> Path:
    """Write ``payload`` to a fresh, exclusively created, synced file in ``directory``."""

    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def write_atomically(path: Path, payload: bytes, *, prefix: str) -> None:
    """Replace ``path`` with ``payload`` in one rename; a link at ``path`` is replaced."""

    temporary = write_temporary(path.parent, payload, prefix=prefix)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


__all__ = ["fsync_directory", "require_no_link_chain", "write_atomically", "write_temporary"]
