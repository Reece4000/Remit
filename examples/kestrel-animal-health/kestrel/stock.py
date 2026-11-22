"""Stock: what is held, batch by batch, and what it cost.

Vaccines and test kits carry the manufacturer's batch number and an expiry date, and
some must be kept chilled or frozen, so stock is held as batches. A batch is received
whole from a purchase, allocated to a shipment as it is packed, and issued when the
shipment leaves; expired stock is written off. Stock is valued at what it cost, in the
base currency, and issuing or writing it off posts that cost to the ledger.

What is available, and what is held is worth, are worked out here whenever a batch
changes, and kept on the batch, so that a listing can find the batches with stock free.

Stock adds to each item how it must be kept, how long it keeps, what a batch must come
with, and what a unit weighs and measures. Receiving reads it, for the storage and bin a
batch takes unless told otherwise and the expiry and certificate it must arrive with; so
does packing, which refuses a batch with less shelf life left than the item's customers
are promised.
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from remit import items, ledger, parties
from remit.kernel import (
    Code,
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
    References,
    Rejected,
    SingleLine,
    Text,
    Transition,
)

from .common import (
    Celsius,
    Centimetres,
    Days,
    Kilograms,
    Quantity,
    account,
    amendments,
    refuse,
)


class StockSettings(BaseModel):
    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    stock_account: Code = "1300"
    """The asset account stock is valued in."""
    cost_of_sales_account: Code = "5000"
    """Where the cost of stock that is shipped goes."""
    write_off_account: Code = "7900"
    """Where the cost of stock that is written off goes."""


module = Module(
    "stock", requires=(parties.module, items.module, ledger.module), settings=StockSettings
)

BatchId = Annotated[UUID, References("stock.batch")]
BatchNumber = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40), SingleLine
]
Maker = Annotated[UUID | None, References("parties.party")]
Held = Annotated[int, Field(ge=0)]
ShelfLife = Annotated[int, Field(ge=1, le=3650)]
"""How many days something keeps from when it was made."""


class Storage(StrEnum):
    AMBIENT = "ambient"
    """15 to 25 °C."""
    CHILLED = "chilled"
    """2 to 8 °C."""
    FROZEN = "frozen"
    """-20 °C or colder."""


@module.document
class Batch(Document):
    """One manufacturer's batch of an item, as received, and how much of it is left.

    `allocated` is the part of what is on hand packed for a shipment that has not yet
    left, and `available` the rest. A batch is used up when nothing is left, and written
    off when what is left can no longer be sold.
    """

    lifecycle = Lifecycle(
        initial="in_stock",
        transitions=(
            Transition("use_up", source="in_stock", target="used_up"),
            Transition("write_off", source="in_stock", target="written_off"),
        ),
    )
    unique = (("item_id", "batch"),)
    searchable = ("batch", "certificate", "location")

    item_id: items.ItemId
    """What the batch is of."""
    batch: BatchNumber
    """The manufacturer's batch or lot number."""
    supplier_id: Maker = None
    """The manufacturer it was bought from."""
    manufactured: date | None = None
    """When the manufacturer made it, as its certificate of analysis says."""
    expiry: date | None
    """The last day it may be used. None for goods that do not expire."""
    received: date | None = None
    """When it first arrived."""
    storage: Storage
    """How it must be kept."""
    location: Code | None = None
    """Where it is kept: a bin, such as CR1-A03 in the first cold room."""
    certificate: Name | None = None
    """The number of the manufacturer's certificate of analysis for the batch."""
    on_hand: Held
    """How many are held."""
    allocated: Held = 0
    """How many of those are packed for a shipment that has not yet left."""
    available: Held
    """How many are free to be packed: those on hand less those allocated."""
    unit_cost: Money
    """What one unit cost, in the base currency."""
    value: Money
    """What those on hand are worth at cost, in the base currency."""
    notes: Text | None = None
    """Anything the warehouse should know, such as a quarantine or a damaged box."""


