"""The parties module, in memory and on PostgreSQL."""

from uuid import uuid7

import pytest
from conftest import ROOT, every
from pydantic import ValidationError

from remit import parties
from remit.kernel import (
    ERASED,
    Actor,
    ActorKind,
    Kernel,
    Rejected,
    Store,
    Unauthorised,
    said,
)
from remit.parties import (
    AddAddress,
    AddContact,
    AmendContact,
    AmendParty,
    ArchiveParty,
    Contact,
    EraseContact,
    EraseParty,
    Party,
    PartyKind,
    RegisterParty,
    RemoveContact,
    RestoreParty,
)

CLERK = Actor(id="clerk", kind=ActorKind.HUMAN, permissions=frozenset({"parties.*"}))


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return Kernel([parties.module], store=store)


def register(kernel: Kernel, code: str = "C0001", **details: object) -> Party:
    command = RegisterParty.model_validate(
        {"code": code, "kind": PartyKind.ORGANISATION, "name": "Harbour Cooperative", **details}
    )
    return kernel.execute(command, actor=CLERK).result


def add_contact(
    kernel: Kernel, party: Party, name: str = "Sanne de Vries", **details: object
) -> Contact:
    command = AddContact.model_validate({"party_id": party.id, "name": name, **details})
    return kernel.execute(command, actor=CLERK).result


def add_address(kernel: Kernel, party: Party, *lines: str, country: str = "NL") -> parties.Address:
    command = AddAddress(
        party_id=party.id,
        label="Delivery",
        lines=lines or ("Kade 12", "Rotterdam"),
        country=country,
    )
    return kernel.execute(command, actor=CLERK).result


def amend(kernel: Kernel, party: Party, **details: object) -> Party:
    current = {"name": party.name, "tax_number": party.tax_number}
    command = AmendParty.model_validate(
        {"party_id": party.id, "version": party.version, **current, **details}
    )
    return kernel.execute(command, actor=CLERK).result


def test_a_party_is_registered_and_found_by_code_or_kind(kernel: Kernel) -> None:
    outcome = kernel.execute(
        RegisterParty(
            code="C0001",
            kind=PartyKind.ORGANISATION,
            name="  Harbour Cooperative ",
            tax_number="NL123456789B01",
        ),
        actor=CLERK,
    )

    registered = outcome.result
    assert (registered.state, registered.name) == ("active", "Harbour Cooperative")
    assert every(kernel, Party, code="C0001") == [registered]
    assert every(kernel, Party, kind=PartyKind.ORGANISATION) == [registered]
    assert outcome.events == (parties.PartyRegistered(party_id=registered.id, code="C0001"),)


def test_codes_are_never_shared_even_with_an_archived_party(kernel: Kernel) -> None:
    first = register(kernel)
    kernel.execute(ArchiveParty(party_id=first.id), actor=CLERK)

    with pytest.raises(Rejected, match="party code 'C0001' is already in use"):
        register(kernel, name="Another Cooperative")
    assert len(every(kernel, Party)) == 1


