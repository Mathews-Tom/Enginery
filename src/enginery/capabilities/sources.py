"""Typed capability sources and their one deterministic precedence.

Every source -- the reviewed project, a user-local directory, a Git
repository pinned to a commit, and Armory -- reaches the resolver through the
same :class:`~enginery.application.delivery_ports.CapabilitySourcePort` and
yields the same manifest-declared permissions, so no source gets different
permission typing. What differs is only *trust*, and trust follows the
source's kind, never a package's own claim:

* ``project``: packages in the reviewed repository base revision, read with git
  plumbing from one pinned commit (never a working tree). This is the
  only source that resolves as ``local_trusted`` and not run-introduced.
* ``user``: a user-local directory; run-introduced and unauthenticated.
* ``git``: a repository URL pinned to an immutable commit sha;
  run-introduced and unauthenticated unless a pinned signature verifies.
* ``armory``: the Armory catalog at one commit; run-introduced and
  unauthenticated unless a pinned signature verifies.

Precedence, highest first, is ``project``, ``user``, ``git``, ``armory``;
within a kind, the order sources were registered in. The first source that
offers a requested ``name@version`` is selected; every lower-precedence
source that also offers it is *shadowed*, and is reported rather than
silently dropped.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass

from enginery.application.delivery_ports import CapabilityDescriptor, CapabilitySourcePort
from enginery.domain.errors import InvalidInputError


class SourceKind(enum.StrEnum):
    PROJECT = "project"
    USER = "user"
    GIT = "git"
    ARMORY = "armory"


SOURCE_PRECEDENCE: tuple[SourceKind, ...] = (
    SourceKind.PROJECT,
    SourceKind.USER,
    SourceKind.GIT,
    SourceKind.ARMORY,
)


_PINNED_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class CapabilitySource:
    """A source port registered with its kind and a human-readable label."""

    kind: SourceKind
    label: str
    port: CapabilitySourcePort

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise InvalidInputError("capability source label must be non-blank")
        if self.kind is SourceKind.PROJECT and not _PINNED_COMMIT.fullmatch(
            str(getattr(self.port, "pinned_commit", ""))
        ):
            raise InvalidInputError(
                "a project source must be a Git source pinned to a full 40-hex commit; a "
                "working directory is never a reviewed base"
            )

    @property
    def reviewed_base(self) -> bool:
        """Only the project source is the reviewed base revision."""

        return self.kind is SourceKind.PROJECT


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """One source's offer of a requested package.

    A shadowed alternative whose closure cannot be locked carries ``problem``
    instead of a descriptor: it is reported, never silently dropped. A
    selected candidate always has a descriptor.
    """

    kind: SourceKind
    label: str
    descriptor: CapabilityDescriptor | None = None
    problem: str | None = None

    def __post_init__(self) -> None:
        if (self.descriptor is None) == (self.problem is None):
            raise InvalidInputError(
                "a source candidate has exactly one of a descriptor or a problem"
            )

    def to_json(self) -> dict[str, object]:
        document: dict[str, object] = {"kind": self.kind.value, "label": self.label}
        if self.descriptor is None:
            document["problem"] = self.problem
        else:
            document["revision"] = self.descriptor.revision
            document["closure_digest"] = str(self.descriptor.digest)
        return document


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """The selected source for a package and every lower-precedence alternative."""

    selected: SourceCandidate
    shadowed: tuple[SourceCandidate, ...] = ()

    def to_json(self) -> dict[str, object]:
        selected = self.selected.descriptor
        return {
            "selected": self.selected.to_json(),
            "shadowed": [
                {
                    **candidate.to_json(),
                    "same_closure": candidate.descriptor is not None
                    and selected is not None
                    and candidate.descriptor.digest == selected.digest,
                }
                for candidate in self.shadowed
            ],
        }


def order_sources(sources: Sequence[CapabilitySource]) -> tuple[CapabilitySource, ...]:
    """Sources in precedence order (stable within a kind); at most one project source."""

    if sum(1 for source in sources if source.kind is SourceKind.PROJECT) > 1:
        raise InvalidInputError("at most one project (reviewed base) source may be configured")
    rank = {kind: index for index, kind in enumerate(SOURCE_PRECEDENCE)}
    return tuple(sorted(sources, key=lambda source: rank[source.kind]))


__all__ = [
    "SOURCE_PRECEDENCE",
    "CapabilitySource",
    "SourceCandidate",
    "SourceKind",
    "SourceSelection",
    "order_sources",
]
