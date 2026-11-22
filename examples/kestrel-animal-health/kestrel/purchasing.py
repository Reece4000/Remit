"""Purchasing: buying from manufacturers what sales orders need, and receiving it.

Kestrel buys once it has won an order, so each purchase order line is raised for a sales
order line, and no more can be bought for one than was sold on it, rounded up to the
manufacturer's minimum order and whole packs; what that rounding adds stays in stock. A
purchase order may gather lines for several sales orders from one manufacturer, except
that an order a programme pays for is bought on purchase orders of its own, which carry
the programme's reference. A purchase order worth more than the approval limit, in the
base currency at the rate of its date, must be approved before it is sent. Once it is
sent, what was ordered is fixed, and the manufacturer confirms it with a reference of its
own.

Goods are received line by line, with the manufacturer's batch, its expiry date, the
temperature it arrived at and the paperwork that came with it; receiving them puts the
batch into stock and owes the manufacturer for it. A line's amount, what it still has to
come, and the order's totals are worked out here and kept on the documents.

Purchasing adds to each item how it is bought: the manufacturer's pack, its minimum
order, and whether it may be bought at all. It adds to each manufacturer the terms it
trades on, which a new purchase order takes unless told otherwise, and whether it is
approved to supply Kestrel: as good distribution practice requires of a wholesaler of
veterinary medicines, purchase orders go only to manufacturers whose approval holds.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from remit import access, items, ledger, parties
from remit.kernel import (
    Code,
    Command,
    Context,
    Currency,
    Document,
    Event,
    Extension,
    Lifecycle,
    Module,
    Money,
    Name,
    Personal,
    Problem,
    Reader,
    References,
    Rejected,
    Text,
    Transition,
    UnitCode,
)

from . import fx, pricelists, sales, stock
from .common import (
    Celsius,
    ContactRef,
    Counted,
    Days,
    Incoterm,
    LineNumber,
    MemberRef,
    Packages,
    PackSize,
    PaymentTerms,
    Percent,
    Quantity,
    ShipmentMethod,
    account,
    amendments,
    amount,
    next_code,
    next_line,
    refreshed,
    refuse,
    require_contact,
    require_currency,
)


class PurchasingSettings(BaseModel):
    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    approval_over: Money = Money.of("15000", "GBP")
    """A purchase order worth more than this, in the base currency, must be approved."""
    creditors_account: Code = "2100"
    """The liability account what is owed to manufacturers is held in."""
    carriage_account: Code = "5100"
    """Where manufacturers' delivery charges go, once everything on the order has arrived."""


module = Module(
    "purchasing",
    requires=(
        parties.module,
        items.module,
        ledger.module,
        access.module,
        fx.module,
        pricelists.module,
        sales.module,
        stock.module,
    ),
    settings=PurchasingSettings,
)

PurchaseOrderId = Annotated[UUID, References("purchasing.purchase_order")]
ForOrder = Annotated[UUID | None, References("sales.sales_order")]
PurchaseOrderLineId = Annotated[UUID, References("purchasing.purchase_order_line")]
GoodsReceiptId = Annotated[UUID, References("purchasing.goods_receipt")]

_TERMS = ("incoterm", "named_place", "payment_terms", "freight")
"""What a purchase order agrees with the manufacturer, fixed once it is approved or sent."""
_ORDERED = ("description", "quantity", "unit_cost", "discount_percent")
"""What a line orders, fixed once its purchase order is approved or sent."""


@module.extension
class ItemPurchasing(Extension[items.Item]):
    """How an item is bought: the manufacturer's pack, its minimum order, and whether it
    may be bought at all."""

    purchase_unit: UnitCode | None = None
    """The manufacturer's pack, such as a case (XCS), when it does not sell single units."""
    pack_size: PackSize = 1
    """How many of the item's unit come in the manufacturer's pack. Purchase orders buy
    whole packs, so quantities are rounded up to a multiple of it."""
    minimum_order: Quantity | None = None
    """The fewest the manufacturer supplies on one order, in the item's unit: a whole
    number of packs. Purchase orders buy at least this many."""
    blocked_for_purchase: bool = False
    """It may not be bought, such as while a recall or a quality complaint is open."""


@module.extension
class SupplierTerms(Extension[parties.Party]):
    """How Kestrel buys from a manufacturer: its currency and terms, how long it takes,
    and how orders reach it."""

    searchable = ("account_number",)

    supplier_currency: Currency | None = None
    """What it invoices in, and so what purchase orders to it are raised in."""
    supplier_payment_terms: PaymentTerms | None = None
    """When Kestrel pays its invoices."""
    supplier_incoterm: Incoterm | None = None
    """The Incoterm it delivers on."""
    supplier_named_place: Name | None = None
    """The place its Incoterm names, such as Kestrel's warehouse for DAP."""
    lead_time_days: Days | None = None
    """How many days it usually takes to deliver, which a purchase order expects unless
    told otherwise."""
    minimum_order_value: Money | None = None
    """The least it accepts on one order, in its currency, without charging for carriage."""
    account_number: Name | None = None
    """Kestrel's account number with it, which every purchase order quotes."""
    ordering_email: Annotated[EmailStr | None, Personal] = None
    """Where purchase orders are sent. Often a person's own address, so it is personal
    data, cleared if the manufacturer's record is erased."""


