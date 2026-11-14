"""The parties module's commands, and how to look a party up."""

from uuid import UUID

from remit.kernel import Code, Command, Context, Name, NotFound, Reader, Rejected

from .model import (
    Address,
    Contact,
    Party,
    PartyAmended,
    PartyArchived,
    PartyKind,
    PartyRegistered,
    PartyRestored,
    TaxNumber,
    module,
)

_AMENDABLE = ("name", "tax_number", "addresses", "contacts")


class RegisterParty(Command[Party]):
    code: Code
    kind: PartyKind
    name: Name
    tax_number: TaxNumber | None = None
    addresses: tuple[Address, ...] = ()
    contacts: tuple[Contact, ...] = ()


class AmendParty(Command[Party]):
    """Replace a party's details.

    Every field is required, so that nothing is cleared by being left out: send each one
    as it should now be, including an empty list or null to clear it.
    """

    party_id: UUID
    version: int
    """The version these details were read at. Refused if the party has changed since."""
    name: Name
    tax_number: TaxNumber | None
    addresses: tuple[Address, ...]
    contacts: tuple[Contact, ...]


class ArchiveParty(Command[Party]):
    party_id: UUID


class RestoreParty(Command[Party]):
    party_id: UUID


@module.command
def register_party(command: RegisterParty, context: Context) -> Party:
    """Register a person or organisation. Codes are unique and never reused."""
    party = context.create(Party.model_validate(command.model_dump()))
    context.emit(PartyRegistered(party_id=party.id, code=party.code))
    return party


@module.command
def amend_party(command: AmendParty, context: Context) -> Party:
    """Change a party's name, tax number, addresses or contacts. Its code and kind are fixed."""
    party = context.get(Party, command.party_id, version=command.version)
    if party.state == "archived":
        raise Rejected(f"party {party.code} is archived; restore it before amending it")
    changes = {field: getattr(command, field) for field in _AMENDABLE}
    changed = tuple(field for field, value in changes.items() if getattr(party, field) != value)
    if not changed:
        raise Rejected(f"nothing would change: party {party.code} already has these details")
    amended = context.update(party, **changes)
    context.emit(PartyAmended(party_id=party.id, changed=changed))
    return amended


@module.command
def archive_party(command: ArchiveParty, context: Context) -> Party:
    """Archive a party the business no longer deals with. Its history is kept."""
    archived = context.transition(context.get(Party, command.party_id), "archive")
    context.emit(PartyArchived(party_id=archived.id))
    return archived


@module.command
def restore_party(command: RestoreParty, context: Context) -> Party:
    """Make an archived party active again."""
    restored = context.transition(context.get(Party, command.party_id), "restore")
    context.emit(PartyRestored(party_id=restored.id))
    return restored


def party(reader: Reader, code: str) -> Party:
    """The party with this code, active or archived."""
    found = reader.find(Party, code=code)
    if not found:
        raise NotFound(f"there is no party with code {code!r}")
    return found[0]
