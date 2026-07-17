from __future__ import annotations

import enum
from dataclasses import dataclass


class PrincipalType(enum.Enum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


@dataclass(frozen=True)
class AuthorityPrincipal:
    """An authenticated principal in the authority model."""

    id: str
    principal_type: PrincipalType
    role: str
    authorization_source: str