@module.extension
class SupplierApproval(Extension[parties.Party]):
    """Whether a manufacturer is approved to supply Kestrel, as good distribution practice
    requires of a wholesaler of veterinary medicines."""

    approved_until: date | None = None
    """The last day its approval holds, by when it must be reviewed. None while it is not
    approved, and no purchase order may be raised to it."""
    approval_reference: Name | None = None
    """The qualification the approval rests on, such as its audit report or its licence."""


@module.document
class PurchaseOrder(Document):
    """An order to a manufacturer. Lines are added while it is a draft."""

    lifecycle = Lifecycle(
        initial="draft",
        transitions=(
            Transition("approve", source="draft", target="approved"),
            Transition("send", source="draft", target="sent"),
            Transition("send", source="approved", target="sent"),
            Transition(
                "confirm", source="sent", target="confirmed", requires=("supplier_reference",)
            ),
            Transition("receive", source="sent", target="received"),
            Transition("receive", source="confirmed", target="received"),
            Transition("cancel", source="draft", target="cancelled"),
            Transition("cancel", source="approved", target="cancelled"),
        ),
    )
    unique = ("code",)
    searchable = ("code", "supplier_reference", "programme")

    code: Code
    """Kestrel's number for the purchase order, such as PO0042."""
    supplier_id: parties.PartyId
    """The manufacturer it is placed with."""
    contact_id: ContactRef = None
    """Who at the manufacturer the order goes to."""
    buyer_id: MemberRef = None
    """Who at Kestrel placed the order."""
    sales_order_id: ForOrder = None
    """The order it buys for, when it buys for one alone."""
    programme: Name | None = None
    """The reference of the programme paying for the order it buys for."""
    ordered: date
    """The order's date. Its total in the base currency is worked out at the rate of this
    day."""
    expected: date | None = None
    """When the goods are expected to arrive."""
    confirmed: date | None = None
    """When the manufacturer confirmed the order."""
    supplier_reference: Name | None = None
    """The manufacturer's reference for the order, from its confirmation."""
    currency: Currency
    """What the manufacturer invoices in."""
    incoterm: Incoterm = Incoterm.DAP
    """Who pays for carriage, and where the goods become Kestrel's."""
    named_place: Name | None = None
    """The place the Incoterm names: Kestrel's warehouse for DAP, say."""
    payment_terms: PaymentTerms = PaymentTerms.NET_30
    """When Kestrel will pay once invoiced."""
    shipment_method: ShipmentMethod = ShipmentMethod.ROAD
    """How the goods will travel to Kestrel."""
    notes: Text | None = None
    """Anything the manufacturer should read, printed on the order."""
    net: Money
    """What the lines come to, after their discounts."""
    freight: Money
    """What the manufacturer charges for delivery."""
    total: Money
    """What Kestrel will owe: the lines and the delivery charge."""
    total_base: Money
    """The total in the base currency, at the rate of the order's date: what the order is
    worth, which the approval limit is held to."""


@module.document
class PurchaseOrderLine(Document):
    """An item on a purchase order, bought for a line of a sales order, and how much of it
    has arrived."""

    lifecycle = Lifecycle(
        initial="open",
        transitions=(
            Transition("receive", source="open", target="received"),
            Transition("remove", source="open", target="removed"),
            Transition("cancel", source="open", target="cancelled"),
        ),
    )
    searchable = ("description", "supplier_item")
    part_of = "purchase_order_id"

    purchase_order_id: PurchaseOrderId
    """The purchase order the line is on."""
    line_number: LineNumber
    """Where the line comes on the purchase order."""
    sales_order_line_id: sales.SalesOrderLineId
    """The sales order line it buys for."""
    item_id: items.ItemId
    """What is bought."""
    description: Name
    """What the manufacturer reads: the order line's description unless it was changed."""
    supplier_item: Name | None = None
    """The manufacturer's own code for the item, from its price."""
    quantity: Quantity
    """How many are bought, in the item's unit."""
    unit: UnitCode
    """The item's unit, which the quantities count."""
    unit_cost: Money
    """Per unit, before the discount, in the purchase order's currency."""
    discount_percent: Percent = Decimal(0)
    """Taken off the manufacturer's price, in per cent."""
    amount: Money
    """What the line comes to: the quantity at the net unit cost, which is the unit cost
    less the discount, to the minor unit, as the manufacturer invoices it."""
    expected: date | None = None
    """When this line is expected to arrive."""
    received: Counted = 0
    """How many have arrived."""
    outstanding: Counted
    """How many are still to come."""
    notes: Text | None = None
    """Anything the manufacturer should read about this line."""


