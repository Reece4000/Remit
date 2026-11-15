"""The parties module's documents and events.

A party records who someone is. Its contacts and addresses are documents of their own,
so that two people can add contacts to the same party at once without colliding, and
so that one person's details can be erased without touching anyone else's.

What a party is to the business (a customer's credit terms, a supplier's lead times, a
donor's Gift Aid declaration) belongs to the module that cares about it, so one
organisation can be a customer, a supplier and a donor without being recorded three
times.

Names, tax numbers, email addresses, phone numbers and address lines are personal data
(see docs/personal-data.md). Events carry ids, codes and field names only.
"""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import EmailStr, Field, StringConstraints

from remit.kernel import (
    Code,
    Country,
    Document,
    Event,
    Lifecycle,
    Module,
    Name,
    Personal,
    Transition,
)

module = Module("parties")

Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
TaxNumber = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class PartyKind(StrEnum):
    ORGANISATION = "organisation"
    PERSON = "person"


@module.document
class Party(Document):
    """A person or organisation the business deals with.

    The code identifies the party to people and never changes or passes to another party.
    Archived parties keep their history, can be restored, and cannot be amended. A party
    who is a person can be erased, which erases their contacts and addresses too.
    """

    lifecycle = Lifecycle(
        initial="active",
        transitions=(
            Transition("archive", source="active", target="archived"),
            Transition("restore", source="archived", target="active"),
            Transition("erase", source="active", target="erased"),
            Transition("erase", source="archived", target="erased"),
        ),
    )
    unique = ("code",)

    code: Code
    kind: PartyKind
    name: Annotated[Name, Personal]
    tax_number: Annotated[TaxNumber | None, Personal] = None
    """A VAT or other tax registration number, as issued."""


_DETAIL = Lifecycle(
    initial="current",
    transitions=(
        Transition("remove", source="current", target="removed"),
        Transition("erase", source="current", target="erased"),
        Transition("erase", source="removed", target="erased"),
    ),
)


@module.document
class Contact(Document):
    """Someone at a party, and how to reach them. Removed contacts keep their history."""

    lifecycle = _DETAIL

    party_id: UUID
    name: Annotated[Name, Personal]
    role: Name | None = None
    """What they do, such as Accounts payable or Site manager."""
    email: Annotated[EmailStr | None, Personal] = None
    phone: Annotated[Line | None, Personal] = None


@module.document
class Address(Document):
    """A party's postal address. Formats differ by country, so only the country is structured."""

    lifecycle = _DETAIL

    party_id: UUID
    label: Name
    """What the address is for, such as Billing, Delivery or Registered office."""
    lines: Annotated[tuple[Line, ...], Personal, Field(min_length=1, max_length=6)]
    """The address as it is written on an envelope, without the country."""
    country: Country


@module.event
class PartyRegistered(Event):
    """A party was registered."""

    party_id: UUID
    code: str


@module.event
class PartyAmended(Event):
    """A party's details were changed. `changed` names the fields that differ."""

    party_id: UUID
    changed: tuple[str, ...]


@module.event
class PartyArchived(Event):
    """A party was archived."""

    party_id: UUID


@module.event
class PartyRestored(Event):
    """An archived party was made active again."""

    party_id: UUID


@module.event
class PartyErased(Event):
    """A person's personal data was erased, with their contacts and addresses."""

    party_id: UUID


@module.event
class ContactAdded(Event):
    """A contact was added to a party."""

    contact_id: UUID
    party_id: UUID


@module.event
class ContactAmended(Event):
    """A contact's details were changed. `changed` names the fields that differ."""

    contact_id: UUID
    changed: tuple[str, ...]


@module.event
class ContactRemoved(Event):
    """A contact was removed from a party."""

    contact_id: UUID


@module.event
class ContactErased(Event):
    """A contact's personal data was erased."""

    contact_id: UUID


@module.event
class AddressAdded(Event):
    """An address was added to a party."""

    address_id: UUID
    party_id: UUID


@module.event
class AddressAmended(Event):
    """An address was changed. `changed` names the fields that differ."""

    address_id: UUID
    changed: tuple[str, ...]


@module.event
class AddressRemoved(Event):
    """An address was removed from a party."""

    address_id: UUID


@module.event
class AddressErased(Event):
    """An address's personal data was erased."""

    address_id: UUID
