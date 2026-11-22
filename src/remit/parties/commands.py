"""The parties module's commands.

Parties and their details are read as any documents are. A party's current contacts are
`page(Contact, where={"party_id": party.id, "state": "current"})`; a party is found by
its code with `where={"code": "C0001"}`, or by what a person remembers of it with
`search="hollander"`.
"""

from typing import Annotated
from uuid import UUID

from pydantic import EmailStr, Field

from remit.kernel import (
    ERASED,
    Code,
    Command,
    Context,
    Country,
    Name,
    Personal,
    Rejected,
)

from .model import (
    Address,
    AddressAdded,
    AddressAmended,
    AddressErased,
    AddressId,
    AddressRemoved,
    Contact,
    ContactAdded,
    ContactAmended,
    ContactErased,
    ContactId,
    ContactRemoved,
    Line,
    Party,
    PartyAmended,
    PartyArchived,
    PartyErased,
    PartyId,
    PartyKind,
    PartyRegistered,
    PartyRestored,
    TaxNumber,
    module,
)

Lines = Annotated[tuple[Line, ...], Personal, Field(min_length=1, max_length=6)]


class RegisterParty(Command[Party]):
    code: Code
    kind: PartyKind
    name: Annotated[Name, Personal]
    tax_number: Annotated[TaxNumber | None, Personal] = None


class AmendParty(Command[Party]):
    """Replace a party's name and tax number.

    Both are required, so that nothing is cleared by being left out: send null to clear
    the tax number.
    """

    amends = "party_id"
    party_id: PartyId
    version: int
    """The version these details were read at. Refused if the party has changed since."""
    name: Annotated[Name, Personal]
    tax_number: Annotated[TaxNumber | None, Personal]


class ArchiveParty(Command[Party]):
    moves = "archive"
    party_id: PartyId


class RestoreParty(Command[Party]):
    moves = "restore"
    party_id: PartyId


class EraseParty(Command[Party]):
    moves = "erase"
    party_id: PartyId


class AddContact(Command[Contact]):
    party_id: PartyId
    name: Annotated[Name, Personal]
    role: Name | None = None
    email: Annotated[EmailStr | None, Personal] = None
    phone: Annotated[Line | None, Personal] = None


class AmendContact(Command[Contact]):
    """Replace a contact's details. Every field is required; send null to clear one."""

    amends = "contact_id"
    contact_id: ContactId
    version: int
    """The version these details were read at. Refused if the contact has changed since."""
    name: Annotated[Name, Personal]
    role: Name | None
    email: Annotated[EmailStr | None, Personal]
    phone: Annotated[Line | None, Personal]


class RemoveContact(Command[Contact]):
    moves = "remove"
    contact_id: ContactId


class EraseContact(Command[Contact]):
    moves = "erase"
    contact_id: ContactId


class AddAddress(Command[Address]):
    party_id: PartyId
    label: Name
    lines: Lines
    country: Country


class AmendAddress(Command[Address]):
    """Replace an address. Every field is required."""

    amends = "address_id"
    address_id: AddressId
    version: int
    """The version this address was read at. Refused if it has changed since."""
    label: Name
    lines: Lines
    country: Country


class RemoveAddress(Command[Address]):
    moves = "remove"
    address_id: AddressId


class EraseAddress(Command[Address]):
    moves = "erase"
    address_id: AddressId


@module.command
def register_party(command: RegisterParty, context: Context) -> Party:
    """Register a person or organisation. Codes are unique and never reused."""
    party = context.create(Party.model_validate(command.model_dump()))
    context.emit(PartyRegistered(party_id=party.id, code=party.code))
    return party


@module.command
def amend_party(command: AmendParty, context: Context) -> Party:
    """Change a party's name or tax number. Its code and kind are fixed."""
    party = _active(context, command.party_id, version=command.version)
    amended, changed = context.revise(party, name=command.name, tax_number=command.tax_number)
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


