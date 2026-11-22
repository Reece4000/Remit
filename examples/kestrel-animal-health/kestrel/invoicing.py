"""Invoicing: billing a shipment once it has left, zero-rated as an export, and its payment.

An export is zero-rated for VAT only with proof the goods left the UK, and the air
waybill is that proof, so a shipment is invoiced once its waybill is dispatched, never
before. Each invoice line is a packed line, priced as the order line was sold, so an
invoice traces back through the packing to the order and the quotation. The order's
carriage and insurance are charged on its first invoice. A deposit paid on the order is
set against the invoice, and the invoice falls due as the order's payment terms say. When
everything sold on an order has been invoiced, the order is closed.

An invoice is converted to the base currency at the rate of the day it is issued, and
the rate is kept on it. A customer who pays later pays at that day's rate, and the
difference is a gain or loss on exchange.

Invoicing adds to each item the income account its sales are recorded in, so that the
accounts show what each part of the range earns, and each invoice line gives the
commodity code and origin airfreight keeps for its item, as a commercial invoice must.

Not yet done: a deposit is released at the rate of the day it is invoiced, not the day it
was paid, and the difference stays on the deposits account until foreign balances are
revalued, which is not built. Payments are taken in full; a part payment is not.
"""

from datetime import date, timedelta
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from remit import items, ledger, parties
from remit.kernel import (
    Code,
    Command,
    Context,
    Country,
    Currency,
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
    Transition,
    UnitCode,
)

from . import airfreight, fx, sales, stock
from .common import (
    AddressRef,
    ContactRef,
    Incoterm,
    LineNumber,
    PaymentTerms,
    Percent,
    Quantity,
    account,
    amendments,
    next_code,
)
from .fx import Factor


class InvoicingSettings(BaseModel):
    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    debtors_account: Code = "1100"
    """The asset account what customers owe is held in."""
    sales_account: Code = "4000"
    """Where zero-rated export sales are recorded."""
    carriage_account: Code = "4100"
    """Where carriage and insurance charged to customers are recorded."""
    exchange_account: Code = "7800"
    """Where gains and losses on exchange go, when a customer pays an invoice at a rate
    other than the one it was issued at."""


module = Module(
    "invoicing",
    requires=(
        parties.module,
        items.module,
        ledger.module,
        fx.module,
        sales.module,
        stock.module,
        airfreight.module,
    ),
    settings=InvoicingSettings,
)

InvoiceId = Annotated[UUID, References("invoicing.invoice")]


class Treatment(StrEnum):
    ZERO_RATED_EXPORT = "zero_rated_export"
    """Goods exported from the UK, with proof that they left: VAT at 0%."""


@module.document
class Invoice(Document):
    """What a customer owes for one shipment. Final once issued; corrected by a credit note.

    It is paid once the customer pays what is due, or at once when a deposit covers it.
    """

    lifecycle = Lifecycle(
        initial="issued",
        transitions=(Transition("pay", source="issued", target="paid", requires=("paid",)),),
    )
    unique = ("code", "waybill_id")
    searchable = ("code", "programme", "customer_po", "awb_number")

    code: Code
    """Kestrel's number for the invoice, such as INV0042."""
    sales_order_id: sales.SalesOrderId
    """The sales order it bills."""
    waybill_id: airfreight.WaybillId
    """The waybill whose shipment it bills."""
    customer_id: parties.PartyId
    """The customer who owes it."""
    contact_id: ContactRef = None
    """Who at the customer the invoice is addressed to."""
    bill_to_id: AddressRef = None
    """The customer's address it is sent to."""
    issued: date
    """The day it was issued, on or after dispatch."""
    payment_terms: PaymentTerms
    """When the customer pays, as ordered."""
    due: date
    """When it must be paid, by its payment terms."""
    currency: Currency
    """The currency it is in: the order's."""
    treatment: Treatment
    """How VAT treats it."""
    incoterm: Incoterm
    """The terms the goods were sold on, as the order had them."""
    named_place: Name | None = None
    """The place the Incoterm names."""
    destination: Country
    """The country the goods went to."""
    awb_number: Annotated[str, Field(title="AWB number")]
    """The air waybill number: the proof of export that makes the invoice zero-rated."""
    customer_po: Annotated[Name | None, Field(title="Customer PO")] = None
    """The customer's purchase order number, which it needs to pay."""
    programme: Name | None = None
    """The reference of the programme paying, which it needs to pay."""
    net: Money
    """What the lines come to."""
    freight: Money
    """Carriage charged: the order's, on its first invoice."""
    insurance: Money
    """Insurance charged: the order's, on its first invoice."""
    total: Money
    """What is invoiced: the lines, carriage and insurance."""
    deposit_applied: Money
    """The part of the deposits paid on the order set against this invoice."""
    amount_due: Money
    """What is left to pay: the total less the deposit applied."""
    exchange_rate: Factor
    """What one unit of the invoice's currency was worth in the base currency on the day
    it was issued."""
    total_base: Money
    """The total in the base currency, at the invoice's exchange rate: what debtors were
    charged in the books."""
    paid: date | None = None
    """When what was due was paid."""
    payment_reference: Name | None = None
    """The bank's reference for the payment."""