@pytest.mark.parametrize(
    "details",
    [
        {"code": "c0001"},
        {"code": "C 0001"},
        {"code": "-C0001"},
        {"code": "C" * 33},
        {"name": "   "},
        {"kind": "company"},
        {"contacts": []},
    ],
)
def test_details_that_cannot_be_right_are_refused(details: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RegisterParty.model_validate(
            {"code": "C0001", "kind": "organisation", "name": "Harbour Cooperative", **details}
        )


def test_amending_replaces_the_details_and_says_what_changed(kernel: Kernel) -> None:
    party = register(kernel, tax_number="NL123456789B01")

    outcome = kernel.execute(
        AmendParty(party_id=party.id, version=party.version, name="Harbour Coop", tax_number=None),
        actor=CLERK,
    )

    amended = outcome.result
    assert (amended.name, amended.tax_number, amended.version) == ("Harbour Coop", None, 2)
    assert (amended.code, amended.kind) == (party.code, party.kind)
    assert outcome.events == (
        parties.PartyAmended(party_id=party.id, changed=("name", "tax_number")),
    )


def test_an_amendment_must_give_every_detail_so_none_is_cleared_by_omission() -> None:
    with pytest.raises(ValidationError) as refused:
        AmendParty.model_validate({"party_id": uuid7(), "version": 1, "name": "Renamed"})
    errors = refused.value.errors()
    assert {tuple(error["loc"]) for error in errors if error["type"] == "missing"} == {
        ("tax_number",)
    }


def test_an_amendment_from_an_out_of_date_copy_is_refused(kernel: Kernel) -> None:
    party = register(kernel)
    amend(kernel, party, name="Harbour Coop")

    with pytest.raises(Rejected, match="has changed since version 1"):
        amend(kernel, party, tax_number="NL123456789B01")
    assert kernel.get(Party, party.id, actor=ROOT).tax_number is None


def test_an_amendment_that_changes_nothing_is_refused(kernel: Kernel) -> None:
    with pytest.raises(Rejected, match="nothing would change"):
        amend(kernel, register(kernel))


def test_the_code_and_kind_cannot_be_amended(kernel: Kernel) -> None:
    party = register(kernel)
    for fixed in ({"code": "C0002"}, {"kind": "person"}):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            amend(kernel, party, **fixed)


def test_an_archived_party_must_be_restored_before_it_changes(kernel: Kernel) -> None:
    party = register(kernel)
    archived = kernel.execute(ArchiveParty(party_id=party.id), actor=CLERK).result

    with pytest.raises(Rejected, match="party C0001 is archived; it cannot be changed"):
        amend(kernel, archived, name="Harbour Coop")
    with pytest.raises(Rejected, match="party C0001 is archived"):
        add_contact(kernel, archived)
    with pytest.raises(Rejected, match="cannot archive from state 'archived'; allowed here: "):
        kernel.execute(ArchiveParty(party_id=party.id), actor=CLERK)

    restored = kernel.execute(RestoreParty(party_id=party.id), actor=CLERK).result
    assert amend(kernel, restored, name="Harbour Coop").state == "active"


def test_contacts_and_addresses_are_kept_beside_the_party(kernel: Kernel) -> None:
    party = register(kernel)
    sanne = add_contact(kernel, party, role="Purchasing", email="Sanne@Example.NL")
    pieter = add_contact(kernel, party, "Pieter Bakker")
    delivery = add_address(kernel, party)

    assert every(kernel, Contact, party_id=party.id, state="current") == [sanne, pieter]
    assert sanne.email == "Sanne@example.nl"
    assert every(kernel, parties.Address, party_id=party.id, state="current") == [delivery]
    assert kernel.get(Party, party.id, actor=ROOT).version == 1, (
        "adding details does not change the party"
    )

    kernel.execute(RemoveContact(contact_id=pieter.id), actor=CLERK)
    assert every(kernel, Contact, party_id=party.id, state="current") == [sanne]


def test_two_contacts_can_be_amended_from_the_same_reading_of_the_party(kernel: Kernel) -> None:
    party = register(kernel)
    sanne, pieter = add_contact(kernel, party), add_contact(kernel, party, "Pieter Bakker")

    for contact, email in ((sanne, "sanne@example.nl"), (pieter, "pieter@example.nl")):
        kernel.execute(
            AmendContact(
                contact_id=contact.id,
                version=contact.version,
                name=contact.name,
                role=None,
                email=email,
                phone=None,
            ),
            actor=CLERK,
        )
    assert [c.email for c in every(kernel, Contact, party_id=party.id, state="current")] == [
        "sanne@example.nl",
        "pieter@example.nl",
    ]


@pytest.mark.parametrize(
    ("lines", "country"),
    [(("Kade 12",), "UK"), (("Kade 12",), "ZZ"), (("Kade 12",), "nl"), ((), "NL")],
)
def test_addresses_need_lines_and_an_assigned_country_code(
    lines: tuple[str, ...], country: str
) -> None:
    with pytest.raises(ValidationError):
        AddAddress(party_id=uuid7(), label="Delivery", lines=lines, country=country)


def test_a_contact_needs_a_real_email_address() -> None:
    with pytest.raises(ValidationError) as raised:
        AddContact(party_id=uuid7(), name="Sanne", email="not an address")
    [found] = raised.value.errors()
    assert found["loc"] == ("email",)
    assert said(found).startswith("value is not a valid email address: ")


def test_erasing_a_contact_leaves_the_rest_of_the_party(kernel: Kernel) -> None:
    party = register(kernel)
    sanne = add_contact(kernel, party, role="Purchasing", email="sanne@example.nl")
    pieter = add_contact(kernel, party, "Pieter Bakker")

    outcome = kernel.execute(EraseContact(contact_id=sanne.id), actor=CLERK)

    erased = outcome.result
    assert (erased.state, erased.name, erased.email, erased.role) == (
        "erased",
        ERASED,
        None,
        "Purchasing",
    )
    assert outcome.events == (parties.ContactErased(contact_id=sanne.id),)
    assert every(kernel, Contact, party_id=party.id, state="current") == [pieter]
    added = [
        entry.input for entry in kernel.audit(actor=ROOT) if entry.action == "parties.add_contact"
    ]
    assert [entry["name"] for entry in added] == [ERASED, "Pieter Bakker"]
    with pytest.raises(Rejected, match="cannot erase from state 'erased'"):
        kernel.execute(EraseContact(contact_id=sanne.id), actor=CLERK)


def test_erasing_a_person_erases_their_contacts_and_addresses(kernel: Kernel) -> None:
    amy = register(kernel, "P0001", kind=PartyKind.PERSON, name="Amy Shaw", tax_number="AB123456C")
    add_contact(kernel, amy, "Amy Shaw", email="amy@example.com")
    add_address(kernel, amy, "1 Mill Lane", "York", country="GB")

    erased = kernel.execute(EraseParty(party_id=amy.id), actor=CLERK).result

    assert (erased.state, erased.code, erased.name, erased.tax_number) == (
        "erased",
        "P0001",
        ERASED,
        None,
    )
    assert [c.state for c in every(kernel, Contact, party_id=amy.id)] == ["erased"]
    assert [a.lines for a in every(kernel, parties.Address, party_id=amy.id)] == [(ERASED,)]


def test_parties_and_their_details_are_found_by_what_people_remember(kernel: Kernel) -> None:
    harbour = register(kernel, "C0001", name="Harbour Coöperative")
    register(kernel, "C0002", name="Hollander & Zoon")
    sanne = add_contact(kernel, harbour, email="sanne@harbour.example")
    add_contact(kernel, harbour, "Pieter Bakker")
    depot = add_address(kernel, harbour, "Kade 12", "3011 Rotterdam")

    def found(kind: type[Party | Contact | parties.Address], search: str) -> tuple[object, ...]:
        return kernel.page(kind, actor=CLERK, search=search).items

    assert found(Party, "cooperative harb") == (harbour,)
    assert [p.code for p in kernel.page(Party, actor=CLERK, search="c000").items] == [
        "C0001",
        "C0002",
    ]
    assert found(Contact, "VRIES") == (sanne,)
    assert found(Contact, "harbour.example") == (sanne,)
    assert found(parties.Address, "rotterdam kade") == (depot,)
    current = {"party_id": harbour.id, "state": "current"}
    assert [
        c.name for c in kernel.page(Contact, actor=CLERK, where=current, order="name").items
    ] == [
        "Pieter Bakker",
        "Sanne de Vries",
    ]


def test_an_erased_person_can_no_longer_be_searched_for(kernel: Kernel) -> None:
    amy = register(kernel, "P0001", kind=PartyKind.PERSON, name="Amy Shaw")
    add_contact(kernel, amy, "Amy Shaw", email="amy@example.com")
    add_address(kernel, amy, "1 Mill Lane", "York", country="GB")
    assert len(kernel.page(Party, actor=CLERK, search="shaw").items) == 1

    kernel.execute(EraseParty(party_id=amy.id), actor=CLERK)

    assert kernel.page(Party, actor=CLERK, search="shaw").items == ()
    assert kernel.page(Contact, actor=CLERK, search="amy").items == ()
    assert kernel.page(parties.Address, actor=CLERK, search="mill").items == ()
    assert [party.code for party in kernel.page(Party, actor=CLERK, search="p0001").items] == [
        "P0001"
    ], "the code is not personal, and still finds the record"
    kept = " ".join(str(entry.input) for entry in kernel.audit(actor=ROOT))
    for personal in ("Amy Shaw", "AB123456C", "amy@example.com", "Mill Lane"):
        assert personal not in kept


def test_an_organisation_is_not_a_person_to_erase(kernel: Kernel) -> None:
    party = register(kernel)
    with pytest.raises(Rejected, match="is an organisation, not a person; erase its contacts"):
        kernel.execute(EraseParty(party_id=party.id), actor=CLERK)


def test_registering_does_not_grant_amending(kernel: Kernel) -> None:
    party = register(kernel)
    registrar = Actor(
        id="registrar", kind=ActorKind.AGENT, permissions=frozenset({"parties.register_party"})
    )
    with pytest.raises(Unauthorised, match=r"parties\.amend_party"):
        kernel.execute(
            AmendParty(party_id=party.id, version=party.version, name="Mine", tax_number=None),
            actor=registrar,
        )


def test_addresses_are_amended_removed_and_erased_on_their_own(kernel: Kernel) -> None:
    party = register(kernel)
    delivery = add_address(kernel, party)
    billing = add_address(kernel, party, "Postbus 3", "Rotterdam")

    moved = kernel.execute(
        parties.AmendAddress(
            address_id=delivery.id,
            version=delivery.version,
            label="Delivery",
            lines=("Kade 14", "Rotterdam"),
            country="NL",
        ),
        actor=CLERK,
    )
    assert moved.events == (parties.AddressAmended(address_id=delivery.id, changed=("lines",)),)

    kernel.execute(parties.RemoveAddress(address_id=billing.id), actor=CLERK)
    erased = kernel.execute(parties.EraseAddress(address_id=billing.id), actor=CLERK).result

    assert (erased.state, erased.lines, erased.country) == ("erased", (ERASED,), "NL")
    assert every(kernel, parties.Address, party_id=party.id, state="current") == [moved.result]


def test_a_removed_contact_cannot_be_amended_but_can_be_erased(kernel: Kernel) -> None:
    party = register(kernel)
    sanne = add_contact(kernel, party)
    removed = kernel.execute(RemoveContact(contact_id=sanne.id), actor=CLERK).result

    with pytest.raises(Rejected, match="this contact is removed; it cannot be changed"):
        kernel.execute(
            AmendContact(
                contact_id=sanne.id,
                version=removed.version,
                name="Sanne",
                role=None,
                email=None,
                phone=None,
            ),
            actor=CLERK,
        )
    assert kernel.execute(EraseContact(contact_id=sanne.id), actor=CLERK).result.state == "erased"


def test_erasing_a_person_skips_details_already_erased(kernel: Kernel) -> None:
    amy = register(kernel, "P0001", kind=PartyKind.PERSON, name="Amy Shaw")
    contact = add_contact(kernel, amy, "Amy Shaw")
    address = add_address(kernel, amy, "1 Mill Lane", country="GB")
    kernel.execute(EraseContact(contact_id=contact.id), actor=CLERK)
    kernel.execute(parties.EraseAddress(address_id=address.id), actor=CLERK)

    assert kernel.execute(EraseParty(party_id=amy.id), actor=CLERK).result.state == "erased"


def test_the_details_of_an_archived_party_cannot_change_but_can_be_erased(kernel: Kernel) -> None:
    party = register(kernel)
    sanne = add_contact(kernel, party)
    delivery = add_address(kernel, party)
    kernel.execute(ArchiveParty(party_id=party.id), actor=CLERK)

    refusals = [
        AmendContact(
            contact_id=sanne.id,
            version=sanne.version,
            name="Sanne",
            role=None,
            email=None,
            phone=None,
        ),
        RemoveContact(contact_id=sanne.id),
        parties.AmendAddress(
            address_id=delivery.id,
            version=delivery.version,
            label="Delivery",
            lines=("Kade 14",),
            country="NL",
        ),
        parties.RemoveAddress(address_id=delivery.id),
    ]
    for command in refusals:
        with pytest.raises(Rejected, match="party C0001 is archived; it cannot be changed"):
            kernel.execute(command, actor=CLERK)
    assert kernel.execute(EraseContact(contact_id=sanne.id), actor=CLERK).result.state == "erased"
