"""The parties module's documents and events.

A party records who someone is: their name, how to reach them, where they are. What
they are to the business (a customer's credit terms, a supplier's lead times, a donor's
Gift Aid declaration) belongs to the module that cares about it, so one organisation can
be a customer, a supplier and a donor without being recorded three times.
"""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from remit.kernel import Code, Country, Document, Event, Lifecycle, Module, Name, Transition

module = Module("parties")

Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
TaxNumber = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class PartyKind(StrEnum):
    ORGANISATION = "organisation"
    PERSON = "person"


class Address(BaseModel):
    """A postal address. Formats differ by country, so only the country is structured."""

    model_config = ConfigDict(frozen=True)

    label: Name
    """What the address is for, such as Billing, Delivery or Registered office."""
    lines: tuple[Line, ...] = Field(min_length=1, max_length=6)
    """The address as it is written on an envelope, without the country."""
    country: Country


class Contact(BaseModel):
    """Someone at the party, and how to reach them."""

    model_config = ConfigDict(frozen=True)

    name: Name
    role: Name | None = None
    """What they do, such as Accounts payable or Lab manager."""
    email: EmailStr | None = None
    phone: Line | None = None


@module.document
class Party(Document):
    """A person or organisation the business deals with.

    The code identifies the party to people and never changes or passes to another party.
    Archived parties keep their history, can be restored, and cannot be amended.
    """

    lifecycle = Lifecycle(
        initial="active",
        transitions=(
            Transition("archive", source="active", target="archived"),
            Transition("restore", source="archived", target="active"),
        ),
    )
    unique = ("code",)

    code: Code
    kind: PartyKind
    name: Name
    tax_number: TaxNumber | None = None
    """A VAT or other tax registration number, as issued."""
    addresses: tuple[Address, ...] = ()
    contacts: tuple[Contact, ...] = ()


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
