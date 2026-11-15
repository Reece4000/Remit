"""The items module, in memory and on PostgreSQL."""

import pytest
from pydantic import ValidationError

from remit import items
from remit.items import AddItem, AmendItem, DiscontinueItem, Item, ItemKind, ReinstateItem
from remit.kernel import Actor, ActorKind, Kernel, NotFound, Rejected, Store, unit_name

BUYER = Actor(id="buyer", kind=ActorKind.HUMAN, permissions=frozenset({"items.*"}))


@pytest.fixture
def kernel(store: Store) -> Kernel:
    return Kernel([items.module], store=store)


def add(kernel: Kernel, code: str = "BOLT-M8", **details: object) -> Item:
    command = AddItem.model_validate(
        {"code": code, "kind": ItemKind.GOODS, "name": "Hex bolt, M8 x 40", **details}
    )
    return kernel.execute(command, actor=BUYER).result


def test_an_item_is_added_in_its_unit(kernel: Kernel) -> None:
    outcome = kernel.execute(
        AddItem(code="WASHER-M8", kind=ItemKind.GOODS, name="Washers, M8", unit="XBX"),
        actor=BUYER,
    )

    added = outcome.result
    assert (added.state, added.unit, added.description) == ("active", "XBX", None)
    assert items.item(kernel, "WASHER-M8") == added
    assert outcome.events == (items.ItemAdded(item_id=added.id, code="WASHER-M8"),)


def test_units_default_to_each_and_services_are_items_too(kernel: Kernel) -> None:
    assert add(kernel).unit == "EA"
    calibration = add(kernel, "CAL", kind=ItemKind.SERVICE, name="Calibration", unit="HUR")
    assert (calibration.kind, calibration.unit, unit_name("HUR")) == (
        ItemKind.SERVICE,
        "HUR",
        "hour",
    )


def test_codes_are_never_shared_even_with_a_discontinued_item(kernel: Kernel) -> None:
    first = add(kernel)
    kernel.execute(DiscontinueItem(item_id=first.id), actor=BUYER)

    with pytest.raises(Rejected, match="item code 'BOLT-M8' is already in use"):
        add(kernel, name="Hex bolt, M8 x 40 (zinc)")


@pytest.mark.parametrize(
    "details",
    [
        {"code": "pip-200"},
        {"name": ""},
        {"kind": "digital"},
        {"unit": "each"},
        {"unit": "box of 96"},
        {"unit": "ea"},
        {"description": ""},
    ],
)
def test_details_that_cannot_be_right_are_refused(details: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AddItem.model_validate({"code": "BOLT-M8", "kind": "goods", "name": "Hex bolt", **details})


def test_amending_changes_the_name_and_description_only(kernel: Kernel) -> None:
    bolt = add(kernel)

    outcome = kernel.execute(
        AmendItem(
            item_id=bolt.id,
            version=bolt.version,
            name=bolt.name,
            description="Grade 8.8, zinc plated",
        ),
        actor=BUYER,
    )

    assert outcome.result.description == "Grade 8.8, zinc plated"
    assert outcome.events == (items.ItemAmended(item_id=bolt.id, changed=("description",)),)
    for fixed in ({"code": "BOLT-M10"}, {"kind": "service"}, {"unit": "XBX"}):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            AmendItem.model_validate(
                {
                    "item_id": bolt.id,
                    "version": 2,
                    "name": bolt.name,
                    "description": None,
                    **fixed,
                }
            )


def test_an_amendment_from_an_out_of_date_copy_is_refused(kernel: Kernel) -> None:
    bolt = add(kernel)
    kernel.execute(
        AmendItem(item_id=bolt.id, version=bolt.version, name="Hex bolt", description=None),
        actor=BUYER,
    )
    with pytest.raises(Rejected, match="has changed since version 1"):
        kernel.execute(
            AmendItem(item_id=bolt.id, version=bolt.version, name="Coach bolt", description=None),
            actor=BUYER,
        )


def test_an_amendment_that_changes_nothing_is_refused(kernel: Kernel) -> None:
    bolt = add(kernel)
    with pytest.raises(Rejected, match="nothing would change"):
        kernel.execute(
            AmendItem(item_id=bolt.id, version=bolt.version, name=bolt.name, description=None),
            actor=BUYER,
        )


def test_a_discontinued_item_must_be_reinstated_before_it_is_amended(kernel: Kernel) -> None:
    bolt = add(kernel)
    stopped = kernel.execute(DiscontinueItem(item_id=bolt.id), actor=BUYER).result

    with pytest.raises(Rejected, match="discontinued; reinstate it before amending it"):
        kernel.execute(
            AmendItem(item_id=bolt.id, version=stopped.version, name="Hex bolt", description=None),
            actor=BUYER,
        )

    reinstated = kernel.execute(ReinstateItem(item_id=bolt.id), actor=BUYER).result
    assert reinstated.state == "active"
    assert [type(recorded.event) for recorded in kernel.events()] == [
        items.ItemAdded,
        items.ItemDiscontinued,
        items.ItemReinstated,
    ]


def test_looking_up_a_code_that_does_not_exist_says_so(kernel: Kernel) -> None:
    with pytest.raises(NotFound, match="no item with code 'NOPE'"):
        items.item(kernel, "NOPE")
