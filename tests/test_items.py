"""The items module, in memory and on PostgreSQL."""

import pytest
from pydantic import ValidationError

from remit import items
from remit.items import AddItem, AmendItem, DiscontinueItem, Item, ItemKind, ReinstateItem
from remit.kernel import Actor, ActorKind, Kernel, NotFound, Rejected, Store

BUYER = Actor(id="buyer", kind=ActorKind.HUMAN, permissions=frozenset({"items.*"}))


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return Kernel([items.module], store=store)


def add(kernel: Kernel, code: str = "PIP-200", **details: object) -> Item:
    command = AddItem.model_validate(
        {"code": code, "kind": ItemKind.GOODS, "name": "Pipette, 20-200 µl", **details}
    )
    return kernel.execute(command, actor=BUYER).result


def test_an_item_is_added_in_its_unit(kernel: Kernel) -> None:
    outcome = kernel.execute(
        AddItem(code="TIP-1000", kind=ItemKind.GOODS, name="Pipette tips", unit="box of 96"),
        actor=BUYER,
    )

    added = outcome.result
    assert (added.state, added.unit, added.description) == ("active", "box of 96", None)
    assert items.item(kernel, "TIP-1000") == added
    assert outcome.events == (items.ItemAdded(item_id=added.id, code="TIP-1000"),)


def test_units_default_to_each_and_services_are_items_too(kernel: Kernel) -> None:
    assert add(kernel).unit == "each"
    calibration = add(kernel, "CAL", kind=ItemKind.SERVICE, name="Calibration", unit="hour")
    assert (calibration.kind, calibration.unit) == (ItemKind.SERVICE, "hour")


def test_codes_are_never_shared_even_with_a_discontinued_item(kernel: Kernel) -> None:
    first = add(kernel)
    kernel.execute(DiscontinueItem(item_id=first.id), actor=BUYER)

    with pytest.raises(Rejected, match="item code 'PIP-200' is already in use"):
        add(kernel, name="Pipette, 20-200 µl (new model)")


@pytest.mark.parametrize(
    "details",
    [{"code": "pip-200"}, {"name": ""}, {"kind": "digital"}, {"unit": " "}, {"description": ""}],
)
def test_details_that_cannot_be_right_are_refused(details: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AddItem.model_validate({"code": "PIP-200", "kind": "goods", "name": "Pipette", **details})


def test_amending_changes_the_name_and_description_only(kernel: Kernel) -> None:
    pipette = add(kernel)

    outcome = kernel.execute(
        AmendItem(
            item_id=pipette.id,
            version=pipette.version,
            name=pipette.name,
            description="Single channel, adjustable volume",
        ),
        actor=BUYER,
    )

    assert outcome.result.description == "Single channel, adjustable volume"
    assert outcome.events == (items.ItemAmended(item_id=pipette.id, changed=("description",)),)
    for fixed in ({"code": "PIP-201"}, {"kind": "service"}, {"unit": "box of 10"}):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            AmendItem.model_validate(
                {
                    "item_id": pipette.id,
                    "version": 2,
                    "name": pipette.name,
                    "description": None,
                    **fixed,
                }
            )


def test_an_amendment_from_an_out_of_date_copy_is_refused(kernel: Kernel) -> None:
    pipette = add(kernel)
    kernel.execute(
        AmendItem(item_id=pipette.id, version=pipette.version, name="Pipette", description=None),
        actor=BUYER,
    )
    with pytest.raises(Rejected, match="has changed since version 1"):
        kernel.execute(
            AmendItem(
                item_id=pipette.id, version=pipette.version, name="Micropipette", description=None
            ),
            actor=BUYER,
        )


def test_an_amendment_that_changes_nothing_is_refused(kernel: Kernel) -> None:
    pipette = add(kernel)
    with pytest.raises(Rejected, match="nothing would change"):
        kernel.execute(
            AmendItem(
                item_id=pipette.id, version=pipette.version, name=pipette.name, description=None
            ),
            actor=BUYER,
        )


def test_a_discontinued_item_must_be_reinstated_before_it_is_amended(kernel: Kernel) -> None:
    pipette = add(kernel)
    stopped = kernel.execute(DiscontinueItem(item_id=pipette.id), actor=BUYER).result

    with pytest.raises(Rejected, match="discontinued; reinstate it before amending it"):
        kernel.execute(
            AmendItem(
                item_id=pipette.id, version=stopped.version, name="Pipette", description=None
            ),
            actor=BUYER,
        )

    reinstated = kernel.execute(ReinstateItem(item_id=pipette.id), actor=BUYER).result
    assert reinstated.state == "active"
    assert [type(recorded.event) for recorded in kernel.events()] == [
        items.ItemAdded,
        items.ItemDiscontinued,
        items.ItemReinstated,
    ]


def test_looking_up_a_code_that_does_not_exist_says_so(kernel: Kernel) -> None:
    with pytest.raises(NotFound, match="no item with code 'NOPE'"):
        items.item(kernel, "NOPE")