@module.extension
class ItemStock(Extension[items.Item]):
    """How an item must be kept, how long it keeps, what each batch of it must arrive
    with, and what one unit weighs and measures packed."""

    searchable = ("bin",)

    storage: Storage = Storage.AMBIENT
    """How it must be kept: ambient, chilled or frozen. Batches are received so unless the
    receipt says otherwise."""
    temperature_min: Celsius | None = None
    """The coldest it may be kept at, in °C, as its label says."""
    temperature_max: Celsius | None = None
    """The warmest it may be kept at, in °C, as its label says."""
    do_not_freeze: bool = False
    """Ruined by freezing, as vaccines and most test kits are: never packed with goods
    shipped on dry ice."""
    light_sensitive: bool = False
    """Must be kept out of the light."""
    shelf_life_days: ShelfLife | None = None
    """How many days a batch keeps from manufacture. None for what does not expire."""
    minimum_shelf_life_days: Days | None = None
    """The fewest days a batch must have left before it expires to be packed for a
    customer, as tenders demand: a batch with less is refused."""
    expiry_tracked: bool = False
    """Every batch must be received with its expiry date."""
    certificate_required: bool = False
    """Every batch must be received with the manufacturer's certificate of analysis."""
    bin: Code | None = None
    """Where it is put away, such as CR1-A03 in the first cold room, unless a receipt
    names another bin."""
    net_weight: Kilograms | None = None
    """What one unit weighs without packing, in kilograms."""
    gross_weight: Kilograms | None = None
    """What one unit weighs packed for export, in kilograms."""
    length_cm: Annotated[Centimetres | None, Field(title="Length (cm)")] = None
    """How long one unit is packed, in centimetres."""
    width_cm: Annotated[Centimetres | None, Field(title="Width (cm)")] = None
    """How wide one unit is packed, in centimetres."""
    height_cm: Annotated[Centimetres | None, Field(title="Height (cm)")] = None
    """How tall one unit is packed, in centimetres."""


@module.event
class ItemStockAmended(Event):
    """How an item is kept, or what it weighs or measures, was changed. `changed` names
    the fields that differ."""

    item_id: items.ItemId
    changed: tuple[str, ...]


@module.event
class BatchReceived(Event):
    """Stock of a batch was received."""

    batch_id: BatchId
    quantity: int


@module.event
class BatchAmended(Event):
    """A batch's details were corrected. `changed` names the fields that differ."""

    batch_id: BatchId
    changed: tuple[str, ...]


@module.event
class StockAllocated(Event):
    """Stock of a batch was set aside for a shipment."""

    batch_id: BatchId
    quantity: int


@module.event
class StockReleased(Event):
    """Stock set aside for a shipment was put back."""

    batch_id: BatchId
    quantity: int


@module.event
class StockIssued(Event):
    """Allocated stock left with a shipment."""

    batch_id: BatchId
    quantity: int


@module.event
class BatchWrittenOff(Event):
    """What was left of a batch was written off."""

    batch_id: BatchId
    quantity: int


class ReceiveBatch(Command[Batch]):
    item_id: items.ItemId
    batch: BatchNumber
    expiry: date | None
    storage: Storage | None = None
    """How the item is kept, as stock records it, if left out."""
    quantity: Quantity
    unit_cost: Money
    """In the base currency."""
    supplier_id: Maker = None
    manufactured: date | None = None
    received: date | None = None
    """Today if left out."""
    location: Code | None = None
    """The item's bin, as stock records it, if left out."""
    certificate: Name | None = None


class AmendItemStock(Command[ItemStock]):
    """Replace how an item is kept, what each batch must arrive with, and what a unit
    weighs and measures. Every field is required; send null to clear one."""

    amends = "item_id"
    item_id: items.ItemId
    version: int
    """The version of the item these were read at. Refused if it has changed since."""
    storage: Storage
    temperature_min: Celsius | None
    temperature_max: Celsius | None
    do_not_freeze: bool
    light_sensitive: bool
    shelf_life_days: ShelfLife | None
    minimum_shelf_life_days: Days | None
    expiry_tracked: bool
    certificate_required: bool
    bin: Code | None
    net_weight: Kilograms | None
    gross_weight: Kilograms | None
    length_cm: Annotated[Centimetres | None, Field(title="Length (cm)")]
    width_cm: Annotated[Centimetres | None, Field(title="Width (cm)")]
    height_cm: Annotated[Centimetres | None, Field(title="Height (cm)")]


