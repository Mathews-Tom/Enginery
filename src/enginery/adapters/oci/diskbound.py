"""Measure the two bind-mounted trees of a running worker against their disk bounds.

A worker writes into two host directories (its task-local workspace and its result directory).
A container cannot be given a capped store for a bind mount of a host path on Docker Desktop:
the engine VM cannot enforce a quota on a path that lives on the host. The bound is therefore
enforced by the host-side launcher, which measures both trees while the worker runs and kills
the container when either outgrows its cap; a per-file ``RLIMIT_FSIZE`` limits how far a single
write can run ahead of the measurement.

Measuring never follows a symlink and never leaves the tree: every entry is examined with
``lstat``, hard links are counted once, and the walk stops as soon as a cap is exceeded.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from enginery.domain.runner import ResourceBounds

_BLOCK_BYTES = 512
"""``st_blocks`` is always counted in 512-byte units."""


@dataclass(frozen=True, slots=True)
class TreeUsage:
    """What a tree occupies: allocated bytes and entries (inodes), and why a walk stopped."""

    bytes_used: int
    entries: int
    unreadable: str | None = None


def measure_tree(root: str, *, max_bytes: int, max_entries: int) -> TreeUsage:
    """Allocated bytes and entry count under ``root``, stopping once either cap is exceeded.

    A directory that cannot be read is reported in ``unreadable`` instead of being skipped: a
    tree that cannot be measured cannot be bounded.
    """
    total = 0
    entries = 0
    seen_links: set[tuple[int, int]] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                children = list(iterator)
        except OSError as error:
            return TreeUsage(total, entries, unreadable=f"{directory}: {error.strerror}")
        for child in children:
            try:
                info = child.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # removed while the walk ran
            except OSError as error:
                return TreeUsage(total, entries, unreadable=f"{child.path}: {error.strerror}")
            entries += 1
            if info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode):
                identity = (info.st_dev, info.st_ino)
                if identity in seen_links:
                    continue
                seen_links.add(identity)
            total += info.st_blocks * _BLOCK_BYTES
            if stat.S_ISDIR(info.st_mode):
                pending.append(child.path)
            if total > max_bytes or entries > max_entries:
                return TreeUsage(total, entries)
    return TreeUsage(total, entries)


@dataclass(frozen=True, slots=True)
class DiskBound:
    """The caps of one launch's workspace and result trees."""

    workspace: str
    out: str
    bounds: ResourceBounds

    def violation(self) -> str | None:
        """The way a tree exceeds its bound right now, or ``None`` while both are within it."""
        for role, path, max_bytes, max_entries in (
            (
                "workspace",
                self.workspace,
                self.bounds.workspace_bytes,
                self.bounds.workspace_inodes,
            ),
            ("result", self.out, self.bounds.out_bytes, self.bounds.out_inodes),
        ):
            usage = measure_tree(path, max_bytes=max_bytes, max_entries=max_entries)
            if usage.unreadable is not None:
                return f"the {role} directory has an unreadable entry, so it cannot be bounded"
            if usage.bytes_used > max_bytes:
                return f"the {role} directory exceeds its {max_bytes}-byte disk bound"
            if usage.entries > max_entries:
                return f"the {role} directory exceeds its {max_entries}-entry bound"
        return None
