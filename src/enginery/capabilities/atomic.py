"""Atomic, link-safe file writes shared by the lockfile and the install records.

A write goes to a fresh, exclusively created file in the destination directory
(``mkstemp`` picks an unpredictable name and creates it with ``O_EXCL``, so a
planted file or symlink is never written through), is synced, and is renamed
over the destination, which replaces a link at the destination rather than
following it. The directory is synced afterwards.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


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


__all__ = ["fsync_directory", "write_atomically", "write_temporary"]
