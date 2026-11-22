"""Sales: quotations for tenders, the orders they become, and the deposits paid on them.

A quotation is priced line by line. Each line carries what Kestrel will pay for the item
as well as what it charges, taken from the manufacturer's current price, so that margin
is known before the quotation goes out. A line's amount and margin, and the quotation's
totals, are worked out here whenever a line is added, amended or removed, at the
exchange rates of the quotation's date, and kept on the documents so that listings can
show them. A quotation below the margin floor must be approved by someone who may
approve quotations before it is sent.

A quotation that is won becomes a sales order. Each order line records the quotation
line it came from, and purchase orders, packing and invoices record the order line they
are for, so every line can be traced from the quotation to the invoice. An order line
counts how much of it has been shipped and invoiced, as airfreight and invoicing report
it, and the order closes once every line is invoiced in full.

Sales adds to each item how it is sold: its product group, the species and disease it
serves, its list price, whether it is a veterinary medicine, the multiples it is sold in,
and whether it may be sold or tendered at all. Quoting reads them, refusing an item
blocked for sale, a veterinary medicine for a customer not authorised to receive one,
an item a tender cannot take, and a quantity that is not a whole multiple. Sales also
adds to each customer its commercial terms, which a new quotation takes unless told
otherwise; a quotation on credit for more than the customer's credit limit must be
approved before it is sent, as one below the margin floor must.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from remit import access, items, ledger, parties
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
    Query,
    Reader,
    References,
    Rejected,
    Text,
    Transition,
    UnitCode,
)

from . import fx, pricelists
from .common import (
    AddressRef,
    Airport,
    ContactRef,
    Counted,
    Days,
    Incoterm,
    LineNumber,
    MarginPercent,
    MemberRef,
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
    percent,
    refreshed,
    refuse,
    require_address,
    require_contact,
    require_currency,
    require_unrepeated,
)

Ratio = Annotated[Decimal, Field(ge=0, lt=1, max_digits=6, decimal_places=4)]


class SalesSettings(BaseModel):
    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    margin_floor: Ratio = Decimal("0.12")
    """A quotation whose margin is below this must be approved before it is sent."""
    deposits_account: Code = "2300"
    """The liability account deposits are held in until they are set against an invoice."""
    bank_accounts: dict[Currency, Code] = Field(
        default_factory=lambda: {"GBP": "1200", "USD": "1210", "EUR": "1220"}
    )
    """The bank account money in each currency is paid into."""


module = Module(
    "sales",
    requires=(
        parties.module,
        items.module,
        ledger.module,
        access.module,
        fx.module,
        pricelists.module,
    ),
    settings=SalesSettings,
)

QuotationId = Annotated[UUID, References("sales.quotation")]
QuotationLineId = Annotated[UUID, References("sales.quotation_line")]
SalesOrderId = Annotated[UUID, References("sales.sales_order")]
SalesOrderLineId = Annotated[UUID, References("sales.sales_order_line")]


class LossReason(StrEnum):
    """Why a customer chose another offer."""

    PRICE = "price"
    """Another offer was cheaper."""
    DELIVERY = "delivery"
    """Another supplier could deliver sooner."""
    SPECIFICATION = "specification"
    """What was offered did not meet the tender's specification."""
    CANCELLED = "cancelled"
    """The customer cancelled the tender, or lost its budget."""


class ProductGroup(StrEnum):
    """The part of the range an item belongs to, as sales are reported."""

    VACCINES = "vaccines"
    """Veterinary vaccines and tuberculins."""
    DIAGNOSTICS = "diagnostics"
    """ELISA, PCR and rapid test kits."""
    REAGENTS = "reagents"
    """Laboratory reagents, media, stains and chemicals."""
    CONSUMABLES = "consumables"
    """Laboratory plastics, field supplies and protective equipment."""
    EQUIPMENT = "equipment"
    """Laboratory instruments."""
    COLD_CHAIN = "cold_chain"
    """Vaccine carriers, cold boxes, refrigerators and temperature monitoring."""


class Species(StrEnum):
    """An animal an item is for."""

    CATTLE = "cattle"
    BUFFALO = "buffalo"
    SHEEP = "sheep"
    GOATS = "goats"
    PIGS = "pigs"
    POULTRY = "poultry"
    DUCKS = "ducks"
    HORSES = "horses"
    DOGS = "dogs"
    CATS = "cats"
    CAMELS = "camels"


class CustomerType(StrEnum):
    """What kind of organisation a customer is, which decides what may be sold to it."""

    VETERINARY_SERVICE = "veterinary_service"
    """A government veterinary service or livestock department."""
    VETERINARY_LABORATORY = "veterinary_laboratory"
    """A veterinary diagnostic laboratory."""
    VETERINARY_DISTRIBUTOR = "veterinary_distributor"
    """A distributor licensed to sell veterinary medicines."""
    DEVELOPMENT_PROGRAMME = "development_programme"
    """A livestock or animal health programme, run with the veterinary service."""
    RESEARCH_INSTITUTE = "research_institute"
    """A university or research institute in veterinary science."""
    COOPERATIVE = "cooperative"
    """A farmers' cooperative, vaccinating its members' animals under a veterinarian."""
    COMMERCIAL_LABORATORY = "commercial_laboratory"
    """A laboratory testing for others, not authorised to receive veterinary medicines."""

    @property
    def receives_medicines(self) -> bool:
        """Whether it is authorised to receive veterinary medicines."""
        return self is not CustomerType.COMMERCIAL_LABORATORY


class CustomerDocument(StrEnum):
    """A document a customer asks to receive with every shipment."""

    CERTIFICATE_OF_ANALYSIS = "certificate_of_analysis"
    """The manufacturer's certificate of analysis for each batch."""
    CERTIFICATE_OF_ORIGIN = "certificate_of_origin"
    """A certificate of origin from a chamber of commerce."""
    PACKING_LIST = "packing_list"
    """A packing list, package by package."""
    TEMPERATURE_RECORD = "temperature_record"
    """The download of the logger that travelled with the goods."""
    LEGALISED_INVOICE = "legalised_invoice"
    """The commercial invoice, legalised by the destination's embassy."""
    INSURANCE_CERTIFICATE = "insurance_certificate"
    """The certificate of insurance for the goods in transit."""


@module.extension
class ItemSales(Extension[items.Item]):
    """How an item is sold: what it is for, what it is listed at, and to whom and in
    what quantities it may be sold."""

    searchable = ("disease_area", "registration_number")

    product_group: ProductGroup | None = None
    """The part of the range it belongs to, as sales are reported."""
    species: tuple[Species, ...] = ()
    """The animals it is for. None named for what serves every species, such as
    laboratory plastics."""
    disease_area: Name | None = None
    """The disease or programme it serves, such as Avian influenza or Brucellosis."""
    list_price: Money | None = None
    """What it is quoted at unless a quotation line gives another price, in the base
    currency, converted at the day's rate for a quotation in another."""
    veterinary_only: bool = False
    """A veterinary medicine, which may be sold only to customers authorised to receive
    one."""
    sales_multiple: PackSize = 1
    """It is sold only in whole multiples of this, such as the ten vials in a box."""
    blocked_for_sale: bool = False
    """It may not be quoted, such as while a quality complaint is open."""
    tender_eligible: bool = True
    """It may be offered in answer to a tender. False for what lacks the registration or
    documents a public tender demands."""
    registration_number: Name | None = None
    """Its UK marketing authorisation, as tenders for vaccines ask for: Vm 01234/5006."""