class AmendBatch(Command[Batch]):
    """Correct a batch's details from its paperwork. Every field is required; send null to
    clear one."""

    amends = "batch_id"
    batch_id: BatchId
    version: int
    """The version these details were read at. Refused if the batch has changed since."""
    manufactured: date | None
    expiry: date | None
    storage: Storage
    location: Code | None
    certificate: Name | None
    notes: Text | None


class AllocateStock(Command[Batch]):
    batch_id: BatchId
    quantity: Quantity


class ReleaseStock(Command[Batch]):
    batch_id: BatchId
    quantity: Quantity


class IssueStock(Command[Batch]):
    moves = "use_up"
    batch_id: BatchId
    quantity: Quantity
    on: date
    reference: str
    """What the stock left with, for the ledger: a waybill's code."""


class WriteOffBatch(Command[Batch]):
    moves = "write_off"
    batch_id: BatchId
    on: date | None = None
    """When the stock was written off. Today if left out."""


@module.command
def amend_item_stock(command: AmendItemStock, context: Context) -> ItemStock:
    """Change how an item is kept, how long it keeps, what each batch of it must arrive
    with, or what a unit weighs and measures.

    The coldest it may be kept at cannot be above the warmest; what a batch must have
    left cannot be more than it keeps for; a unit cannot weigh less packed than unpacked;
    and something kept frozen cannot also be ruined by freezing.
    """
    held = context.extension(ItemStock, command.item_id, version=command.version)
    item = context.get(items.Item, command.item_id)
    if item.kind is not items.ItemKind.GOODS:
        raise Rejected(f"item {item.code} is a service, which is not kept in stock")
    _require_keepable(command)
    amended, changed = context.revise(held, **amendments(command))
    context.emit(ItemStockAmended(item_id=item.id, changed=changed))
    return amended


@module.command
def receive_batch(command: ReceiveBatch, context: Context) -> Batch:
    """Receive stock of a batch. A batch received again adds to what is held of it.

    It is kept and put away as the item is unless the receipt says otherwise, and an item
    tracked by expiry, or needing a certificate of analysis, must arrive with it.
    """
    item = context.get(items.Item, command.item_id)
    if item.kind is not items.ItemKind.GOODS:
        raise Rejected(f"item {item.code} is a service, which cannot be held")
    base = context.settings(ledger.LedgerSettings).base_currency
    if command.unit_cost.currency != base:
        raise Rejected(f"stock is valued in {base}, not {command.unit_cost.currency}")
    _require_in_order(command.manufactured, command.expiry)
    kept = context.extension(ItemStock, item.id)
    _require_paperwork(item, kept, command.expiry, command.certificate)
    storage = command.storage or kept.storage
    location = command.location or kept.bin
    found = context.find(Batch, item_id=item.id, batch=command.batch)
    if not found:
        batch = context.create(
            Batch(
                item_id=item.id,
                batch=command.batch,
                supplier_id=command.supplier_id,
                manufactured=command.manufactured,
                expiry=command.expiry,
                received=command.received or context.now.date(),
                storage=storage,
                location=location,
                certificate=command.certificate,
                unit_cost=command.unit_cost,
                **_holding(command.unit_cost, on_hand=command.quantity, allocated=0),
            )
        )
    else:
        (held,) = found
        same = (held.expiry, held.storage, held.unit_cost)
        if held.state != "in_stock" or same != (command.expiry, storage, command.unit_cost):
            raise Rejected(
                f"batch {command.batch} of {item.code} is already held as {held.state}, "
                f"expiring {held.expiry}, {held.storage}, at {held.unit_cost} each; "
                "a second receipt must match it"
            )
        on_hand = held.on_hand + command.quantity
        batch = context.update(held, **_holding(held.unit_cost, on_hand, held.allocated))
    context.emit(BatchReceived(batch_id=batch.id, quantity=command.quantity))
    return batch


