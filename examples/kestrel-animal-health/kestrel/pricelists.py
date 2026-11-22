"""Manufacturers' price lists, and the history of each price.

A price is what one manufacturer charges for one item from a date. Loading a new list
records a price for each line that has changed, which supersedes the one before; a
product the manufacturer no longer offers has its price withdrawn. Nothing is
overwritten, so the history of a price is the list of its documents.

Price lists add to each item the manufacturer it is bought from when nobody names
another, which must have a current price for it. A quotation line that names no supplier
takes that manufacturer's price, and so does the purchase order raised for it.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from remit import items, parties
from remit.kernel import (
    Command,
    Context,
    Document,
    Event,
    Extension,
    Lifecycle,
    Module,
    Money,
    Name,
    Problem,
    Reader,
    References,
    Rejected,
    Transition,
)

from .common import Days

module = Module("pricelists", requires=(parties.module, items.module))

PriceId = Annotated[UUID, References("pricelists.price")]
Supplier = Annotated[UUID | None, References("parties.party")]


@module.document
class Price(Document):
    """What a manufacturer charges for an item, per unit, from a date."""

    lifecycle = Lifecycle(
        initial="current",
        transitions=(
            Transition("supersede", source="current", target="superseded"),
            Transition("withdraw", source="current", target="withdrawn"),
        ),
    )
    searchable = ("reference", "source")

    supplier_id: parties.PartyId
    """The manufacturer whose price it is."""
    item_id: items.ItemId
    """What it is the price of."""
    cost: Money
    """Per unit of the item, in the currency the manufacturer invoices in."""
    effective: date
    """The first day the price applies."""
    reference: Name | None = None
    """The manufacturer's own code for the item."""
    source: Name | None = None
    """Where the price came from: a price list, or an email asking for it."""
    lead_time_days: Days | None = None
    """How many days the manufacturer takes to supply the item once it is ordered."""


@module.extension
class ItemSourcing(Extension[items.Item]):
    """Which manufacturer an item is bought from when nobody names another."""

    supplier_id: Supplier = None
    """The manufacturer it is bought from unless another is named, on a quotation line and
    so on the purchase order raised for it. It must have a current price for the item."""


@module.event
class ItemSourcingAmended(Event):
    """The manufacturer an item is bought from by default was changed."""

    item_id: items.ItemId
    changed: tuple[str, ...]


@module.event
class PriceRecorded(Event):
    """A price was recorded. `supersedes` is the price it replaced, if there was one."""

    price_id: PriceId
    supersedes: Annotated[UUID | None, References("pricelists.price")] = None


@module.event
class PriceWithdrawn(Event):
    """A manufacturer stopped offering an item."""

    price_id: PriceId


class RecordPrice(Command[Price]):
    supplier_id: parties.PartyId
    item_id: items.ItemId
    cost: Money
    effective: date
    reference: Name | None = None
    source: Name | None = None
    lead_time_days: Days | None = None


class AmendItemSourcing(Command[ItemSourcing]):
    """Replace the manufacturer an item is bought from by default; send null for none."""

    amends = "item_id"
    item_id: items.ItemId
    version: int
    """The version of the item this was read at. Refused if it has changed since."""
    supplier_id: Supplier


class WithdrawPrice(Command[Price]):
    moves = "withdraw"
    price_id: PriceId


@module.command
def amend_item_sourcing(command: AmendItemSourcing, context: Context) -> ItemSourcing:
    """Change the manufacturer an item is bought from by default, which must have a current
    price for it."""
    held = context.extension(ItemSourcing, command.item_id, version=command.version)
    if command.supplier_id is not None and not current(
        context, command.item_id, command.supplier_id
    ):
        item = context.get(items.Item, command.item_id)
        supplier = context.get(parties.Party, command.supplier_id)
        said = f"names {supplier.code}, which has no current price for {item.code}"
        raise Rejected(f"supplier_id {said}", problems=(Problem(("supplier_id",), said),))
    amended, changed = context.revise(held, supplier_id=command.supplier_id)
    context.emit(ItemSourcingAmended(item_id=command.item_id, changed=changed))
    return amended


@module.command
def record_price(command: RecordPrice, context: Context) -> Price:
    """Record a manufacturer's price for an item, replacing its current one."""
    supplier = context.get(parties.Party, command.supplier_id)
    if supplier.state != "active":
        raise Rejected(f"party {supplier.code} is {supplier.state}")
    item = context.get(items.Item, command.item_id)
    if item.state != "active":
        raise Rejected(f"item {item.code} is {item.state}; reinstate it to price it")
    replaced = current(context, item.id, supplier.id)
    terms = (command.cost, command.reference, command.lead_time_days)
    if replaced and (replaced.cost, replaced.reference, replaced.lead_time_days) == terms:
        raise Rejected(
            f"{supplier.code} already charges {command.cost} for {item.code}, on these terms"
        )
    if replaced:
        context.transition(replaced, "supersede")
    price = context.create(Price.model_validate(command.model_dump()))
    context.emit(PriceRecorded(price_id=price.id, supersedes=replaced.id if replaced else None))
    return price


@module.command
def withdraw_price(command: WithdrawPrice, context: Context) -> Price:
    """Record that a manufacturer no longer offers an item. Its history is kept."""
    withdrawn = context.transition(context.get(Price, command.price_id), "withdraw")
    context.emit(PriceWithdrawn(price_id=withdrawn.id))
    return withdrawn


def current(reader: Reader, item_id: UUID, supplier_id: UUID) -> Price | None:
    """The price a manufacturer charges for an item now, if it offers it."""
    found = reader.find(Price, item_id=item_id, supplier_id=supplier_id, state="current")
    return found[0] if found else None


def offered(reader: Reader, item_id: UUID, supplier_id: UUID | None) -> Price:
    """The current price of an item from a manufacturer; when none is named, from the one
    it is bought from by default, or from the only one offering it.

    Refused when no manufacturer offers it, or when several do and none is named or kept
    as the default.
    """
    if supplier_id is not None:
        price = current(reader, item_id, supplier_id)
        if price is None:
            item = reader.get(items.Item, item_id)
            supplier = reader.get(parties.Party, supplier_id)
            raise Rejected(f"{supplier.code} has no current price for {item.code}; record one")
        return price
    prices = reader.find(Price, item_id=item_id, state="current")
    preferred = reader.extension(ItemSourcing, item_id).supplier_id
    chosen = [p for p in prices if p.supplier_id == preferred] or prices
    if len(chosen) == 1:
        return chosen[0]
    item = reader.get(items.Item, item_id)
    if not prices:
        raise Rejected(f"no manufacturer has a current price for {item.code}; record one")
    codes = ", ".join(reader.get(parties.Party, p.supplier_id).code for p in prices)
    raise Rejected(f"{codes} all offer {item.code}; name the supplier")