@module.document
class GoodsReceipt(Document):
    """Goods that arrived against a purchase order line, as one batch, and the paperwork
    that came with them. Fixed once recorded, as the stock and the ledger are."""

    lifecycle = Lifecycle(initial="received")
    unique = ("code",)
    searchable = ("code", "delivery_note", "certificate")

    code: Code
    """Kestrel's number for the receipt, such as GR0042."""
    purchase_order_id: PurchaseOrderId
    """The purchase order the goods came on."""
    purchase_order_line_id: PurchaseOrderLineId
    """The purchase order line they were received against."""
    supplier_id: parties.PartyId
    """The manufacturer who sent them."""
    item_id: items.ItemId
    """What arrived."""
    batch_id: stock.BatchId
    """The batch they went into stock as."""
    quantity: Quantity
    """How many arrived, in the item's unit."""
    received: date
    """The day they arrived."""
    temperature: Celsius | None = None
    """What the goods measured on arrival, in °C."""
    delivery_note: Name | None = None
    """The number on the delivery note that came with the goods."""
    carrier: Name | None = None
    """Who delivered them."""
    packages: Packages | None = None
    """How many packages arrived."""
    certificate: Name | None = None
    """The number of the certificate of analysis that came with the batch."""
    data_logger: Name | None = None
    """The serial number of the temperature logger that travelled with goods sent cold,
    whose record shows they stayed in range."""
    notes: Text | None = None
    """Anything found on arrival, such as a damaged box."""


@module.event
class ItemPurchasingAmended(Event):
    """How an item is bought was changed. `changed` names the fields that differ."""

    item_id: items.ItemId
    changed: tuple[str, ...]


@module.event
class SupplierTermsAmended(Event):
    """A manufacturer's terms were changed. `changed` names the fields that differ."""

    party_id: parties.PartyId
    changed: tuple[str, ...]


@module.event
class SupplierApproved(Event):
    """A manufacturer's approval to supply was given, renewed or withdrawn."""

    party_id: parties.PartyId
    approved_until: date | None


@module.event
class PurchaseOrderRaised(Event):
    """A purchase order was started."""

    purchase_order_id: PurchaseOrderId
    code: str


@module.event
class PurchaseOrderAmended(Event):
    """A purchase order's details were changed. `changed` names the fields that differ."""

    purchase_order_id: PurchaseOrderId
    changed: tuple[str, ...]


@module.event
class PurchaseOrderLineAdded(Event):
    """A line was added to a purchase order."""

    purchase_order_line_id: PurchaseOrderLineId
    purchase_order_id: PurchaseOrderId


@module.event
class PurchaseOrderLineAmended(Event):
    """A purchase order line was changed. `changed` names the fields that differ."""

    purchase_order_line_id: PurchaseOrderLineId
    changed: tuple[str, ...]


@module.event
class PurchaseOrderLineRemoved(Event):
    """A line was taken off a draft purchase order."""

    purchase_order_line_id: PurchaseOrderLineId


@module.event
class PurchaseOrderApproved(Event):
    """A purchase order over the approval limit was approved."""

    purchase_order_id: PurchaseOrderId


@module.event
class PurchaseOrderSent(Event):
    """A purchase order was sent to the manufacturer."""

    purchase_order_id: PurchaseOrderId


@module.event
class PurchaseOrderConfirmed(Event):
    """The manufacturer confirmed a purchase order."""

    purchase_order_id: PurchaseOrderId


@module.event
class PurchaseOrderCancelled(Event):
    """A purchase order was cancelled before it was sent."""

    purchase_order_id: PurchaseOrderId


@module.event
class GoodsReceived(Event):
    """Goods arrived against a purchase order line."""

    goods_receipt_id: GoodsReceiptId
    purchase_order_line_id: PurchaseOrderLineId
    quantity: int


class AmendItemPurchasing(Command[ItemPurchasing]):
    """Replace how an item is bought. Every field is required; send null to clear one."""

    amends = "item_id"
    item_id: items.ItemId
    version: int
    """The version of the item these were read at. Refused if it has changed since."""
    purchase_unit: UnitCode | None
    pack_size: PackSize
    minimum_order: Quantity | None
    blocked_for_purchase: bool


class AmendSupplierTerms(Command[SupplierTerms]):
    """Replace a manufacturer's terms. Every field is required; send null to clear one."""

    amends = "party_id"
    party_id: parties.PartyId
    version: int
    """The version of the party these were read at. Refused if it has changed since."""
    supplier_currency: Currency | None
    supplier_payment_terms: PaymentTerms | None
    supplier_incoterm: Incoterm | None
    supplier_named_place: Name | None
    lead_time_days: Days | None
    minimum_order_value: Money | None
    account_number: Name | None
    ordering_email: Annotated[EmailStr | None, Personal]


class ApproveSupplier(Command[SupplierApproval]):
    """Give, renew or withdraw a manufacturer's approval to supply. Send null for both
    fields to withdraw it."""

    amends = "party_id"
    party_id: parties.PartyId
    version: int
    """The version of the party this was read at. Refused if it has changed since."""
    approved_until: date | None
    approval_reference: Name | None


