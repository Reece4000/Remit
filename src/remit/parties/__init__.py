"""Parties: the people and organisations a business deals with.

A standard module, built only on the kernel's public API. Other modules record what a
party is to them (customer, supplier, donor) in their own documents, referring to the
party by id, and declare `requires=(parties.module,)`.
"""

from .commands import AmendParty, ArchiveParty, RegisterParty, RestoreParty, party
from .model import (
    Address,
    Contact,
    Party,
    PartyAmended,
    PartyArchived,
    PartyKind,
    PartyRegistered,
    PartyRestored,
    module,
)

__all__ = [
    "Address",
    "AmendParty",
    "ArchiveParty",
    "Contact",
    "Party",
    "PartyAmended",
    "PartyArchived",
    "PartyKind",
    "PartyRegistered",
    "PartyRestored",
    "RegisterParty",
    "RestoreParty",
    "module",
    "party",
]
