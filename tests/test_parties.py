"""The parties module, in memory and on PostgreSQL."""

from uuid import uuid7

import pytest
from pydantic import ValidationError

from remit import parties
from remit.kernel import Actor, ActorKind, Kernel, NotFound, Rejected, Store, Unauthorised
from remit.parties import (
    Address,
    AmendParty,
    ArchiveParty,
    Contact,
    Party,
    PartyKind,
    RegisterParty,
    RestoreParty,
)

CLERK = Actor(id="clerk", kind=ActorKind.HUMAN, permissions=frozenset({"parties.*"}))
NAIROBI = Address(label="Delivery", lines=("University Way", "Nairobi"), country="KE")
LAB_MANAGER = Contact(name="Wanjiru Kamau", role="Lab manager", email="w.kamau@example.ac.ke")


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return Kernel([parties.module], store=store)


def register(kernel: Kernel, code: str = "C0001", **details: object) -> Party:
    command = RegisterParty.model_validate(
        {"code": code, "kind": PartyKind.ORGANISATION, "name": "Highland University", **details}
    )
    return kernel.execute(command, actor=CLERK).result


def amend(kernel: Kernel, party: Party, **details: object) -> Party:
    current = {
        field: getattr(party, field) for field in ("name", "tax_number", "addresses", "contacts")
    }
    command = AmendParty.model_validate(
        {"party_id": party.id, "version": party.version, **current, **details}
    )
    return kernel.execute(command, actor=CLERK).result


def test_a_party_is_registered_with_its_addresses_and_contacts(kernel: Kernel) -> None:
    outcome = kernel.execute(
        RegisterParty(
            code="C0001",
            kind=PartyKind.ORGANISATION,
            name="  Highland University ",
            tax_number="KE-P051234567X",
            addresses=(NAIROBI,),
            contacts=(LAB_MANAGER,),
        ),
        actor=CLERK,
    )

    registered = outcome.result
    assert (registered.state, registered.name) == ("active", "Highland University")
    assert parties.party(kernel, "C0001") == registered
    assert kernel.get(Party, registered.id).contacts == (LAB_MANAGER,)
    assert outcome.events == (parties.PartyRegistered(party_id=registered.id, code="C0001"),)


def test_codes_are_never_shared_even_with_an_archived_party(kernel: Kernel) -> None:
    first = register(kernel)
    kernel.execute(ArchiveParty(party_id=first.id), actor=CLERK)

    with pytest.raises(Rejected, match="party code 'C0001' is already in use"):
        register(kernel, name="Another University")
    assert len(kernel.find(Party)) == 1


@pytest.mark.parametrize(
    "details",
    [
        {"code": "c0001"},
        {"code": "C 0001"},
        {"code": "-C0001"},
        {"code": "C" * 33},
        {"name": "   "},
        {"kind": "company"},
        {"contacts": [{"name": "Wanjiru", "email": "not an address"}]},
        {"addresses": [{"label": "Delivery", "lines": ["Nairobi"], "country": "Kenya"}]},
        {"addresses": [{"label": "Delivery", "lines": [], "country": "KE"}]},
        {"addresses": [{"label": "Delivery", "lines": ["London"], "country": "UK"}]},
        {"addresses": [{"label": "Delivery", "lines": ["Nowhere"], "country": "ZZ"}]},
    ],
)
def test_details_that_cannot_be_right_are_refused(details: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RegisterParty.model_validate(
            {"code": "C0001", "kind": "organisation", "name": "Highland University", **details}
        )


def test_amending_replaces_the_details_and_says_what_changed(kernel: Kernel) -> None:
    party = register(kernel, tax_number="KE-P051234567X", contacts=(LAB_MANAGER,))

    outcome = kernel.execute(
        AmendParty(
            party_id=party.id,
            version=party.version,
            name="Highland University (HU)",
            tax_number=None,
            addresses=(),
            contacts=(),
        ),
        actor=CLERK,
    )

    amended = outcome.result
    assert (amended.name, amended.tax_number, amended.contacts) == (
        "Highland University (HU)",
        None,
        (),
    )
    assert (amended.code, amended.kind, amended.version) == (party.code, party.kind, 2)
    assert outcome.events == (
        parties.PartyAmended(party_id=party.id, changed=("name", "tax_number", "contacts")),
    )


def test_an_amendment_must_give_every_detail_so_none_is_cleared_by_omission() -> None:
    with pytest.raises(ValidationError) as refused:
        AmendParty.model_validate({"party_id": uuid7(), "version": 1, "name": "Renamed"})
    missing = {
        tuple(error["loc"]) for error in refused.value.errors() if error["type"] == "missing"
    }
    assert missing == {("tax_number",), ("addresses",), ("contacts",)}


def test_an_amendment_from_an_out_of_date_copy_is_refused(kernel: Kernel) -> None:
    party = register(kernel)
    amend(kernel, party, name="Highland University (HU)")

    with pytest.raises(Rejected, match="has changed since version 1"):
        amend(kernel, party, addresses=(NAIROBI,))
    assert parties.party(kernel, "C0001").addresses == ()


def test_an_amendment_that_changes_nothing_is_refused(kernel: Kernel) -> None:
    party = register(kernel)
    with pytest.raises(Rejected, match="nothing would change"):
        amend(kernel, party)


def test_the_code_and_kind_cannot_be_amended(kernel: Kernel) -> None:
    party = register(kernel)
    for fixed in ({"code": "C0002"}, {"kind": "person"}):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            amend(kernel, party, **fixed)


def test_an_archived_party_must_be_restored_before_it_is_amended(kernel: Kernel) -> None:
    party = register(kernel)
    archived = kernel.execute(ArchiveParty(party_id=party.id), actor=CLERK).result

    with pytest.raises(Rejected, match="archived; restore it before amending it"):
        amend(kernel, archived, name="Highland University (HU)")
    with pytest.raises(
        Rejected, match="cannot archive from state 'archived'; allowed here: restore"
    ):
        kernel.execute(ArchiveParty(party_id=party.id), actor=CLERK)

    restored = kernel.execute(RestoreParty(party_id=party.id), actor=CLERK).result
    assert amend(kernel, restored, name="Highland University (HU)").state == "active"
    assert [type(recorded.event) for recorded in kernel.events()] == [
        parties.PartyRegistered,
        parties.PartyArchived,
        parties.PartyRestored,
        parties.PartyAmended,
    ]


def test_looking_up_a_code_that_does_not_exist_says_so(kernel: Kernel) -> None:
    with pytest.raises(NotFound, match="no party with code 'C9999'"):
        parties.party(kernel, "C9999")


def test_registering_does_not_grant_amending(kernel: Kernel) -> None:
    party = register(kernel)
    registrar = Actor(
        id="registrar", kind=ActorKind.AGENT, permissions=frozenset({"parties.register_party"})
    )
    with pytest.raises(Unauthorised, match=r"parties\.amend_party"):
        kernel.execute(
            AmendParty(
                party_id=party.id,
                version=party.version,
                name="Mine now",
                tax_number=None,
                addresses=(),
                contacts=(),
            ),
            actor=registrar,
        )