class RaisePurchaseOrder(Command[PurchaseOrder]):
    supplier_id: parties.PartyId
    currency: Currency | None = None
    """The manufacturer's, from its terms, if left out."""
    sales_order_id: ForOrder = None
    contact_id: ContactRef = None
    buyer_id: MemberRef = None
    ordered: date | None = None
    """Today if left out."""
    expected: date | None = None
    """The order's date and the manufacturer's lead time, from its terms, if left out."""
    incoterm: Incoterm | None = None
    """The manufacturer's, with its named place, from its terms, if left out; DAP if it has
    none."""
    named_place: Name | None = None
    payment_terms: PaymentTerms | None = None
    """The manufacturer's, from its terms, if left out; 30 days if it has none."""
    shipment_method: ShipmentMethod = ShipmentMethod.ROAD
    notes: Text | None = None
    freight: Money | None = None
    """No charge if left out."""
    code: Code | None = None
    """Numbered PO0001, PO0002 and on if left out."""


class AmendPurchaseOrder(Command[PurchaseOrder]):
    """Replace a purchase order's details. Every field is required; send null to clear one.

    Its Incoterm, named place, payment terms and freight are agreed with the manufacturer,
    so they change only while it is a draft. Its dates, references and notes can be
    corrected until everything has arrived.
    """

    amends = "purchase_order_id"
    purchase_order_id: PurchaseOrderId
    version: int
    """The version these details were read at. Refused if the order has changed since."""
    contact_id: ContactRef
    buyer_id: MemberRef
    expected: date | None
    supplier_reference: Name | None
    incoterm: Incoterm
    named_place: Name | None
    payment_terms: PaymentTerms
    shipment_method: ShipmentMethod
    notes: Text | None
    freight: Money


class AddPurchaseOrderLine(Command[PurchaseOrderLine]):
    purchase_order_id: PurchaseOrderId
    sales_order_line_id: Annotated[
        UUID,
        References(
            "sales.sales_order_line", where={"sales_order_id": "purchase_order_id.sales_order_id"}
        ),
    ]
    quantity: Quantity | None = None
    """All that is still to be bought for the order line if left out."""
    unit_cost: Money | None = None
    """The manufacturer's current price if left out."""
    discount_percent: Percent = Decimal(0)
    expected: date | None = None
    """The purchase order's expected date if left out."""
    notes: Text | None = None


class AmendPurchaseOrderLine(Command[PurchaseOrderLine]):
    """Replace a purchase order line. Every field is required; send null to clear one.

    What it orders changes only while the purchase order is a draft; when it is expected,
    and its notes, until it has arrived.
    """

    amends = "purchase_order_line_id"
    purchase_order_line_id: PurchaseOrderLineId
    version: int
    """The version this line was read at. Refused if it has changed since."""
    description: Name
    quantity: Quantity
    unit_cost: Money
    discount_percent: Percent
    expected: date | None
    notes: Text | None


class RemovePurchaseOrderLine(Command[PurchaseOrderLine]):
    moves = "remove"
    purchase_order_line_id: PurchaseOrderLineId


class ApprovePurchaseOrder(Command[PurchaseOrder]):
    moves = "approve"
    purchase_order_id: PurchaseOrderId


class SendPurchaseOrder(Command[PurchaseOrder]):
    moves = "send"
    purchase_order_id: PurchaseOrderId


class ConfirmPurchaseOrder(Command[PurchaseOrder]):
    moves = "confirm"
    purchase_order_id: PurchaseOrderId
    supplier_reference: Name
    expected: date | None = None
    """As the order had it if left out."""
    confirmed: date | None = None
    """Today if left out."""


class CancelPurchaseOrder(Command[PurchaseOrder]):
    moves = "cancel"
    purchase_order_id: PurchaseOrderId


class ReceiveGoods(Command[GoodsReceipt]):
    moves = "receive"
    purchase_order_line_id: PurchaseOrderLineId
    quantity: Quantity
    batch: stock.BatchNumber
    expiry: date | None
    storage: stock.Storage | None = None
    """How the item is kept, as stock records it, if left out."""
    temperature: Celsius | None = None
    """Needed for goods kept chilled or frozen."""
    received: date | None = None
    """Today if left out."""
    manufactured: date | None = None
    location: Code | None = None
    """The bin the batch is put away in."""
    certificate: Name | None = None
    delivery_note: Name | None = None
    carrier: Name | None = None
    packages: Packages | None = None
    data_logger: Name | None = None
    notes: Text | None = None


@module.command
def amend_item_purchasing(command: AmendItemPurchasing, context: Context) -> ItemPurchasing:
    """Change how an item is bought.

    A pack of more than one needs the unit it is bought in, and the minimum order is a
    whole number of packs.
    """
    held = context.extension(ItemPurchasing, command.item_id, version=command.version)
    problems: list[Problem] = []
    if command.pack_size > 1 and command.purchase_unit is None:
        problems.append(Problem(("purchase_unit",), f"is needed for a pack of {command.pack_size}"))
    minimum = command.minimum_order
    if minimum is not None and minimum % command.pack_size:
        said = f"must be a whole number of packs of {command.pack_size}"
        problems.append(Problem(("minimum_order",), said))
    refuse(problems)
    amended, changed = context.revise(held, **amendments(command))
    context.emit(ItemPurchasingAmended(item_id=command.item_id, changed=changed))
    return amended