@module.document
class InvoiceLine(Document):
    """An item invoiced: what was packed for an order line, at the price it was sold at."""

    lifecycle = Lifecycle(initial="issued")
    part_of = "invoice_id"

    invoice_id: InvoiceId
    """The invoice the line is on."""
    line_number: LineNumber
    """Where the line comes on the invoice."""
    packing_line_id: airfreight.PackingLineId
    """The packing line whose goods it bills."""
    sales_order_line_id: sales.SalesOrderLineId
    """The sales order line it bills."""
    item_id: items.ItemId
    """What is billed."""
    description: Name
    """What the invoice says, as the order line does."""
    batch_id: stock.BatchId
    """The batch shipped, which the customer's import papers name."""
    quantity: Quantity
    """How many are billed, in the item's unit."""
    unit: UnitCode
    """The item's unit, which the quantity counts."""
    unit_price: Money
    """Per unit, before the discount, as sold."""
    discount_percent: Percent
    """Taken off the line's list price, in per cent, as ordered."""
    amount: Money
    """What the line comes to: the quantity at the unit price, less the discount."""
    commodity_code: airfreight.CommodityCode | None = None
    """The item's commodity code, as airfreight keeps it, which customs reads here."""
    country_of_origin: Country | None = None
    """Where the item was made, as airfreight keeps it."""


@module.extension
class ItemInvoicing(Extension[items.Item]):
    """Where an item's sales are recorded in the books."""

    revenue_account: Code | None = None
    """The income account its sales are recorded in, so that the accounts show what each
    part of the range earns. The settings' sales account if none is given."""


class AmendItemInvoicing(Command[ItemInvoicing]):
    """Replace the account an item's sales are recorded in; send null for the settings'."""

    amends = "item_id"
    item_id: items.ItemId
    version: int
    """The version of the item this was read at. Refused if it has changed since."""
    revenue_account: Code | None


@module.event
class ItemInvoicingAmended(Event):
    """The account an item's sales are recorded in was changed."""

    item_id: items.ItemId
    changed: tuple[str, ...]


@module.event
class InvoiceIssued(Event):
    """An invoice was issued."""

    invoice_id: InvoiceId
    code: str


@module.event
class InvoicePaid(Event):
    """What was due on an invoice was paid, or a deposit covered it."""

    invoice_id: InvoiceId
    paid: date


class RaiseInvoice(Command[Invoice]):
    waybill_id: airfreight.WaybillId
    issued: date | None = None
    """Today if left out."""
    code: Code | None = None
    """Numbered INV0001, INV0002 and on if left out."""


class RecordPayment(Command[Invoice]):
    """Record that a customer paid what was due on an invoice, in full, into the bank."""

    moves = "pay"
    invoice_id: InvoiceId
    paid: date
    reference: Name | None = None
    """The bank's reference for the payment."""