@module.extension
class CustomerTerms(Extension[parties.Party]):
    """How Kestrel trades with a customer: what kind of organisation it is, its currency,
    terms and credit, where its goods go, and the permit it imports them under."""

    searchable = ("import_permit", "vendor_number")

    customer_type: CustomerType | None = None
    """What kind of organisation it is, which decides whether it may buy veterinary
    medicines."""
    customer_currency: Currency | None = None
    """What it is quoted and invoiced in, unless a quotation says otherwise."""
    customer_payment_terms: PaymentTerms | None = None
    """When it pays, unless a quotation says otherwise."""
    credit_limit: Money | None = None
    """The most it may owe on credit terms, in its currency. A quotation on credit for
    more must be approved before it is sent."""
    customer_incoterm: Incoterm | None = None
    """The Incoterm it buys on, unless a quotation says otherwise."""
    customer_named_place: Name | None = None
    """The place its Incoterm names, such as its airport for CIP."""
    destination_airport: Airport | None = None
    """The airport its goods are flown to, which a waybill takes."""
    salesperson_id: MemberRef = None
    """Who at Kestrel looks after it, and so its quotations."""
    import_permit: Name | None = None
    """The number of the permit it imports veterinary goods under."""
    import_permit_expires: date | None = None
    """The last day its import permit holds. Nothing may be dispatched to it after then."""
    documents_required: tuple[CustomerDocument, ...] = ()
    """Documents it asks to receive with every shipment, beyond what its country requires."""
    vendor_number: Name | None = None
    """Kestrel's supplier number in its books, which its purchase orders quote."""


@module.document
class Quotation(Document):
    """A priced offer to a customer, usually in answer to a tender.

    Lines are added while it is a draft. Approving it fixes it as approved; reopening it
    returns it to draft. Once sent, it is won, lost or withdrawn.
    """

    lifecycle = Lifecycle(
        initial="draft",
        transitions=(
            Transition("approve", source="draft", target="approved"),
            Transition("reopen", source="approved", target="draft"),
            Transition("send", source="draft", target="sent", requires=("valid_until",)),
            Transition("send", source="approved", target="sent", requires=("valid_until",)),
            Transition("win", source="sent", target="won"),
            Transition("lose", source="sent", target="lost", requires=("lost_reason",)),
            Transition("withdraw", source="draft", target="withdrawn"),
            Transition("withdraw", source="approved", target="withdrawn"),
            Transition("withdraw", source="sent", target="withdrawn"),
        ),
    )
    unique = ("code",)
    searchable = ("code", "tender", "programme", "end_user")

    code: Code
    """Kestrel's number for the quotation, such as Q0042."""
    customer_id: parties.PartyId
    """The customer it is for."""
    contact_id: ContactRef = None
    """Who at the customer the quotation is addressed to."""
    bill_to_id: AddressRef = None
    """The customer's address the invoice will go to."""
    ship_to_id: AddressRef = None
    """The customer's address the goods will go to."""
    salesperson_id: MemberRef = None
    """Who at Kestrel is responsible for the quotation."""
    quoted: date
    """The quotation's date. Its costs and margin are worked out at the rates of this day."""
    valid_until: date | None = None
    """The last day the customer may accept it. Needed before it is sent."""
    requested_delivery: date | None = None
    """When the customer wants the goods."""
    sent: date | None = None
    """When it was sent to the customer."""
    currency: Currency
    """What the customer is quoted, and will be invoiced, in."""
    incoterm: Incoterm = Incoterm.CIP
    """Who pays for carriage and insurance, and where the goods become the customer's."""
    named_place: Name | None = None
    """The place the Incoterm names: the destination airport for CIP, say."""
    payment_terms: PaymentTerms = PaymentTerms.NET_30
    """When the customer will pay once invoiced."""
    shipment_method: ShipmentMethod = ShipmentMethod.AIR
    """How the goods will travel."""
    cold_chain: bool = False
    """Whether any of the goods must travel chilled or frozen."""
    destination: Country
    """The country the goods are going to, whose import rules apply."""
    tender: Name | None = None
    """The customer's reference for the tender this answers."""
    programme: Name | None = None
    """The reference of the programme paying, which every document must carry."""
    end_user: Name | None = None
    """The organisation that will use the goods, when it is not the customer: a
    laboratory supplied through a distributor, say."""
    notes: Text | None = None
    """Anything the customer should read, printed on the quotation."""
    lost_reason: LossReason | None = None
    """Why it was lost, once it is."""
    discount: Money
    """What the lines' discounts take off their list prices, in all."""
    net: Money
    """What the lines come to, after their discounts."""
    freight: Money
    """Carriage charged to the customer, in the quotation's currency."""
    insurance: Money
    """Insurance of the goods in transit charged to the customer, as CIP and CIF require."""
    total: Money
    """What the customer is quoted: the lines, carriage and insurance."""
    cost: Money
    """What the goods will cost to buy, in the base currency."""
    margin: Money
    """What the goods earn, in the base currency: the lines less their cost. Carriage and
    insurance are passed on at cost, so they earn nothing."""
    margin_percent: MarginPercent | None = None
    """The margin as a share of what the lines come to. None while nothing is charged."""


@module.document
class QuotationLine(Document):
    """An item on a quotation: how many, at what price, and what it will cost to buy."""

    lifecycle = Lifecycle(
        initial="current", transitions=(Transition("remove", source="current", target="removed"),)
    )
    searchable = ("description",)
    part_of = "quotation_id"

    quotation_id: QuotationId
    """The quotation the line is on."""
    line_number: LineNumber
    """Where the line comes on the quotation."""
    item_id: items.ItemId
    """What is quoted."""
    description: Name
    """What the customer reads: the item's name unless it was changed."""
    quantity: Quantity
    """How many the customer asks for, in the item's unit."""
    unit: UnitCode
    """The item's unit, which the quantity counts."""
    unit_price: Money
    """Per unit, before the discount, in the quotation's currency."""
    discount_percent: Percent = Decimal(0)
    """Taken off the line's list price, in per cent."""
    amount: Money
    """What the line comes to: the quantity at the unit price, less the discount."""
    supplier_id: parties.PartyId
    """The manufacturer it will be bought from."""
    price_id: pricelists.PriceId
    """The manufacturer's price the cost was taken from."""
    unit_cost: Money
    """Per unit, in the manufacturer's currency, from its price when the line was added."""
    cost: Money
    """What the line will cost to buy, in the base currency, at the quotation date's rates."""
    margin_percent: MarginPercent | None = None
    """What the line earns as a share of its amount."""
    lead_time_days: Days | None = None
    """How many days the manufacturer takes to supply it."""
    delivery: date | None = None
    """When Kestrel undertakes to deliver this line."""
    notes: Text | None = None
    """Anything the customer should read about this line."""