@module.command
def amend_supplier_terms(command: AmendSupplierTerms, context: Context) -> SupplierTerms:
    """Change a manufacturer's terms.

    A named place needs the Incoterm that names it, and the minimum order value is in the
    manufacturer's currency.
    """
    held = context.extension(SupplierTerms, command.party_id, version=command.version)
    problems: list[Problem] = []
    if command.supplier_named_place is not None and command.supplier_incoterm is None:
        problems.append(Problem(("supplier_named_place",), "needs the Incoterm that names it"))
    least = command.minimum_order_value
    if least is not None and command.supplier_currency is None:
        said = "needs the manufacturer's currency, to be in"
        problems.append(Problem(("minimum_order_value",), said))
    elif least is not None and least.currency != command.supplier_currency:
        said = (
            f"is in {least.currency}, not the manufacturer's currency, {command.supplier_currency}"
        )
        problems.append(Problem(("minimum_order_value",), said))
    refuse(problems)
    amended, changed = context.revise(held, **amendments(command))
    context.emit(SupplierTermsAmended(party_id=command.party_id, changed=changed))
    return amended


@module.command
def approve_supplier(command: ApproveSupplier, context: Context) -> SupplierApproval:
    """Give, renew or withdraw a manufacturer's approval to supply. An approval needs the
    qualification it rests on."""
    held = context.extension(SupplierApproval, command.party_id, version=command.version)
    if (command.approved_until is None) != (command.approval_reference is None):
        field, said = (
            ("approval_reference", "is needed: give the qualification the approval rests on")
            if command.approval_reference is None
            else ("approved_until", "is needed with the qualification")
        )
        raise Rejected(f"{field} {said}", problems=(Problem((field,), said),))
    approved, _ = context.revise(held, **amendments(command))
    context.emit(SupplierApproved(party_id=command.party_id, approved_until=command.approved_until))
    return approved


@module.command
def raise_purchase_order(command: RaisePurchaseOrder, context: Context) -> PurchaseOrder:
    """Start a purchase order to an approved manufacturer, on its terms unless told
    otherwise. Lines are added to it next."""
    supplier = context.get(parties.Party, command.supplier_id)
    if supplier.state != "active":
        raise Rejected(f"party {supplier.code} is {supplier.state}")
    ordered = command.ordered or context.now.date()
    approved = context.extension(SupplierApproval, supplier.id).approved_until
    if approved is None:
        raise Rejected(f"{supplier.code} is not an approved supplier; approve it before ordering")
    if approved < ordered:
        raise Rejected(f"{supplier.code}'s approval lapsed on {approved}; renew it before ordering")
    require_contact(context, supplier.id, command.contact_id)
    terms = context.extension(SupplierTerms, supplier.id)
    currency = command.currency or terms.supplier_currency
    if currency is None:
        said = f"is needed: {supplier.code} has none in its terms"
        raise Rejected(f"currency {said}", problems=(Problem(("currency",), said),))
    incoterm, named_place = command.incoterm, command.named_place
    if incoterm is None:
        incoterm, named_place = (
            terms.supplier_incoterm or Incoterm.DAP,
            named_place or terms.supplier_named_place,
        )
    lead = terms.lead_time_days
    expected = command.expected or (ordered + timedelta(days=lead) if lead is not None else None)
    freight = command.freight or Money.zero(currency)
    require_currency(currency, freight=freight)
    programme = None
    if command.sales_order_id:
        programme = _open_order(context, command.sales_order_id).programme
    taken = {"code", "ordered", "freight", "currency", "expected", "incoterm", "named_place"}
    order = context.create(
        PurchaseOrder.model_validate(
            {
                **command.model_dump(exclude=taken | {"payment_terms"}),
                "code": command.code or next_code(context, PurchaseOrder, "PO"),
                "programme": programme,
                "ordered": ordered,
                "currency": currency,
                "expected": expected,
                "incoterm": incoterm,
                "named_place": named_place,
                "payment_terms": command.payment_terms
                or terms.supplier_payment_terms
                or PaymentTerms.NET_30,
                "freight": freight,
                **_totals(context, [], freight, ordered),
            }
        )
    )
    context.emit(PurchaseOrderRaised(purchase_order_id=order.id, code=order.code))
    return order


@module.command
def amend_purchase_order(command: AmendPurchaseOrder, context: Context) -> PurchaseOrder:
    """Change a purchase order's contact, buyer, dates, reference, terms or notes."""
    order = context.get(PurchaseOrder, command.purchase_order_id, version=command.version)
    if order.state in {"received", "cancelled"}:
        raise Rejected(f"purchase order {order.code} is {order.state}; it can no longer change")
    require_contact(context, order.supplier_id, command.contact_id)
    require_currency(order.currency, freight=command.freight)
    changes = amendments(command)
    agreed = [field for field in _TERMS if changes[field] != getattr(order, field)]
    if agreed and order.state != "draft":
        raise Rejected(
            f"purchase order {order.code} is {order.state}; what was agreed with the "
            f"manufacturer ({', '.join(agreed)}) changes only while it is a draft"
        )
    amended, changed = context.revise(order, **changes)
    totalled = _total_order(context, amended)
    context.emit(PurchaseOrderAmended(purchase_order_id=order.id, changed=changed))
    return totalled