@module.command
def amend_item_invoicing(command: AmendItemInvoicing, context: Context) -> ItemInvoicing:
    """Change the account an item's sales are recorded in, which must be an active income
    account."""
    held = context.extension(ItemInvoicing, command.item_id, version=command.version)
    code = command.revenue_account
    if code is not None:
        found = context.find(ledger.Account, code=code)
        said = None
        if not found:
            said = "is not an account in the ledger"
        elif found[0].type is not ledger.AccountType.INCOME:
            said = f"is not an income account: it is {found[0].type}"
        elif found[0].state != "active":
            said = "is archived"
        if said is not None:
            raise Rejected(
                f"revenue_account {code} {said}", problems=(Problem(("revenue_account",), said),)
            )
    amended, changed = context.revise(held, **amendments(command))
    context.emit(ItemInvoicingAmended(item_id=command.item_id, changed=changed))
    return amended


@module.command
def raise_invoice(command: RaiseInvoice, context: Context) -> Invoice:
    """Invoice a dispatched shipment, zero-rated, setting any deposit paid against it."""
    waybill = context.get(airfreight.Waybill, command.waybill_id)
    if waybill.state != "dispatched" or waybill.awb_number is None:
        raise Rejected(
            f"waybill {waybill.code} is {waybill.state}: an export is zero-rated only with "
            "proof that it left the UK, so it is invoiced once dispatched"
        )
    issued = command.issued or context.now.date()
    if waybill.dispatched is not None and issued < waybill.dispatched:
        raise Rejected(
            f"waybill {waybill.code} left on {waybill.dispatched}; its invoice cannot be dated "
            "before then"
        )
    order = context.get(sales.SalesOrder, waybill.sales_order_id)
    packed = airfreight.packed(context, waybill.id)
    sold = {line.id: context.get(sales.SalesOrderLine, line.sales_order_line_id) for line in packed}
    priced = {line.id: sales.net_price(sold[line.id], line.quantity) for line in packed}
    first = not context.find(Invoice, sales_order_id=order.id)
    zero = Money.zero(order.currency)
    freight, insurance = (order.freight, order.insurance) if first else (zero, zero)
    net = Money.total(priced.values(), order.currency)
    total = net + freight + insurance
    applied = min(_deposit_left(context, order), total)
    rate = fx.rate_on(context, order.currency, issued)
    base = context.settings(ledger.LedgerSettings).base_currency
    invoice = context.create(
        Invoice(
            code=command.code or next_code(context, Invoice, "INV"),
            sales_order_id=order.id,
            waybill_id=waybill.id,
            customer_id=order.customer_id,
            contact_id=order.contact_id,
            bill_to_id=order.bill_to_id,
            issued=issued,
            payment_terms=order.payment_terms,
            due=issued + timedelta(days=order.payment_terms.days),
            currency=order.currency,
            treatment=Treatment.ZERO_RATED_EXPORT,
            incoterm=order.incoterm,
            named_place=order.named_place,
            destination=waybill.destination,
            awb_number=waybill.awb_number,
            customer_po=order.customer_po,
            programme=order.programme,
            net=net,
            freight=freight,
            insurance=insurance,
            total=total,
            deposit_applied=applied,
            amount_due=total - applied,
            exchange_rate=rate,
            total_base=total.convert(base, rate=rate),
        )
    )
    settings = context.settings(InvoicingSettings)
    earned: dict[str, Money] = {}
    for number, line in enumerate(packed, start=1):
        order_line = sold[line.id]
        books = context.extension(ItemInvoicing, order_line.item_id).revenue_account
        code = books or settings.sales_account
        earned[code] = earned.get(code, zero) + priced[line.id]
        shipping = context.extension(airfreight.ItemShipping, order_line.item_id)
        context.create(
            InvoiceLine(
                invoice_id=invoice.id,
                line_number=number,
                packing_line_id=line.id,
                sales_order_line_id=order_line.id,
                item_id=order_line.item_id,
                description=order_line.description,
                batch_id=line.batch_id,
                quantity=line.quantity,
                unit=order_line.unit,
                unit_price=order_line.unit_price,
                discount_percent=order_line.discount_percent,
                amount=priced[line.id],
                commodity_code=shipping.commodity_code,
                country_of_origin=shipping.country_of_origin,
            )
        )
        context.execute(
            sales.InvoiceOrderLine(sales_order_line_id=order_line.id, quantity=line.quantity)
        )
    _post(context, invoice, order, earned)
    context.emit(InvoiceIssued(invoice_id=invoice.id, code=invoice.code))
    if invoice.amount_due.is_zero:
        invoice = context.transition(invoice, "pay", paid=issued)
        context.emit(InvoicePaid(invoice_id=invoice.id, paid=issued))
    lines = context.find(sales.SalesOrderLine, sales_order_id=order.id)
    if all(line.invoiced == line.quantity for line in lines):
        context.execute(sales.CloseOrder(sales_order_id=order.id))
    return invoice