@module.document
class SalesOrder(Document):
    """A quotation the customer accepted: what Kestrel must buy, ship and invoice."""

    lifecycle = Lifecycle(
        initial="open",
        transitions=(
            Transition("close", source="open", target="closed"),
            Transition("cancel", source="open", target="cancelled"),
        ),
    )
    unique = ("code",)
    searchable = ("code", "tender", "programme", "customer_po", "end_user")

    code: Code
    """Kestrel's number for the order, such as SO0042."""
    quotation_id: QuotationId
    """The quotation the customer accepted."""
    customer_id: parties.PartyId
    """The customer who ordered."""
    contact_id: ContactRef = None
    """Who at the customer placed the order."""
    bill_to_id: AddressRef = None
    """The customer's address invoices go to."""
    ship_to_id: AddressRef = None
    """The customer's address the goods go to."""
    salesperson_id: MemberRef = None
    """Who at Kestrel is responsible for the order."""
    ordered: date
    """When the order was placed."""
    customer_po: Annotated[Name | None, Field(title="Customer PO")] = None
    """The customer's purchase order number, which its invoices must quote."""
    customer_po_date: Annotated[date | None, Field(title="Customer PO date")] = None
    """The date of the customer's purchase order."""
    requested_delivery: date | None = None
    """When the customer wants the goods."""
    promised_delivery: date | None = None
    """When Kestrel has promised to deliver them."""
    currency: Currency
    """What the customer is invoiced in."""
    incoterm: Incoterm
    """Who pays for carriage and insurance, and where the goods become the customer's."""
    named_place: Name | None = None
    """The place the Incoterm names."""
    payment_terms: PaymentTerms
    """When the customer will pay once invoiced, as quoted."""
    shipment_method: ShipmentMethod
    """How the goods will travel, as quoted."""
    cold_chain: bool
    """Whether any of the goods must travel chilled or frozen."""
    destination: Country
    """The country the goods are going to."""
    tender: Name | None = None
    """The customer's reference for the tender the order was won on."""
    programme: Name | None = None
    """The reference of the programme paying, which every document must carry."""
    end_user: Name | None = None
    """The organisation that will use the goods, when it is not the customer."""
    notes: Text | None = None
    """Anything those who buy and ship the order should know."""
    discount: Money
    """What the lines' discounts take off their list prices, in all."""
    net: Money
    """What the lines come to, after their discounts."""
    freight: Money
    """Carriage charged to the customer, on the order's first invoice."""
    insurance: Money
    """Insurance charged to the customer, on the order's first invoice."""
    total: Money
    """What the customer pays for the order: the lines, carriage and insurance."""
    cost: Money
    """What the goods cost to buy, in the base currency, as quoted."""
    margin: Money
    """What the goods earn, in the base currency, as quoted."""
    margin_percent: MarginPercent | None = None
    """The margin as a share of what the lines come to."""
    deposit_due: Money | None = None
    """The deposit the customer must pay before anything is bought."""
    deposit_received: Money
    """All deposits received so far, in the order's currency."""


@module.document
class SalesOrderLine(Document):
    """An item on a sales order, as it was quoted, and how much of it has gone and been
    invoiced. Its quantity and price are fixed as the customer accepted them."""

    lifecycle = Lifecycle(
        initial="open",
        transitions=(
            Transition("close", source="open", target="closed"),
            Transition("cancel", source="open", target="cancelled"),
        ),
    )
    searchable = ("description",)
    part_of = "sales_order_id"

    sales_order_id: SalesOrderId
    """The order the line is on."""
    quotation_line_id: QuotationLineId
    """The quotation line it was won from."""
    line_number: LineNumber
    """Where the line comes on the order."""
    item_id: items.ItemId
    """What is ordered."""
    description: Name
    """What the customer reads, as quoted unless it was changed."""
    quantity: Quantity
    """How many were ordered, in the item's unit."""
    unit: UnitCode
    """The item's unit, which the quantities count."""
    unit_price: Money
    """Per unit, before the discount, in the order's currency."""
    discount_percent: Percent
    """Taken off the line's list price, in per cent, as quoted."""
    amount: Money
    """What the line comes to: the quantity at the unit price, less the discount."""
    supplier_id: parties.PartyId
    """The manufacturer it is bought from."""
    unit_cost: Money
    """Per unit, in the manufacturer's currency, as quoted."""
    cost: Money
    """What the line costs to buy, in the base currency, as quoted."""
    margin_percent: MarginPercent | None = None
    """What the line earns as a share of its amount."""
    promised_delivery: date | None = None
    """When Kestrel has promised to deliver this line."""
    shipped: Counted = 0
    """How many have left with a shipment."""
    invoiced: Counted = 0
    """How many have been invoiced."""
    outstanding: Counted
    """How many are still to be shipped."""
    notes: Text | None = None
    """Anything those who buy and ship this line should know."""


@module.event
class ItemSalesAmended(Event):
    """How an item is sold was changed. `changed` names the fields that differ."""

    item_id: items.ItemId
    changed: tuple[str, ...]


@module.event
class CustomerTermsAmended(Event):
    """A customer's commercial terms were changed. `changed` names the fields that differ."""

    party_id: parties.PartyId
    changed: tuple[str, ...]


@module.event
class QuotationOpened(Event):
    """A quotation was started."""

    quotation_id: QuotationId
    code: str


@module.event
class QuotationAmended(Event):
    """A quotation's details were changed. `changed` names the fields that differ."""

    quotation_id: QuotationId
    changed: tuple[str, ...]


@module.event
class QuotationLineAdded(Event):
    """A line was added to a quotation."""

    quotation_line_id: QuotationLineId
    quotation_id: QuotationId


@module.event
class QuotationLineAmended(Event):
    """A quotation line was changed. `changed` names the fields that differ."""

    quotation_line_id: QuotationLineId
    changed: tuple[str, ...]


@module.event
class QuotationLineRemoved(Event):
    """A line was taken off a quotation."""

    quotation_line_id: QuotationLineId


@module.event
class QuotationApproved(Event):
    """A quotation below the margin floor was approved to be sent."""

    quotation_id: QuotationId


@module.event
class QuotationReopened(Event):
    """An approved quotation was returned to draft to be changed."""

    quotation_id: QuotationId


@module.event
class QuotationSent(Event):
    """A quotation was sent to the customer."""

    quotation_id: QuotationId


@module.event
class QuotationLost(Event):
    """The customer chose another offer."""

    quotation_id: QuotationId
    reason: LossReason


@module.event
class QuotationWithdrawn(Event):
    """Kestrel withdrew a quotation."""

    quotation_id: QuotationId


@module.event
class OrderPlaced(Event):
    """A quotation was won and became a sales order."""

    sales_order_id: SalesOrderId
    quotation_id: QuotationId


@module.event
class SalesOrderAmended(Event):
    """A sales order's details were changed. `changed` names the fields that differ."""

    sales_order_id: SalesOrderId
    changed: tuple[str, ...]


@module.event
class SalesOrderLineAmended(Event):
    """A sales order line was changed. `changed` names the fields that differ."""

    sales_order_line_id: SalesOrderLineId
    changed: tuple[str, ...]


@module.event
class DepositReceived(Event):
    """A deposit was paid on an order."""

    sales_order_id: SalesOrderId
    amount: Money


@module.event
class OrderLineShipped(Event):
    """Some of an order line left with a shipment."""

    sales_order_line_id: SalesOrderLineId
    quantity: int