@module.command
def add_purchase_order_line(command: AddPurchaseOrderLine, context: Context) -> PurchaseOrderLine:
    """Buy an item for a sales order line. No more can be bought than was sold, rounded up
    to the manufacturer's minimum order and whole packs, which is what is bought if no
    quantity is given. An item blocked for purchase is refused.

    A line for an order a programme pays for goes on a purchase order raised for that
    order, so that the programme's reference is on it.
    """
    order = _draft(context, command.purchase_order_id)
    wanted = context.get(sales.SalesOrderLine, command.sales_order_line_id)
    sales_order = _open_order(context, wanted.sales_order_id)
    if order.sales_order_id not in (None, sales_order.id):
        raise Rejected(f"purchase order {order.code} buys for another sales order")
    if sales_order.programme and order.sales_order_id != sales_order.id:
        raise Rejected(
            f"sales order {sales_order.code} is paid for by programme {sales_order.programme}, "
            f"whose reference must be on its purchase orders; raise one for {sales_order.code}"
        )
    buying = context.extension(ItemPurchasing, wanted.item_id)
    item = context.get(items.Item, wanted.item_id)
    if buying.blocked_for_purchase:
        raise Rejected(f"item {item.code} is blocked for purchase")
    bought = bought_for(context, wanted.id)
    most = to_order(buying, wanted.quantity - bought)
    quantity = command.quantity or most
    if not quantity or quantity > most:
        raise Rejected(
            f"{wanted.quantity} were sold on this line of {sales_order.code} and {bought} "
            f"are already on purchase orders; {_can_buy(most, wanted.quantity - bought)}, "
            f"not {quantity}"
        )
    _require_packs(item, buying, quantity)
    if command.unit_cost is None:
        price = pricelists.offered(context, wanted.item_id, order.supplier_id)
        unit_cost = price.cost
    else:
        price = pricelists.current(context, wanted.item_id, order.supplier_id)
        unit_cost = command.unit_cost
    require_currency(order.currency, unit_cost=unit_cost)
    charged = net_cost(unit_cost, command.discount_percent).times(quantity)
    numbers = [line.line_number for line in _all_lines(context, order.id)]
    line = context.create(
        PurchaseOrderLine(
            purchase_order_id=order.id,
            line_number=next_line(numbers),
            sales_order_line_id=wanted.id,
            item_id=wanted.item_id,
            description=wanted.description,
            supplier_item=price.reference if price else None,
            quantity=quantity,
            unit=wanted.unit,
            unit_cost=unit_cost,
            discount_percent=command.discount_percent,
            amount=charged,
            expected=command.expected or order.expected,
            outstanding=quantity,
            notes=command.notes,
        )
    )
    _total_order(context, order)
    context.emit(PurchaseOrderLineAdded(purchase_order_line_id=line.id, purchase_order_id=order.id))
    return line


@module.command
def amend_purchase_order_line(
    command: AmendPurchaseOrderLine, context: Context
) -> PurchaseOrderLine:
    """Change what a purchase order line orders, when it is expected, or its notes."""
    line = context.get(PurchaseOrderLine, command.purchase_order_line_id, version=command.version)
    order = context.get(PurchaseOrder, line.purchase_order_id)
    if line.state != "open":
        raise Rejected(
            f"line {line.line_number} of purchase order {order.code} is {line.state}; "
            "it can no longer change"
        )
    changes = amendments(command)
    ordered = [field for field in _ORDERED if changes[field] != getattr(line, field)]
    if ordered and order.state != "draft":
        raise Rejected(
            f"purchase order {order.code} is {order.state}; what its lines order "
            f"({', '.join(ordered)}) changes only while it is a draft"
        )
    require_currency(order.currency, unit_cost=command.unit_cost)
    if command.quantity != line.quantity:
        wanted = context.get(sales.SalesOrderLine, line.sales_order_line_id)
        buying = context.extension(ItemPurchasing, wanted.item_id)
        others = bought_for(context, wanted.id) - line.quantity
        most = to_order(buying, wanted.quantity - others)
        if command.quantity > most:
            raise Rejected(
                f"{wanted.quantity} were sold on that sales order line and {others} are on "
                f"other purchase orders; this line can buy {most}, not {command.quantity}"
            )
        _require_packs(context.get(items.Item, wanted.item_id), buying, command.quantity)
    amended, changed = context.revise(line, **changes)
    charged = net_cost(command.unit_cost, command.discount_percent).times(command.quantity)
    worked_out = refreshed(
        context,
        amended,
        {"amount": charged, "outstanding": command.quantity - line.received},
    )
    _total_order(context, order)
    context.emit(PurchaseOrderLineAmended(purchase_order_line_id=line.id, changed=changed))
    return worked_out