@module.command
def record_payment(command: RecordPayment, context: Context) -> Invoice:
    """Record a customer's payment of what was due on an invoice.

    The bank receives it at the rate of the day it was paid; what the customer owed is
    cleared at the rate the invoice was issued at, and the difference is a gain or loss
    on exchange.
    """
    invoice = context.get(Invoice, command.invoice_id)
    if command.paid < invoice.issued:
        raise Rejected(
            f"invoice {invoice.code} was issued on {invoice.issued}; it cannot have been "
            f"paid before then, on {command.paid}"
        )
    bank = context.settings(sales.SalesSettings).bank_accounts.get(invoice.currency)
    if bank is None:
        raise Rejected(f"no bank account takes {invoice.currency}; name one in the settings")
    paid = context.transition(
        invoice, "pay", paid=command.paid, payment_reference=command.reference
    )
    settings = context.settings(InvoicingSettings)
    received = fx.in_base(context, invoice.amount_due, command.paid)
    owed = invoice.total_base - _base(context, invoice, invoice.deposit_applied)
    gained = received - owed
    postings = [
        ledger.debit(account(context, bank), invoice.amount_due, base=received),
        ledger.credit(account(context, settings.debtors_account), invoice.amount_due, base=owed),
    ]
    if gained.sign > 0:
        postings.append(ledger.credit(account(context, settings.exchange_account), gained))
    elif gained.sign < 0:
        postings.append(ledger.debit(account(context, settings.exchange_account), -gained))
    context.execute(
        ledger.PostJournalEntry(
            posting_date=command.paid,
            description=f"Payment of invoice {invoice.code}",
            postings=tuple(postings),
        )
    )
    context.emit(InvoicePaid(invoice_id=invoice.id, paid=command.paid))
    return paid


def _deposit_left(context: Context, order: sales.SalesOrder) -> Money:
    """Deposits paid on an order that earlier invoices have not used."""
    used = Money.total(
        (i.deposit_applied for i in context.find(Invoice, sales_order_id=order.id)),
        order.currency,
    )
    return order.deposit_received - used


def _base(context: Context, invoice: Invoice, amount: Money) -> Money:
    """An amount on an invoice in the base currency, at the rate it was issued at."""
    base = context.settings(ledger.LedgerSettings).base_currency
    return amount.convert(base, rate=invoice.exchange_rate)


def _post(
    context: Context, invoice: Invoice, order: sales.SalesOrder, earned: dict[str, Money]
) -> None:
    """Debtors owe the total, recorded as sales, in each income account what its items
    earned, and as carriage charged; the deposit then pays part of it. The total's value
    in the base currency is split between them in proportion, so that nothing is lost to
    rounding."""
    settings = context.settings(InvoicingSettings)
    debtors = account(context, settings.debtors_account)
    if invoice.total.is_zero:
        return
    charges = invoice.freight + invoice.insurance
    credited = [*earned.items(), (settings.carriage_account, charges)]
    credited = [(code, amount) for code, amount in credited if not amount.is_zero]
    total = invoice.total_base
    split = total.allocate([amount.amount for _, amount in credited])
    postings = [ledger.debit(debtors, invoice.total, base=total)]
    postings += [
        ledger.credit(account(context, code), amount, base=base)
        for (code, amount), base in zip(credited, split, strict=True)
    ]
    if not invoice.deposit_applied.is_zero:
        deposits = context.settings(sales.SalesSettings).deposits_account
        applied = _base(context, invoice, invoice.deposit_applied)
        postings += [
            ledger.debit(account(context, deposits), invoice.deposit_applied, base=applied),
            ledger.credit(debtors, invoice.deposit_applied, base=applied),
        ]
    context.execute(
        ledger.PostJournalEntry(
            posting_date=invoice.issued,
            description=f"Invoice {invoice.code} for order {order.code}, zero-rated export",
            postings=tuple(postings),
        )
    )