@module.event
class OrderLineInvoiced(Event):
    """Some of an order line was invoiced."""

    sales_order_line_id: SalesOrderLineId
    quantity: int


@module.event
class OrderClosed(Event):
    """Everything on an order has been invoiced."""

    sales_order_id: SalesOrderId


@module.event
class OrderCancelled(Event):
    """An order was cancelled before anything on it was shipped."""

    sales_order_id: SalesOrderId


class Margin(BaseModel):
    """What a quotation or order earns, in the base currency."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    revenue: Money
    cost: Money
    margin: Money
    ratio: Decimal | None
    """Margin as a share of revenue; none when there is no revenue."""


class AmendItemSales(Command[ItemSales]):
    """Replace how an item is sold. Every field is required; send null to clear one."""

    amends = "item_id"
    item_id: items.ItemId
    version: int
    """The version of the item these were read at. Refused if it has changed since."""
    product_group: ProductGroup | None
    species: tuple[Species, ...]
    disease_area: Name | None
    list_price: Money | None
    veterinary_only: bool
    sales_multiple: PackSize
    blocked_for_sale: bool
    tender_eligible: bool
    registration_number: Name | None


class AmendCustomerTerms(Command[CustomerTerms]):
    """Replace a customer's commercial terms. Every field is required; send null to clear
    one."""

    amends = "party_id"
    party_id: parties.PartyId
    version: int
    """The version of the party these were read at. Refused if it has changed since."""
    customer_type: CustomerType | None
    customer_currency: Currency | None
    customer_payment_terms: PaymentTerms | None
    credit_limit: Money | None
    customer_incoterm: Incoterm | None
    customer_named_place: Name | None
    destination_airport: Airport | None
    salesperson_id: MemberRef
    import_permit: Name | None
    import_permit_expires: date | None
    documents_required: tuple[CustomerDocument, ...]
    vendor_number: Name | None


class OpenQuotation(Command[Quotation]):
    customer_id: parties.PartyId
    currency: Currency | None = None
    """The customer's, from its terms, if left out."""
    destination: Country
    quoted: date | None = None
    """Today if left out."""
    valid_until: date | None = None
    requested_delivery: date | None = None
    contact_id: ContactRef = None
    bill_to_id: AddressRef = None
    ship_to_id: AddressRef = None
    salesperson_id: MemberRef = None
    """The customer's, from its terms, if left out."""
    incoterm: Incoterm | None = None
    """The customer's, with its named place, from its terms, if left out; CIP if it has none."""
    named_place: Name | None = None
    payment_terms: PaymentTerms | None = None
    """The customer's, from its terms, if left out; 30 days if it has none."""
    shipment_method: ShipmentMethod = ShipmentMethod.AIR
    cold_chain: bool = False
    tender: Name | None = None
    programme: Name | None = None
    end_user: Name | None = None
    notes: Text | None = None
    freight: Money | None = None
    """No charge if left out."""
    insurance: Money | None = None
    """No charge if left out."""
    code: Code | None = None
    """Numbered Q0001, Q0002 and on if left out."""


class AmendQuotation(Command[Quotation]):
    """Replace a draft quotation's details. Every field is required; send null to clear one.

    Its customer, currency and date are fixed: its lines are priced in that currency and
    costed at that date's rates.
    """

    amends = "quotation_id"
    quotation_id: QuotationId
    version: int
    """The version these details were read at. Refused if the quotation has changed since."""
    contact_id: ContactRef
    bill_to_id: AddressRef
    ship_to_id: AddressRef
    salesperson_id: MemberRef
    valid_until: date | None
    requested_delivery: date | None
    incoterm: Incoterm
    named_place: Name | None
    payment_terms: PaymentTerms
    shipment_method: ShipmentMethod
    cold_chain: bool
    destination: Country
    tender: Name | None
    programme: Name | None
    end_user: Name | None
    notes: Text | None
    freight: Money
    insurance: Money


class AddQuotationLine(Command[QuotationLine]):
    quotation_id: QuotationId
    item_id: items.ItemId
    quantity: Quantity
    unit_price: Money | None = None
    """The item's list price, in the quotation's currency at the rate of its date, if left
    out."""
    discount_percent: Percent = Decimal(0)
    supplier_id: Annotated[UUID | None, References("parties.party")] = None
    """Who to buy from. May be left out when only one manufacturer offers the item."""
    description: Name | None = None
    """The item's name if left out."""
    lead_time_days: Days | None = None
    """The manufacturer's, from its price, if left out."""
    delivery: date | None = None
    """The quotation's date and the lead time if left out, when the lead time is known."""
    notes: Text | None = None


class AmendQuotationLine(Command[QuotationLine]):
    """Replace a line of a draft quotation. Every field is required; send null to clear one.

    A different supplier costs the line at that manufacturer's current price.
    """

    amends = "quotation_line_id"
    quotation_line_id: QuotationLineId
    version: int
    """The version this line was read at. Refused if it has changed since."""
    description: Name
    quantity: Quantity
    unit_price: Money
    discount_percent: Percent
    supplier_id: parties.PartyId
    lead_time_days: Days | None
    delivery: date | None
    notes: Text | None


class RemoveQuotationLine(Command[QuotationLine]):
    moves = "remove"
    quotation_line_id: QuotationLineId


class ApproveQuotation(Command[Quotation]):
    moves = "approve"
    quotation_id: QuotationId


class ReopenQuotation(Command[Quotation]):
    moves = "reopen"
    quotation_id: QuotationId


class SendQuotation(Command[Quotation]):
    moves = "send"
    quotation_id: QuotationId
    sent: date | None = None
    """Today if left out."""


class WinQuotation(Command[SalesOrder]):
    moves = "win"
    quotation_id: QuotationId
    deposit: Ratio = Decimal(0)
    """The share of the order's value the customer pays before anything is bought."""
    customer_po: Annotated[Name | None, Field(title="Customer PO")] = None
    customer_po_date: Annotated[date | None, Field(title="Customer PO date")] = None
    ordered: date | None = None
    """Today if left out."""
    code: Code | None = None
    """Numbered SO0001, SO0002 and on if left out."""


class LoseQuotation(Command[Quotation]):
    moves = "lose"
    quotation_id: QuotationId
    reason: LossReason


class WithdrawQuotation(Command[Quotation]):
    moves = "withdraw"
    quotation_id: QuotationId


class AmendSalesOrder(Command[SalesOrder]):
    """Replace an open order's details. Every field is required; send null to clear one.

    Its customer, currency and lines are fixed as the customer accepted them. Carriage and
    insurance are charged on the first invoice, so they are fixed once it is raised.
    """

    amends = "sales_order_id"
    sales_order_id: SalesOrderId
    version: int
    """The version these details were read at. Refused if the order has changed since."""
    contact_id: ContactRef
    bill_to_id: AddressRef
    ship_to_id: AddressRef
    salesperson_id: MemberRef
    customer_po: Annotated[Name | None, Field(title="Customer PO")]
    customer_po_date: Annotated[date | None, Field(title="Customer PO date")]
    requested_delivery: date | None
    promised_delivery: date | None
    incoterm: Incoterm
    named_place: Name | None
    payment_terms: PaymentTerms
    shipment_method: ShipmentMethod
    cold_chain: bool
    destination: Country
    end_user: Name | None
    notes: Text | None
    freight: Money
    insurance: Money