@module.command
def remove_purchase_order_line(
    command: RemovePurchaseOrderLine, context: Context
) -> PurchaseOrderLine:
    """Take a line off a draft purchase order. What it bought can be bought again."""
    line = context.get(PurchaseOrderLine, command.purchase_order_line_id)
    order = _draft(context, line.purchase_order_id)
    removed = context.transition(line, "remove")
    _total_order(context, order)
    context.emit(PurchaseOrderLineRemoved(purchase_order_line_id=removed.id))
    return removed


@module.command
def approve_purchase_order(command: ApprovePurchaseOrder, context: Context) -> PurchaseOrder:
    """Approve a purchase order over the approval limit to be sent."""
    order = context.get(PurchaseOrder, command.purchase_order_id)
    _require_lines(context, order)
    approved = context.transition(order, "approve")
    context.emit(PurchaseOrderApproved(purchase_order_id=order.id))
    return approved


@module.command
def send_purchase_order(command: SendPurchaseOrder, context: Context) -> PurchaseOrder:
    """Send a purchase order. Over the approval limit, it must be approved first."""
    order = context.get(PurchaseOrder, command.purchase_order_id)
    _require_lines(context, order)
    if order.state == "draft":
        limit = context.settings(PurchasingSettings).approval_over
        if order.total_base > limit:
            raise Rejected(
                f"purchase order {order.code} is worth {order.total_base}, over the {limit} limit; "
                "it must be approved before it is sent"
            )
    sent = context.transition(order, "send")
    context.emit(PurchaseOrderSent(purchase_order_id=order.id))
    return sent


@module.command
def confirm_purchase_order(command: ConfirmPurchaseOrder, context: Context) -> PurchaseOrder:
    """Record the manufacturer's confirmation of a sent purchase order, with its reference
    and when it expects to deliver."""
    order = context.get(PurchaseOrder, command.purchase_order_id)
    confirmed = context.transition(
        order,
        "confirm",
        supplier_reference=command.supplier_reference,
        expected=command.expected or order.expected,
        confirmed=command.confirmed or context.now.date(),
    )
    context.emit(PurchaseOrderConfirmed(purchase_order_id=order.id))
    return confirmed


@module.command
def cancel_purchase_order(command: CancelPurchaseOrder, context: Context) -> PurchaseOrder:
    """Cancel a purchase order that has not been sent. What it bought can be bought again."""
    order = context.get(PurchaseOrder, command.purchase_order_id)
    cancelled = context.transition(order, "cancel")
    for line in _lines(context, order.id):
        context.transition(line, "cancel")
    context.emit(PurchaseOrderCancelled(purchase_order_id=cancelled.id))
    return cancelled


@module.command
def receive_goods(command: ReceiveGoods, context: Context) -> GoodsReceipt:
    """Receive goods against a purchase order line, into stock as one batch.

    The manufacturer is owed the quantity at the line's net unit cost. The batch is
    valued at that unit cost in the base currency, at the rate on the day it arrived. When
    the last of the order arrives, its delivery charge is owed too.
    """
    line = context.get(PurchaseOrderLine, command.purchase_order_line_id)
    order = context.get(PurchaseOrder, line.purchase_order_id)
    if order.state not in {"sent", "confirmed"}:
        raise Rejected(f"purchase order {order.code} is {order.state}; goods arrive once sent")
    if command.quantity > line.outstanding:
        raise Rejected(f"{line.outstanding} are still to come on this line, not {command.quantity}")
    storage = command.storage or context.extension(stock.ItemStock, line.item_id).storage
    if storage is not stock.Storage.AMBIENT and command.temperature is None:
        raise Rejected(f"goods kept {storage} need the temperature they arrived at")
    received = command.received or context.now.date()
    net = net_cost(line.unit_cost, line.discount_percent)
    unit_cost = fx.in_base(context, net, received)
    batch = context.execute(
        stock.ReceiveBatch(
            item_id=line.item_id,
            batch=command.batch,
            expiry=command.expiry,
            storage=storage,
            quantity=command.quantity,
            unit_cost=unit_cost,
            supplier_id=order.supplier_id,
            manufactured=command.manufactured,
            received=received,
            location=command.location,
            certificate=command.certificate,
        )
    )
    settings = context.settings(PurchasingSettings)
    _owe(
        context,
        received,
        f"{order.code}: received {command.quantity} of batch {batch.batch}",
        context.settings(stock.StockSettings).stock_account,
        net.times(command.quantity),
        unit_cost.times(command.quantity),
    )
    total = line.received + command.quantity
    counted = {"received": total, "outstanding": line.quantity - total}
    if total == line.quantity:
        context.transition(line, "receive", **counted)
        if all(other.state == "received" for other in _lines(context, order.id)):
            context.transition(order, "receive")
            if not order.freight.is_zero:
                _owe(
                    context,
                    received,
                    f"{order.code}: delivery charge",
                    settings.carriage_account,
                    order.freight,
                    fx.in_base(context, order.freight, received),
                )
    else:
        context.update(line, **counted)
    receipt = context.create(
        GoodsReceipt(
            code=next_code(context, GoodsReceipt, "GR"),
            purchase_order_id=order.id,
            purchase_order_line_id=line.id,
            supplier_id=order.supplier_id,
            item_id=line.item_id,
            batch_id=batch.id,
            quantity=command.quantity,
            received=received,
            temperature=command.temperature,
            delivery_note=command.delivery_note,
            carrier=command.carrier,
            packages=command.packages,
            certificate=command.certificate,
            data_logger=command.data_logger,
            notes=command.notes,
        )
    )
    context.emit(
        GoodsReceived(
            goods_receipt_id=receipt.id, purchase_order_line_id=line.id, quantity=command.quantity
        )
    )
    return receipt


