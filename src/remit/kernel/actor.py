"""Actors: who is issuing a command or reading.

People and agents are the same kind of thing to the kernel. Both hold permissions, both
are checked the same way, and both are named in the audit trail.

The system acts too: when it is set up, when a server signs someone in, and when a
module reacts to an event. Each time it acts under a name of its own, which its id is
made from (`system:setup`), so that what it did can say in words who did it.
"""

from collections.abc import Iterable
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

    @classmethod
    def system(cls, name: str, *, permissions: Iterable[str] = ()) -> Actor:
        """The system acting as `name`, such as `setup` or the module reacting to an
        event, under the id `system:<name>`."""
        return cls(id=f"{_SYSTEM}{name}", kind=ActorKind.SYSTEM, permissions=frozenset(permissions))


_SYSTEM = "system:"


def system_name(id: str) -> str | None:
    """What the system acting under this id is called, in words, from the name the id is
    made from: `Setup` for `system:setup`, `Kestrel stock` for `system:kestrel_stock`.
    None for an id that is not the system's."""
    name = id.removeprefix(_SYSTEM)
    if name == id or not name:
        return None
    return name.replace("_", " ").capitalize()