class AmendSalesOrderLine(Command[SalesOrderLine]):
    """Replace an open order line's description, promised delivery and notes. Every field
    is required; send null to clear one."""

    amends = "sales_order_line_id"
    sales_order_line_id: SalesOrderLineId
    version: int
    """The version this line was read at. Refused if it has changed since."""
    description: Name
    promised_delivery: date | None
    notes: Text | None


class RecordDeposit(Command[SalesOrder]):
    sales_order_id: SalesOrderId
    amount: Money
    received: date


class ShipOrderLine(Command[SalesOrderLine]):
    """Count some of an order line as shipped. Airfreight sends this as a shipment leaves."""

    sales_order_line_id: SalesOrderLineId
    quantity: Quantity


class InvoiceOrderLine(Command[SalesOrderLine]):
    """Count some of an order line as invoiced. Invoicing sends this as it raises an invoice."""

    sales_order_line_id: SalesOrderLineId
    quantity: Quantity


class CloseOrder(Command[SalesOrder]):
    moves = "close"
    sales_order_id: SalesOrderId


class CancelOrder(Command[SalesOrder]):
    moves = "cancel"
    sales_order_id: SalesOrderId


class QuotationMargin(Query[Margin]):
    quotation_id: QuotationId


class OrderMargin(Query[Margin]):
    sales_order_id: SalesOrderId


@module.command
def amend_item_sales(command: AmendItemSales, context: Context) -> ItemSales:
    """Change how an item is sold. Its list price is in the base currency, and it names
    each species once."""
    held = context.extension(ItemSales, command.item_id, version=command.version)
    base = context.settings(ledger.LedgerSettings).base_currency
    problems = [p for p in (require_unrepeated("species", command.species),) if p]
    price = command.list_price
    if price is not None and price.currency != base:
        said = f"is in {price.currency}, not the base currency, {base}"
        problems.append(Problem(("list_price",), said))
    elif price is not None and price.amount <= 0:
        problems.append(Problem(("list_price",), "must be more than nothing"))
    refuse(problems)
    amended, changed = context.revise(held, **amendments(command))
    context.emit(ItemSalesAmended(item_id=command.item_id, changed=changed))
    return amended


@module.command
def amend_customer_terms(command: AmendCustomerTerms, context: Context) -> CustomerTerms:
    """Change a customer's commercial terms.

    A named place needs the Incoterm that names it, an import permit's expiry needs the
    permit's number, and a credit limit is in the customer's currency.
    """
    held = context.extension(CustomerTerms, command.party_id, version=command.version)
    problems = [
        p for p in (require_unrepeated("documents_required", command.documents_required),) if p
    ]
    if command.customer_named_place is not None and command.customer_incoterm is None:
        problems.append(Problem(("customer_named_place",), "needs the Incoterm that names it"))
    if command.import_permit_expires is not None and command.import_permit is None:
        problems.append(Problem(("import_permit_expires",), "needs the permit's number"))
    limit = command.credit_limit
    if limit is not None and command.customer_currency is None:
        problems.append(Problem(("credit_limit",), "needs the customer's currency, to be in"))
    elif limit is not None and limit.currency != command.customer_currency:
        said = f"is in {limit.currency}, not the customer's currency, {command.customer_currency}"
        problems.append(Problem(("credit_limit",), said))
    refuse(problems)
    amended, changed = context.revise(held, **amendments(command))
    context.emit(CustomerTermsAmended(party_id=command.party_id, changed=changed))
    return amended


@module.command
def open_quotation(command: OpenQuotation, context: Context) -> Quotation:
    """Start a quotation for a customer, on its terms unless told otherwise. Lines are
    added to it next."""
    customer = context.get(parties.Party, command.customer_id)
    if customer.state != "active":
        raise Rejected(f"party {customer.code} is {customer.state}")
    _require_own(context, customer.id, command.contact_id, command.bill_to_id, command.ship_to_id)
    terms = context.extension(CustomerTerms, customer.id)
    currency = command.currency or terms.customer_currency
    if currency is None:
        said = f"is needed: {customer.code} has none in its terms"
        raise Rejected(f"currency {said}", problems=(Problem(("currency",), said),))
    incoterm, named_place = command.incoterm, command.named_place
    if incoterm is None:
        incoterm, named_place = (
            terms.customer_incoterm or Incoterm.CIP,
            named_place or terms.customer_named_place,
        )
    freight = command.freight or Money.zero(currency)
    insurance = command.insurance or Money.zero(currency)
    require_currency(currency, freight=freight, insurance=insurance)
    quoted = command.quoted or context.now.date()
    _require_valid(quoted, command.valid_until)
    taken = {"code", "quoted", "freight", "insurance", "currency", "incoterm", "named_place"}
    quotation = context.create(
        Quotation.model_validate(
            {
                **command.model_dump(exclude=taken | {"payment_terms", "salesperson_id"}),
                "code": command.code or next_code(context, Quotation, "Q"),
                "quoted": quoted,
                "currency": currency,
                "incoterm": incoterm,
                "named_place": named_place,
                "payment_terms": command.payment_terms
                or terms.customer_payment_terms
                or PaymentTerms.NET_30,
                "salesperson_id": command.salesperson_id or terms.salesperson_id,
                "freight": freight,
                "insurance": insurance,
                **_totals(context, [], currency, freight + insurance, quoted),
            }
        )
    )
    context.emit(QuotationOpened(quotation_id=quotation.id, code=quotation.code))
    return quotation


@module.command
def amend_quotation(command: AmendQuotation, context: Context) -> Quotation:
    """Change a draft quotation's terms, references, addresses, carriage or insurance."""
    quotation = context.get(Quotation, command.quotation_id, version=command.version)
    _require_draft(quotation, lines=False)
    _require_own(
        context, quotation.customer_id, command.contact_id, command.bill_to_id, command.ship_to_id
    )
    if command.tender is not None:
        for line in _lines(context, quotation.id):
            _require_tenderable(context, line.item_id, quotation.code, command.tender)
    require_currency(quotation.currency, freight=command.freight, insurance=command.insurance)
    _require_valid(quotation.quoted, command.valid_until)
    amended, changed = context.revise(quotation, **amendments(command))
    totalled = _total_quotation(context, amended)
    context.emit(QuotationAmended(quotation_id=quotation.id, changed=changed))
    return totalled