def to_order(buying: ItemPurchasing, needed: int) -> int:
    """The most a purchase order may buy to have `needed` more: at least the manufacturer's
    minimum, in whole packs. Nothing when nothing more is needed."""
    if needed <= 0:
        return 0
    wanted = max(needed, buying.minimum_order or 0)
    return -(-wanted // buying.pack_size) * buying.pack_size


def _can_buy(most: int, needed: int) -> str:
    if most == max(needed, 0):
        return f"{most} more can be bought"
    return f"{needed} more are needed, which the manufacturer's packs make {most}"


def _require_packs(item: items.Item, buying: ItemPurchasing, quantity: int) -> None:
    """Refuse a quantity the manufacturer does not supply: fewer than its minimum order,
    or not in whole packs."""
    problems: list[Problem] = []
    if quantity % buying.pack_size:
        said = f"must be whole packs of {buying.pack_size}, as {item.code} is bought"
        problems.append(Problem(("quantity",), said))
    if buying.minimum_order is not None and quantity < buying.minimum_order:
        said = f"is below the manufacturer's minimum order of {buying.minimum_order}"
        problems.append(Problem(("quantity",), said))
    refuse(problems)


def net_cost(unit_cost: Money, discount_percent: Decimal) -> Money:
    """A unit cost less a discount, to the minor unit: what each unit is invoiced at."""
    return amount(unit_cost, 1, discount_percent)


def bought_for(reader: Reader, sales_order_line_id: UUID) -> int:
    """How much is on purchase orders for a sales order line, not counting lines removed
    or cancelled."""
    found = reader.find(PurchaseOrderLine, sales_order_line_id=sales_order_line_id)
    return sum(line.quantity for line in found if line.state in {"open", "received"})


def _owe(
    context: Context, on: date, description: str, debit: str, owed: Money, base: Money
) -> None:
    """Owe the manufacturer an amount in its currency, debiting an account with what it
    comes to in the base currency. Stock is debited with what the batch is valued at."""
    creditors = context.settings(PurchasingSettings).creditors_account
    context.execute(
        ledger.PostJournalEntry(
            posting_date=on,
            description=description,
            postings=(
                ledger.debit(account(context, debit), base),
                ledger.credit(account(context, creditors), owed, base=base),
            ),
        )
    )


class _Totals(TypedDict):
    net: Money
    total: Money
    total_base: Money


def _totals(reader: Reader, lines: list[PurchaseOrderLine], freight: Money, on: date) -> _Totals:
    """A purchase order's totals from its lines and delivery charge, at a date's rate."""
    net = Money.total((line.amount for line in lines), freight.currency)
    total = net + freight
    base = reader.settings(ledger.LedgerSettings).base_currency
    worth = Money.zero(base) if total.is_zero else fx.in_base(reader, total, on)
    return {"net": net, "total": total, "total_base": worth}


def _total_order(context: Context, order: PurchaseOrder) -> PurchaseOrder:
    """Work a purchase order's totals out again from its lines."""
    totals = _totals(context, _lines(context, order.id), order.freight, order.ordered)
    return refreshed(context, order, totals)


def _all_lines(reader: Reader, order_id: UUID) -> list[PurchaseOrderLine]:
    return reader.find(PurchaseOrderLine, purchase_order_id=order_id)


def _lines(reader: Reader, order_id: UUID) -> list[PurchaseOrderLine]:
    """A purchase order's lines, but those removed."""
    return [line for line in _all_lines(reader, order_id) if line.state != "removed"]


def _draft(context: Context, order_id: UUID) -> PurchaseOrder:
    order = context.get(PurchaseOrder, order_id)
    if order.state != "draft":
        raise Rejected(f"purchase order {order.code} is {order.state}; only a draft changes")
    return order


def _open_order(context: Context, order_id: UUID) -> sales.SalesOrder:
    order = context.get(sales.SalesOrder, order_id)
    if order.state != "open":
        raise Rejected(f"sales order {order.code} is {order.state}")
    return order


def _require_lines(context: Context, order: PurchaseOrder) -> list[PurchaseOrderLine]:
    lines = _lines(context, order.id)
    if not lines:
        raise Rejected(f"purchase order {order.code} has no lines")
    return lines