@module.command
def amend_batch(command: AmendBatch, context: Context) -> Batch:
    """Correct a batch's dates, storage, bin, certificate or notes.

    An expiry that has passed is refused while some of the batch is packed: unpack it
    first, since expired stock cannot be shipped.
    """
    batch = context.get(Batch, command.batch_id, version=command.version)
    if batch.state != "in_stock":
        raise Rejected(f"batch {batch.batch} is {batch.state}; it can no longer change")
    _require_in_order(command.manufactured, command.expiry)
    if command.expiry is not None and command.expiry < context.now.date() and batch.allocated:
        raise Rejected(
            f"{batch.allocated} of batch {batch.batch} are packed for a shipment; unpack them "
            f"before giving it an expiry that has passed ({command.expiry})"
        )
    amended, changed = context.revise(batch, **amendments(command))
    context.emit(BatchAmended(batch_id=batch.id, changed=changed))
    return amended


@module.command
def allocate_stock(command: AllocateStock, context: Context) -> Batch:
    """Set stock of a batch aside for a shipment. Expired stock cannot be allocated, nor
    a batch with less shelf life left than the item must have when it is packed."""
    batch = _in_stock(context, command.batch_id)
    today = context.now.date()
    if batch.expiry is not None and batch.expiry < today:
        raise Rejected(f"batch {batch.batch} expired on {batch.expiry}")
    minimum = context.extension(ItemStock, batch.item_id).minimum_shelf_life_days
    if batch.expiry is not None and minimum is not None and (batch.expiry - today).days < minimum:
        item = context.get(items.Item, batch.item_id)
        raise Rejected(
            f"batch {batch.batch} expires on {batch.expiry}, {(batch.expiry - today).days} days "
            f"from now; {item.code} must have at least {minimum} days left when it is packed"
        )
    if command.quantity > batch.available:
        raise Rejected(
            f"batch {batch.batch} has {batch.available} free; {command.quantity} were asked for"
        )
    allocated = context.update(
        batch, **_holding(batch.unit_cost, batch.on_hand, batch.allocated + command.quantity)
    )
    context.emit(StockAllocated(batch_id=batch.id, quantity=command.quantity))
    return allocated


@module.command
def release_stock(command: ReleaseStock, context: Context) -> Batch:
    """Put back stock that was set aside for a shipment."""
    batch = _in_stock(context, command.batch_id)
    if command.quantity > batch.allocated:
        raise Rejected(f"batch {batch.batch} has only {batch.allocated} allocated")
    released = context.update(
        batch, **_holding(batch.unit_cost, batch.on_hand, batch.allocated - command.quantity)
    )
    context.emit(StockReleased(batch_id=batch.id, quantity=command.quantity))
    return released


@module.command
def issue_stock(command: IssueStock, context: Context) -> Batch:
    """Take allocated stock out of the books as it leaves, posting its cost to cost of sales."""
    batch = _in_stock(context, command.batch_id)
    if command.quantity > batch.allocated:
        raise Rejected(f"batch {batch.batch} has only {batch.allocated} allocated to issue")
    settings = context.settings(StockSettings)
    cost = batch.unit_cost.times(command.quantity)
    _post(
        context,
        command.on,
        f"{command.reference}: {command.quantity} of batch {batch.batch} shipped",
        debit=settings.cost_of_sales_account,
        credit=settings.stock_account,
        amount=cost,
    )
    left = batch.on_hand - command.quantity
    changes = _holding(batch.unit_cost, left, batch.allocated - command.quantity)
    issued = (
        context.transition(batch, "use_up", **changes)
        if left == 0
        else context.update(batch, **changes)
    )
    context.emit(StockIssued(batch_id=batch.id, quantity=command.quantity))
    return issued