@module.command
def add_quotation_line(command: AddQuotationLine, context: Context) -> QuotationLine:
    """Add an item to a draft quotation, costed at the manufacturer's current price.

    An item blocked for sale is refused, as is a veterinary medicine for a customer not
    authorised to receive one, an item a tender cannot take on a quotation answering one,
    and a quantity that is not a whole multiple of what the item is sold in.
    """
    quotation = context.get(Quotation, command.quotation_id)
    _require_draft(quotation, lines=True)
    item = context.get(items.Item, command.item_id)
    if item.state != "active":
        raise Rejected(f"item {item.code} is {item.state}; it cannot be quoted")
    sold = context.extension(ItemSales, item.id)
    _require_saleable(context, quotation, item, sold)
    _require_multiple(item, sold, command.quantity)
    unit_price = command.unit_price or _list_price(context, quotation, item, sold)
    require_currency(quotation.currency, unit_price=unit_price)
    price = pricelists.offered(context, item.id, command.supplier_id)
    numbers = [line.line_number for line in context.find(QuotationLine, quotation_id=quotation.id)]
    lead = command.lead_time_days if command.lead_time_days is not None else price.lead_time_days
    delivery = command.delivery or (
        quotation.quoted + timedelta(days=lead) if lead is not None else None
    )
    line = context.create(
        QuotationLine(
            quotation_id=quotation.id,
            line_number=next_line(numbers),
            item_id=item.id,
            description=command.description or item.name,
            quantity=command.quantity,
            unit=item.unit,
            unit_price=unit_price,
            discount_percent=command.discount_percent,
            supplier_id=price.supplier_id,
            price_id=price.id,
            unit_cost=price.cost,
            lead_time_days=lead,
            delivery=delivery,
            notes=command.notes,
            **_priced(
                context,
                unit_price,
                command.quantity,
                command.discount_percent,
                price.cost,
                quotation.quoted,
            ),
        )
    )
    _total_quotation(context, quotation)
    context.emit(QuotationLineAdded(quotation_line_id=line.id, quotation_id=quotation.id))
    return line


@module.command
def amend_quotation_line(command: AmendQuotationLine, context: Context) -> QuotationLine:
    """Change a line of a draft quotation: what it says, how many, its price, its supplier,
    its lead time, its delivery or its notes."""
    line = context.get(QuotationLine, command.quotation_line_id, version=command.version)
    quotation = context.get(Quotation, line.quotation_id)
    if line.state != "current":
        raise Rejected(f"line {line.line_number} of quotation {quotation.code} is {line.state}")
    _require_draft(quotation, lines=True)
    require_currency(quotation.currency, unit_price=command.unit_price)
    if command.quantity != line.quantity:
        item = context.get(items.Item, line.item_id)
        _require_multiple(item, context.extension(ItemSales, item.id), command.quantity)
    price_id, unit_cost = line.price_id, line.unit_cost
    if command.supplier_id != line.supplier_id:
        price = pricelists.offered(context, line.item_id, command.supplier_id)
        price_id, unit_cost = price.id, price.cost
    amended, changed = context.revise(line, **amendments(command))
    priced = _priced(
        context,
        command.unit_price,
        command.quantity,
        command.discount_percent,
        unit_cost,
        quotation.quoted,
    )
    repriced = refreshed(context, amended, {"price_id": price_id, "unit_cost": unit_cost, **priced})
    _total_quotation(context, quotation)
    context.emit(QuotationLineAmended(quotation_line_id=line.id, changed=changed))
    return repriced


@module.command
def remove_quotation_line(command: RemoveQuotationLine, context: Context) -> QuotationLine:
    """Take a line off a draft quotation."""
    line = context.get(QuotationLine, command.quotation_line_id)
    quotation = context.get(Quotation, line.quotation_id)
    _require_draft(quotation, lines=True)
    removed = context.transition(line, "remove")
    _total_quotation(context, quotation)
    context.emit(QuotationLineRemoved(quotation_line_id=removed.id))
    return removed


@module.command
def approve_quotation(command: ApproveQuotation, context: Context) -> Quotation:
    """Approve a quotation to be sent whatever its margin. Changing it undoes this."""
    quotation = context.get(Quotation, command.quotation_id)
    _require_lines(context, quotation)
    approved = context.transition(quotation, "approve")
    context.emit(QuotationApproved(quotation_id=quotation.id))
    return approved


@module.command
def reopen_quotation(command: ReopenQuotation, context: Context) -> Quotation:
    """Return an approved quotation to draft, to change it. It must be approved again."""
    reopened = context.transition(context.get(Quotation, command.quotation_id), "reopen")
    context.emit(QuotationReopened(quotation_id=reopened.id))
    return reopened


@module.command
def send_quotation(command: SendQuotation, context: Context) -> Quotation:
    """Send a quotation to the customer. Below the margin floor it must be approved first,
    and so must one on credit for more than the customer's credit limit.

    Its margin is the one worked out at the rates of its date, and its total and the
    credit limit are compared in the base currency at those rates.
    """
    quotation = context.get(Quotation, command.quotation_id)
    _require_lines(context, quotation)
    if quotation.state == "draft":
        reasons = _needs_approval(context, quotation)
        if reasons:
            raise Rejected(
                f"quotation {quotation.code} {' and '.join(reasons)}; "
                "it must be approved before it is sent"
            )
    sent = context.transition(quotation, "send", sent=command.sent or context.now.date())
    context.emit(QuotationSent(quotation_id=quotation.id))
    return sent


@module.command
def win_quotation(command: WinQuotation, context: Context) -> SalesOrder:
    """Record that the customer accepted a quotation, which becomes a sales order."""
    quotation = context.get(Quotation, command.quotation_id)
    lines = _lines(context, quotation.id)
    won = context.transition(quotation, "win")
    promised = [line.delivery for line in lines if line.delivery is not None]
    ordered = command.ordered or context.now.date()
    order = context.create(
        SalesOrder(
            code=command.code or next_code(context, SalesOrder, "SO"),
            quotation_id=won.id,
            customer_id=won.customer_id,
            contact_id=won.contact_id,
            bill_to_id=won.bill_to_id,
            ship_to_id=won.ship_to_id,
            salesperson_id=won.salesperson_id,
            ordered=ordered,
            customer_po=command.customer_po,
            customer_po_date=command.customer_po_date,
            requested_delivery=won.requested_delivery,
            promised_delivery=max(promised, default=None),
            currency=won.currency,
            incoterm=won.incoterm,
            named_place=won.named_place,
            payment_terms=won.payment_terms,
            shipment_method=won.shipment_method,
            cold_chain=won.cold_chain,
            destination=won.destination,
            tender=won.tender,
            programme=won.programme,
            end_user=won.end_user,
            notes=won.notes,
            discount=won.discount,
            net=won.net,
            freight=won.freight,
            insurance=won.insurance,
            total=won.total,
            cost=won.cost,
            margin=won.margin,
            margin_percent=won.margin_percent,
            deposit_due=won.total.times(command.deposit) if command.deposit else None,
            deposit_received=Money.zero(won.currency),
        )
    )
    for number, line in enumerate(lines, start=1):
        context.create(
            SalesOrderLine(
                sales_order_id=order.id,
                quotation_line_id=line.id,
                line_number=number,
                item_id=line.item_id,
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price=line.unit_price,
                discount_percent=line.discount_percent,
                amount=line.amount,
                supplier_id=line.supplier_id,
                unit_cost=line.unit_cost,
                cost=line.cost,
                margin_percent=line.margin_percent,
                promised_delivery=line.delivery,
                outstanding=line.quantity,
                notes=line.notes,
            )
        )
    context.emit(OrderPlaced(sales_order_id=order.id, quotation_id=won.id))
    return order


@module.command
def lose_quotation(command: LoseQuotation, context: Context) -> Quotation:
    """Record that the customer chose another offer, and why."""
    lost = context.transition(
        context.get(Quotation, command.quotation_id), "lose", lost_reason=command.reason
    )
    context.emit(QuotationLost(quotation_id=lost.id, reason=command.reason))
    return lost