@module.command
def erase_party(command: EraseParty, context: Context) -> Party:
    """Erase a person's personal data, with all their contacts and addresses.

    Only a person can be erased: an organisation is not a data subject. Its people are
    its contacts, and each of them can be erased on their own.
    """
    party = context.get(Party, command.party_id)
    if party.kind is not PartyKind.PERSON:
        raise Rejected(
            f"party {party.code} is an organisation, not a person; erase its contacts instead"
        )
    for contact in context.find(Contact, party_id=party.id):
        if contact.state != "erased":
            _erase_contact(context, contact)
    for address in context.find(Address, party_id=party.id):
        if address.state != "erased":
            _erase_address(context, address)
    erased = context.transition(party, "erase", name=ERASED, tax_number=None)
    context.erase(erased)
    context.emit(PartyErased(party_id=party.id))
    return erased


@module.command
def add_contact(command: AddContact, context: Context) -> Contact:
    """Add someone at a party, and how to reach them."""
    _active(context, command.party_id)
    contact = context.create(Contact.model_validate(command.model_dump()))
    context.emit(ContactAdded(contact_id=contact.id, party_id=contact.party_id))
    return contact


@module.command
def amend_contact(command: AmendContact, context: Context) -> Contact:
    """Change a contact's name, role, email or phone."""
    contact = context.get(Contact, command.contact_id, version=command.version)
    _require_changeable(context, contact, "contact")
    amended, changed = context.revise(
        contact, name=command.name, role=command.role, email=command.email, phone=command.phone
    )
    context.emit(ContactAmended(contact_id=contact.id, changed=changed))
    return amended


@module.command
def remove_contact(command: RemoveContact, context: Context) -> Contact:
    """Stop using a contact. Its history is kept."""
    contact = context.get(Contact, command.contact_id)
    _require_changeable(context, contact, "contact")
    removed = context.transition(contact, "remove")
    context.emit(ContactRemoved(contact_id=removed.id))
    return removed


@module.command
def erase_contact(command: EraseContact, context: Context) -> Contact:
    """Erase a contact's personal data, here and in the audit trail."""
    return _erase_contact(context, context.get(Contact, command.contact_id))


@module.command
def add_address(command: AddAddress, context: Context) -> Address:
    """Add a postal address to a party."""
    _active(context, command.party_id)
    address = context.create(Address.model_validate(command.model_dump()))
    context.emit(AddressAdded(address_id=address.id, party_id=address.party_id))
    return address


@module.command
def amend_address(command: AmendAddress, context: Context) -> Address:
    """Change an address."""
    address = context.get(Address, command.address_id, version=command.version)
    _require_changeable(context, address, "address")
    amended, changed = context.revise(
        address, label=command.label, lines=command.lines, country=command.country
    )
    context.emit(AddressAmended(address_id=address.id, changed=changed))
    return amended


@module.command
def remove_address(command: RemoveAddress, context: Context) -> Address:
    """Stop using an address. Its history is kept."""
    address = context.get(Address, command.address_id)
    _require_changeable(context, address, "address")
    removed = context.transition(address, "remove")
    context.emit(AddressRemoved(address_id=removed.id))
    return removed


@module.command
def erase_address(command: EraseAddress, context: Context) -> Address:
    """Erase an address's personal data, here and in the audit trail."""
    return _erase_address(context, context.get(Address, command.address_id))


def _active(context: Context, party_id: UUID, *, version: int | None = None) -> Party:
    party = context.get(Party, party_id, version=version)
    if party.state != "active":
        raise Rejected(f"party {party.code} is {party.state}; it cannot be changed")
    return party


def _require_changeable(context: Context, detail: Contact | Address, what: str) -> None:
    """A contact or address changes only while it is current and its party is active.

    Erasure is the exception: it is always allowed, whatever the party's state.
    """
    _active(context, detail.party_id)
    if detail.state != "current":
        raise Rejected(f"this {what} is {detail.state}; it cannot be changed")


def _erase_contact(context: Context, contact: Contact) -> Contact:
    erased = context.transition(contact, "erase", name=ERASED, email=None, phone=None)
    context.erase(erased)
    context.emit(ContactErased(contact_id=erased.id))
    return erased


def _erase_address(context: Context, address: Address) -> Address:
    erased = context.transition(address, "erase", lines=(ERASED,))
    context.erase(erased)
    context.emit(AddressErased(address_id=erased.id))
    return erased
