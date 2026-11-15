"""Actors: who is issuing a command or reading.

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
    """What this actor may do, as patterns over permission names.

    A command's permission is its name (`ledger.post_journal_entry`). Reading a kind of
    document, or asking a query, takes `<module>.read.<name>` (`ledger.read.account`,
    `ledger.read.trial_balance`). So `ledger.*` grants a whole module, `ledger.read.*`
    grants reading it and changing nothing, and `*.read.*` grants reading everything,
    the audit trail (`kernel.read.audit`) and event log (`kernel.read.events`) included.
    """

    def may(self, permission: str) -> bool:
        return any(fnmatchcase(permission, pattern) for pattern in self.permissions)