@module.command
def withdraw_quotation(command: WithdrawQuotation, context: Context) -> Quotation:
    """Withdraw a quotation Kestrel no longer stands by."""
    withdrawn = context.transition(context.get(Quotation, command.quotation_id), "withdraw")
    context.emit(QuotationWithdrawn(quotation_id=withdrawn.id))
    return withdrawn


@module.command
def amend_sales_order(command: AmendSalesOrder, context: Context) -> SalesOrder:
    """Change an open order's references, dates, terms, addresses, carriage or insurance."""
    order = context.get(SalesOrder, command.sales_order_id, version=command.version)
    if order.state != "open":
        raise Rejected(f"order {order.code} is {order.state}; it can no longer change")
    _require_own(
        context, order.customer_id, command.contact_id, command.bill_to_id, command.ship_to_id
    )
    require_currency(order.currency, freight=command.freight, insurance=command.insurance)
    charges = (command.freight, command.insurance)
    if charges != (order.freight, order.insurance) and any(
        line.invoiced for line in context.find(SalesOrderLine, sales_order_id=order.id)
    ):
        raise Rejected(
            f"order {order.code} has been invoiced, and its freight and insurance with it; "
            "they can no longer change"
        )
    amended, changed = context.revise(order, **amendments(command))
    totalled = refreshed(
        context, amended, {"total": amended.net + amended.freight + amended.insurance}
    )
    context.emit(SalesOrderAmended(sales_order_id=order.id, changed=changed))
    return totalled


@module.command
def amend_sales_order_line(command: AmendSalesOrderLine, context: Context) -> SalesOrderLine:
    """Change what an open order line says, when it is promised, or its notes."""
    line = context.get(SalesOrderLine, command.sales_order_line_id, version=command.version)
    if line.state != "open":
        order = context.get(SalesOrder, line.sales_order_id)
        raise Rejected(
            f"line {line.line_number} of order {order.code} is {line.state}; "
            "it can no longer change"
        )
    amended, changed = context.revise(line, **amendments(command))
    context.emit(SalesOrderLineAmended(sales_order_line_id=line.id, changed=changed))
    return amended


@module.command
def record_deposit(command: RecordDeposit, context: Context) -> SalesOrder:
    """Record a deposit paid into the bank, held as owed to the customer until invoiced."""
    order = context.get(SalesOrder, command.sales_order_id)
    if order.state != "open":
        raise Rejected(f"order {order.code} is {order.state}")
    if command.amount.currency != order.currency or command.amount.sign <= 0:
        raise Rejected(f"a deposit on {order.code} is a positive amount in {order.currency}")
    settings = context.settings(SalesSettings)
    bank = settings.bank_accounts.get(order.currency)
    if bank is None:
        raise Rejected(f"no bank account takes {order.currency}; name one in the settings")
    base = fx.in_base(context, command.amount, command.received)
    context.execute(
        ledger.PostJournalEntry(
            posting_date=command.received,
            description=f"Deposit on order {order.code}",
            postings=(
                ledger.debit(account(context, bank), command.amount, base=base),
                ledger.credit(
                    account(context, settings.deposits_account), command.amount, base=base
                ),
            ),
        )
    )
    paid = context.update(order, deposit_received=order.deposit_received + command.amount)
    context.emit(DepositReceived(sales_order_id=order.id, amount=command.amount))
    return paid


@module.command
def ship_order_line(command: ShipOrderLine, context: Context) -> SalesOrderLine:
    """Count some of an order line as shipped. No more can ship than is outstanding."""
    line = _open_line(context, command.sales_order_line_id)
    if command.quantity > line.outstanding:
        raise Rejected(
            f"{line.outstanding} of line {line.line_number} are still to ship, "
            f"not {command.quantity}"
        )
    shipped = line.shipped + command.quantity
    counted = context.update(line, shipped=shipped, outstanding=line.quantity - shipped)
    context.emit(OrderLineShipped(sales_order_line_id=line.id, quantity=command.quantity))
    return counted


@module.command
def invoice_order_line(command: InvoiceOrderLine, context: Context) -> SalesOrderLine:
    """Count some of an order line as invoiced. Only what has shipped can be invoiced."""
    line = _open_line(context, command.sales_order_line_id)
    if line.invoiced + command.quantity > line.shipped:
        raise Rejected(
            f"{line.shipped} of line {line.line_number} have shipped and {line.invoiced} are "
            f"invoiced; {command.quantity} more cannot be"
        )
    counted = context.update(line, invoiced=line.invoiced + command.quantity)
    context.emit(OrderLineInvoiced(sales_order_line_id=line.id, quantity=command.quantity))
    return counted


@module.command
def close_order(command: CloseOrder, context: Context) -> SalesOrder:
    """Close an order once everything on it has been invoiced, and its lines with it."""
    order = context.get(SalesOrder, command.sales_order_id)
    lines = context.find(SalesOrderLine, sales_order_id=order.id)
    if short := [line for line in lines if line.invoiced < line.quantity]:
        numbers = ", ".join(str(line.line_number) for line in short)
        raise Rejected(f"order {order.code} still has lines to invoice: {numbers}")
    closed = context.transition(order, "close")
    for line in lines:
        context.transition(line, "close")
    context.emit(OrderClosed(sales_order_id=closed.id))
    return closed


@module.command
def cancel_order(command: CancelOrder, context: Context) -> SalesOrder:
    """Cancel an order before anything on it has shipped, while it holds no deposit."""
    order = context.get(SalesOrder, command.sales_order_id)
    lines = context.find(SalesOrderLine, sales_order_id=order.id)
    if any(line.shipped for line in lines):
        raise Rejected(f"goods on order {order.code} have shipped; it cannot be cancelled")
    if not order.deposit_received.is_zero:
        raise Rejected(
            f"order {order.code} holds a deposit of {order.deposit_received}; "
            "it cannot be cancelled while the deposit is held"
        )
    cancelled = context.transition(order, "cancel")
    for line in lines:
        context.transition(line, "cancel")
    context.emit(OrderCancelled(sales_order_id=cancelled.id))
    return cancelled


@module.query
def quotation_margin(query: QuotationMargin, reader: Reader) -> Margin:
    """What a quotation would earn, in the base currency, at today's rates."""
    return margin(reader, _lines(reader, query.quotation_id), reader.now.date())


@module.query
def order_margin(query: OrderMargin, reader: Reader) -> Margin:
    """What an order earns, in the base currency, at today's rates."""
    lines = reader.find(SalesOrderLine, sales_order_id=query.sales_order_id)
    return margin(reader, lines, reader.now.date())


def margin(reader: Reader, lines: list[QuotationLine] | list[SalesOrderLine], on: date) -> Margin:
    """What lines earn, converting their amounts and costs to the base currency at a date's
    rates."""
    base = reader.settings(ledger.LedgerSettings).base_currency
    revenue = Money.total((fx.in_base(reader, line.amount, on) for line in lines), base)
    cost = Money.total(
        (fx.in_base(reader, line.unit_cost.times(line.quantity), on) for line in lines), base
    )
    earned = revenue - cost
    ratio = (
        None if revenue.is_zero else (earned.amount / revenue.amount).quantize(Decimal("0.0001"))
    )
    return Margin(revenue=revenue, cost=cost, margin=earned, ratio=ratio)