@module.command
def write_off_batch(command: WriteOffBatch, context: Context) -> Batch:
    """Write off what is left of a batch, such as stock past its expiry date."""
    batch = _in_stock(context, command.batch_id)
    if batch.allocated:
        raise Rejected(f"{batch.allocated} of batch {batch.batch} is packed; unpack it first")
    settings = context.settings(StockSettings)
    _post(
        context,
        command.on or context.now.date(),
        f"Batch {batch.batch} written off: {batch.on_hand} units",
        debit=settings.write_off_account,
        credit=settings.stock_account,
        amount=batch.value,
    )
    written_off = context.transition(batch, "write_off", **_holding(batch.unit_cost, 0, 0))
    context.emit(BatchWrittenOff(batch_id=batch.id, quantity=batch.on_hand))
    return written_off


class _Holding(TypedDict):
    on_hand: int
    allocated: int
    available: int
    value: Money


def _holding(unit_cost: Money, on_hand: int, allocated: int) -> _Holding:
    """What a batch holds, with what is available and what it is worth worked out."""
    return {
        "on_hand": on_hand,
        "allocated": allocated,
        "available": on_hand - allocated,
        "value": unit_cost.times(on_hand),
    }


def _require_keepable(command: AmendItemStock) -> None:
    """Refuse details of how an item is kept that contradict one another."""
    problems: list[Problem] = []
    low, high = command.temperature_min, command.temperature_max
    if low is not None and high is not None and low > high:
        said = f"is above temperature_max, {high} °C"
        problems.append(Problem(("temperature_min",), said))
    if command.minimum_shelf_life_days is not None:
        life = command.shelf_life_days
        if life is None:
            said = "needs the shelf life it is part of"
            problems.append(Problem(("minimum_shelf_life_days",), said))
        elif command.minimum_shelf_life_days > life:
            said = f"is more than the shelf life of {life} days"
            problems.append(Problem(("minimum_shelf_life_days",), said))
    net, gross = command.net_weight, command.gross_weight
    if net is not None and gross is not None and gross < net:
        problems.append(Problem(("gross_weight",), f"is less than the net weight, {net} kg"))
    if command.storage is Storage.FROZEN and command.do_not_freeze:
        problems.append(Problem(("do_not_freeze",), "contradicts storage: it is kept frozen"))
    sizes = {"length_cm": command.length_cm, "width_cm": command.width_cm}
    sizes["height_cm"] = command.height_cm
    if None in sizes.values() and any(size is not None for size in sizes.values()):
        missing = [name for name, size in sizes.items() if size is None]
        problems += [Problem((name,), "is needed with the other dimensions") for name in missing]
    refuse(problems)


def _require_paperwork(
    item: items.Item, kept: ItemStock, expiry: date | None, certificate: str | None
) -> None:
    """Refuse a batch that arrives without the expiry or certificate its item needs."""
    problems: list[Problem] = []
    if kept.expiry_tracked and expiry is None:
        problems.append(Problem(("expiry",), f"is needed: {item.code} is tracked by expiry"))
    if kept.certificate_required and certificate is None:
        said = f"is needed: {item.code} needs a certificate of analysis with every batch"
        problems.append(Problem(("certificate",), said))
    refuse(problems)


def _require_in_order(manufactured: date | None, expiry: date | None) -> None:
    if manufactured is not None and expiry is not None and expiry <= manufactured:
        raise Rejected(f"a batch made on {manufactured} cannot expire on {expiry}, before it")


def _in_stock(context: Context, batch_id: UUID) -> Batch:
    batch = context.get(Batch, batch_id)
    if batch.state != "in_stock":
        raise Rejected(f"batch {batch.batch} is {batch.state}")
    return batch


def _post(
    context: Context, on: date, description: str, *, debit: str, credit: str, amount: Money
) -> None:
    """Move an amount in the base currency from one account to another."""
    if amount.is_zero:
        return
    context.execute(
        ledger.PostJournalEntry(
            posting_date=on,
            description=description,
            postings=(
                ledger.debit(account(context, debit), amount),
                ledger.credit(account(context, credit), amount),
            ),
        )
    )
