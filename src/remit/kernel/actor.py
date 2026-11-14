"""Actors: who is issuing a command.

People and agents are the same kind of thing to the kernel. Both hold permissions, both
are checked the same way, and both are named in the audit trail.
"""

from enum import StrEnum
from fnmatch import fnmatchcase

from pydantic import BaseModel, ConfigDict


class ActorKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class Actor(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: ActorKind
    permissions: frozenset[str] = frozenset()
    """Command names this actor may issue. `ledger.*` grants a whole module."""

    def may(self, permission: str) -> bool:
        return any(fnmatchcase(permission, pattern) for pattern in self.permissions)