def net_price(line: SalesOrderLine, quantity: int) -> Money:
    """What part of an order line comes to, at its price and discount."""
    return amount(line.unit_price, quantity, line.discount_percent)


class _Priced(TypedDict):
    amount: Money
    cost: Money
    margin_percent: Decimal | None


class _Totals(TypedDict):
    discount: Money
    net: Money
    total: Money
    cost: Money
    margin: Money
    margin_percent: Decimal | None


def _priced(
    reader: Reader,
    unit_price: Money,
    quantity: int,
    discount_percent: Decimal,
    unit_cost: Money,
    on: date,
) -> _Priced:
    """What a line comes to, what it costs in the base currency and what it earns, at a
    date's rates."""
    charged = amount(unit_price, quantity, discount_percent)
    cost = fx.in_base(reader, unit_cost.times(quantity), on)
    earned = fx.in_base(reader, charged, on)
    return {
        "amount": charged,
        "cost": cost,
        "margin_percent": percent(earned - cost, earned),
    }


def _totals(
    reader: Reader, lines: Sequence[QuotationLine], currency: str, charges: Money, on: date
) -> _Totals:
    """A quotation's totals from its lines and its carriage and insurance, at a date's rates."""
    base = reader.settings(ledger.LedgerSettings).base_currency
    listed = Money.total((line.unit_price.times(line.quantity) for line in lines), currency)
    net = Money.total((line.amount for line in lines), currency)
    cost = Money.total((line.cost for line in lines), base)
    total = net + charges
    earned = Money.zero(base) if net.is_zero else fx.in_base(reader, net, on)
    return {
        "discount": listed - net,
        "net": net,
        "total": total,
        "cost": cost,
        "margin": earned - cost,
        "margin_percent": percent(earned - cost, earned),
    }


def _total_quotation(context: Context, quotation: Quotation) -> Quotation:
    """Work a quotation's totals out again from its current lines."""
    totals = _totals(
        context,
        _lines(context, quotation.id),
        quotation.currency,
        quotation.freight + quotation.insurance,
        quotation.quoted,
    )
    return refreshed(context, quotation, totals)


def _needs_approval(reader: Reader, quotation: Quotation) -> list[str]:
    """Why a quotation must be approved before it is sent, if it must."""
    reasons: list[str] = []
    floor = reader.settings(SalesSettings).margin_floor
    revenue = quotation.cost + quotation.margin
    ratio = None if revenue.is_zero else quotation.margin.amount / revenue.amount
    if ratio is None or ratio < floor:
        shown = "nothing" if ratio is None else f"{ratio:.1%}"
        reasons.append(f"earns {shown}, below the {floor:.0%} floor")
    limit = reader.extension(CustomerTerms, quotation.customer_id).credit_limit
    on = quotation.quoted
    if (
        limit is not None
        and quotation.payment_terms.days
        and fx.in_base(reader, quotation.total, on) > fx.in_base(reader, limit, on)
    ):
        customer = reader.get(parties.Party, quotation.customer_id)
        reasons.append(
            f"comes to {quotation.total} on credit, over {customer.code}'s limit of {limit}"
        )
    return reasons


def _require_saleable(
    reader: Reader, quotation: Quotation, item: items.Item, sold: ItemSales
) -> None:
    """Refuse to quote an item blocked for sale, a veterinary medicine to a customer not
    authorised to receive one, or an item a tender cannot take in answer to one."""
    if sold.blocked_for_sale:
        raise Rejected(f"item {item.code} is blocked for sale; it cannot be quoted")
    if sold.veterinary_only:
        kind = reader.extension(CustomerTerms, quotation.customer_id).customer_type
        if kind is None or not kind.receives_medicines:
            customer = reader.get(parties.Party, quotation.customer_id)
            who = (
                "has no customer type recorded"
                if kind is None
                else f"is a {kind.value.replace('_', ' ')}"
            )
            raise Rejected(
                f"{item.code} is a veterinary medicine, and {customer.code} {who}; "
                "it may be sold only to a customer authorised to receive one"
            )
    if quotation.tender is not None:
        _require_tenderable(reader, item.id, quotation.code, quotation.tender)


def _require_tenderable(reader: Reader, item_id: UUID, code: str, tender: str) -> None:
    if not reader.extension(ItemSales, item_id).tender_eligible:
        item = reader.get(items.Item, item_id)
        raise Rejected(
            f"{item.code} cannot be offered in a tender, and quotation {code} answers tender "
            f"{tender}"
        )


def _require_multiple(item: items.Item, sold: ItemSales, quantity: int) -> None:
    if quantity % sold.sales_multiple:
        said = f"must be a whole multiple of {sold.sales_multiple}, as {item.code} is sold"
        raise Rejected(f"quantity {quantity} {said}", problems=(Problem(("quantity",), said),))


def _list_price(reader: Reader, quotation: Quotation, item: items.Item, sold: ItemSales) -> Money:
    """An item's list price in a quotation's currency, at the rate of its date."""
    if sold.list_price is None:
        said = f"is needed: {item.code} has no list price"
        raise Rejected(f"unit_price {said}", problems=(Problem(("unit_price",), said),))
    rate = fx.rate_on(reader, quotation.currency, quotation.quoted)
    return sold.list_price.convert(quotation.currency, rate=1 / rate)


def _lines(reader: Reader, quotation_id: UUID) -> list[QuotationLine]:
    return reader.find(QuotationLine, quotation_id=quotation_id, state="current")


def _require_draft(quotation: Quotation, *, lines: bool) -> None:
    """Refuse to change a quotation, or its lines, unless it is a draft."""
    if quotation.state == "approved":
        raise Rejected(
            f"quotation {quotation.code} is approved; reopen it to change "
            f"{'its lines' if lines else 'it'}, and have it approved again"
        )
    if quotation.state != "draft":
        raise Rejected(
            f"quotation {quotation.code} is {quotation.state}; "
            f"{'its lines change' if lines else 'it changes'} only while it is a draft"
        )


def _require_own(
    reader: Reader,
    customer_id: UUID,
    contact_id: UUID | None,
    bill_to_id: UUID | None,
    ship_to_id: UUID | None,
) -> None:
    require_contact(reader, customer_id, contact_id)
    require_address(reader, customer_id, bill_to_id, "bill_to_id")
    require_address(reader, customer_id, ship_to_id, "ship_to_id")


def _require_valid(quoted: date, valid_until: date | None) -> None:
    if valid_until is not None and valid_until < quoted:
        said = f"is before the quotation's date, {quoted}"
        raise Rejected(f"valid_until {said}", problems=(Problem(("valid_until",), said),))


def _require_lines(context: Context, quotation: Quotation) -> list[QuotationLine]:
    lines = _lines(context, quotation.id)
    if not lines:
        raise Rejected(f"quotation {quotation.code} has no lines")
    return lines


def _open_line(context: Context, line_id: UUID) -> SalesOrderLine:
    line = context.get(SalesOrderLine, line_id)
    if line.state != "open":
        order = context.get(SalesOrder, line.sales_order_id)
        raise Rejected(f"line {line.line_number} of order {order.code} is {line.state}")
    return line
