"""Airfreight: packing an order, its air waybill, and what each destination demands.

A waybill is filled in as each stage completes. Packing records which batch fulfils
which order line, and how much, which sets that stock aside; closing the packing needs
the number of pieces, their weight, size and temperature range. Booking adds the
airline's waybill number and flight, and needs the airports. Each destination country
requires its own documents, such as a veterinary import permit, and some are required
whatever the destination when the goods need them, such as a veterinary health
certificate; those requirements are data, kept here, not code. The waybill can be
completed only once every document required is held, and dispatched only once complete,
and while the customer's import permit is current. Dispatch takes the stock out of the
books and counts the order lines as shipped.

Airfreight adds to each item what customs and the airline must be told about it: its
commodity code and origin, whether it is dangerous goods, and what its shipment needs,
such as dry ice, a temperature logger or a health certificate. What a waybill needs is
worked out from the items packed on it: closing the packing refuses a temperature range
that would not keep every item within its limits, and a shipment without the dry ice or
logger its goods need, and goods that must not freeze are never packed with goods
shipped frozen.

What the airline charges by, the chargeable weight, what the goods are declared at for
customs, and the dangerous goods the airline must be told of are worked out here and
kept on the waybill. The air waybill is the proof that goods left the UK, which makes
their invoice zero-rated, so invoicing waits for dispatch.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from remit import items, parties
from remit.kernel import (
    Code,
    Command,
    Context,
    Country,
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

from . import sales, stock
from .common import (
    AddressRef,
    Airport,
    Kilograms,
    LineNumber,
    Packages,
    Quantity,
    amendments,
    next_code,
    next_line,
    refreshed,
    refuse,
    require_address,
)

module = Module("airfreight", requires=(parties.module, items.module, sales.module, stock.module))

RequirementId = Annotated[UUID, References("airfreight.requirement")]
WaybillId = Annotated[UUID, References("airfreight.waybill")]
PackingLineId = Annotated[UUID, References("airfreight.packing_line")]
AwbNumber = Annotated[str, StringConstraints(pattern=r"^\d{3}-\d{8}$")]
"""An air waybill number: the airline's three-digit prefix, a seven-digit serial and a
check digit, the serial's remainder on division by seven: 176-12345675."""
CommodityCode = Annotated[str, StringConstraints(pattern=r"^\d{6,10}$")]
"""A commodity code, as the Harmonized System numbers goods for customs: six to ten digits
without spaces, such as 30024200 for vaccines for veterinary medicine."""
UnNumber = Annotated[str, StringConstraints(pattern=r"^UN\d{4}$")]
"""A UN number, which identifies dangerous goods in transport: UN1170 for ethanol."""
CubicMetres = Annotated[Decimal, Field(gt=0, max_digits=8, decimal_places=3)]
NotifyParty = Annotated[UUID | None, References("parties.party")]

VOLUMETRIC = 6000
"""Cubic centimetres to the kilogram: airlines charge by volume at this rate when it comes
to more than the weight."""


class TemperatureRange(StrEnum):
    """The range a shipment must be kept in, as the airline's temperature labels name it."""

    CRT = "CRT"
    """Controlled room temperature: 15 to 25 °C."""
    ERT = "ERT"
    """Extended room temperature: 2 to 25 °C."""
    COL = "COL"
    """Cool: 2 to 8 °C."""
    FRO = "FRO"
    """Frozen: -20 °C or colder, usually on dry ice."""
    AMB = "AMB"
    """Ambient: carried without temperature control, as instruments and plastics are."""


_KEPT_AT: dict[TemperatureRange, tuple[int | None, int | None] | None] = {
    TemperatureRange.CRT: (15, 25),
    TemperatureRange.ERT: (2, 25),
    TemperatureRange.COL: (2, 8),
    TemperatureRange.FRO: (None, -20),
    TemperatureRange.AMB: None,
}
"""The coldest and warmest each range keeps goods at, in °C: None at an end for no limit,
and None for a range that controls nothing."""


class HazardClass(StrEnum):
    """A class or division of dangerous goods, as the UN Model Regulations number them."""

    FLAMMABLE_GAS = "2.1"
    """Flammable gases, such as aerosols of them."""
    GAS = "2.2"
    """Gases neither flammable nor toxic, such as compressed nitrogen."""
    FLAMMABLE_LIQUID = "3"
    """Flammable liquids, such as ethanol or xylene."""
    FLAMMABLE_SOLID = "4.1"
    """Flammable solids."""
    OXIDISER = "5.1"
    """Oxidising substances."""
    TOXIC = "6.1"
    """Toxic substances."""
    INFECTIOUS = "6.2"
    """Infectious substances, category A."""
    CORROSIVE = "8"
    """Corrosive substances, such as acids and alkalis."""
    MISCELLANEOUS = "9"
    """Miscellaneous dangerous goods, such as formalin solutions."""


class PackingGroup(StrEnum):
    """How dangerous goods within a class are: I for great danger, III for minor."""

    I = "I"  # noqa: E741  the regulations' own name
    """Great danger."""
    II = "II"
    """Medium danger."""
    III = "III"
    """Minor danger."""


class Packaging(StrEnum):
    """What an item is packed in to fly."""

    CARTON = "carton"
    """A fibreboard carton."""
    INSULATED_SHIPPER = "insulated_shipper"
    """A validated insulated shipper with conditioned gel packs, for 2 to 8 °C."""
    DRY_ICE_SHIPPER = "dry_ice_shipper"
    """An insulated shipper that vents, packed with dry ice."""
    UN_SPECIFICATION = "un_specification"
    """UN-specification packaging, tested and marked, as dangerous goods need."""
    TRIPLE = "triple"
    """Triple packaging to packing instruction 650, as a biological substance needs."""
    CRATE = "crate"
    """A wooden crate, for instruments."""


class Condition(StrEnum):
    """When a document is required."""

    ALWAYS = "always"
    """For every shipment."""
    HEALTH_CERTIFICATE = "health_certificate"
    """When any goods packed need a veterinary health certificate."""
    EXPORT_LICENCE = "export_licence"
    """When any goods packed need an export licence."""
    DANGEROUS_GOODS = "dangerous_goods"
    """When any goods packed are dangerous goods, declared by UN number."""


@module.extension
class ItemShipping(Extension[items.Item]):
    """What customs and the airline must be told about an item, and what its shipment
    needs."""

    searchable = ("commodity_code", "un_number", "proper_shipping_name", "customs_description")

    commodity_code: CommodityCode | None = None
    """Its commodity code, six to ten digits, as the customs declaration and the commercial
    invoice give it."""
    country_of_origin: Country | None = None
    """Where it was made, as a two-letter ISO code, for the commercial invoice."""
    customs_description: Name | None = None
    """How customs should read it, when its name is no help: Veterinary vaccine, inactivated."""
    un_number: Annotated[UnNumber | None, Field(title="UN number")] = None
    """Its UN number when it is dangerous goods by air, which a shipper's declaration then
    lists. Biological substances and dry ice are not given here: each has its own field."""
    hazard_class: HazardClass | None = None
    """Its dangerous goods class, which a UN number needs."""
    packing_group: PackingGroup | None = None
    """How dangerous it is within its class, for the classes that are grouped."""
    proper_shipping_name: Name | None = None
    """The name the regulations give it, as the shipper's declaration must: Ethanol
    solution."""
    biological_substance: bool = False
    """A biological substance, category B, flown as UN3373: triple packed and marked, but
    with no shipper's declaration."""
    dry_ice: bool = False
    """Flown on dry ice, which is dangerous goods in itself, UN1845: the waybill must say
    how much."""
    temperature_logger: bool = False
    """Flown with a logger recording the temperature in transit, whose serial number the
    waybill must give."""
    health_certificate: bool = False
    """Needs a veterinary health certificate to be exported, where a requirement asks for
    one."""
    export_licence: bool = False
    """Needs an export licence, as some pathogen detection kits and equipment do under UK
    export control."""
    packaging: Packaging = Packaging.CARTON
    """What it is packed in to fly."""


@module.document
class Requirement(Document):
    """A document required before goods may be sent: by a destination country, or for
    every destination, for every shipment or only when the goods packed need it."""

    lifecycle = Lifecycle(
        initial="active", transitions=(Transition("retire", source="active", target="retired"),)
    )
    unique = (("country", "document"),)
    searchable = ("document",)

    country: Country | None = None
    """The destination country that requires it, as a two-letter ISO code. None for a
    document required whatever the destination."""
    document: Name
    """What is required, such as Veterinary import permit or Health certificate."""
    condition: Condition = Condition.ALWAYS
    """When it is required: for every shipment, or only when the goods packed need it."""
    note: Name | None = None
    """Who issues it, or anything else that helps obtain it."""


@module.document
class Waybill(Document):
    """A shipment of an order by air, and its air waybill, filled in as it goes."""

    lifecycle = Lifecycle(
        initial="packing",
        transitions=(
            Transition(
                "close_packing",
                source="packing",
                target="packed",
                requires=("pieces", "gross_weight", "dimensions", "temperature"),
            ),
            Transition("reopen_packing", source="packed", target="packing"),
            Transition(
                "book",
                source="packed",
                target="booked",
                requires=("origin_airport", "destination_airport", "airline", "awb_number"),
            ),
            Transition("complete", source="booked", target="complete"),
            Transition("dispatch", source="complete", target="dispatched"),
        ),
    )
    unique = ("code", "awb_number")
    searchable = ("code", "awb_number", "programme", "flight")

    code: Code
    """Kestrel's number for the waybill, such as WB0042."""
    sales_order_id: sales.SalesOrderId
    """The sales order being shipped."""
    consignee_id: parties.PartyId
    """Who the goods are consigned to: the customer."""
    ship_to_id: AddressRef = None
    """Where the goods go, from the order unless it was changed."""
    notify_party_id: NotifyParty = None
    """Who the airline tells when the goods land, when not the consignee: a clearing agent."""
    destination: Country
    """The country the goods are going to, whose requirements apply."""
    programme: Name | None = None
    """The reference of the programme paying, which the waybill must carry."""
    origin_airport: Airport | None = None
    """Where the goods are flown from."""
    destination_airport: Airport | None = None
    """Where they land."""
    forwarder: Name | None = None
    """The freight forwarder handling the shipment, if one is."""
    pieces: Packages | None = None
    """How many packages the shipment is in."""
    gross_weight: Kilograms | None = None
    """What the packages weigh, packing and coolant included, in kilograms."""
    volume: CubicMetres | None = None
    """What the packages measure, in cubic metres."""
    chargeable_weight: Kilograms | None = None
    """What the airline charges by: the gross weight, or the volume at 6,000 cm³ to the
    kilo if that comes to more, rounded up to the half kilo."""
    dimensions: Name | None = None
    """Of each package, as the airline wants them: 4 boxes of 60 x 40 x 40 cm."""
    temperature: TemperatureRange | None = None
    """The range the shipment must be kept in."""
    dangerous_goods: Text | None = None
    """The dangerous goods the airline must be told of, a line for each, worked out from the
    items packed and the dry ice: UN1170 Ethanol solution, class 3, PG II."""
    dry_ice: Kilograms | None = None
    """Dry ice packed to keep the goods frozen, in kilograms. It is dangerous goods in
    itself, UN1845, and the airline must be told how much."""
    temperature_logger: Name | None = None
    """The serial number of the logger packed to record the temperature in transit."""
    instructions: Text | None = None
    """Handling instructions printed on the waybill: keep upright, do not freeze."""
    customs_value: Money
    """What the goods packed are declared at: their price on the order, in its currency."""
    airline: Name | None = None
    """The airline carrying it, once booked."""
    awb_number: Annotated[AwbNumber | None, Field(title="AWB number")] = None
    """The air waybill number the airline issued, once booked."""
    flight: Name | None = None
    """The flight booked, by the airline's number for it."""
    flight_date: date | None = None
    """When the flight booked leaves."""
    dispatched: date | None = None
    """When the goods left."""


@module.document
class PackingLine(Document):
    """How much of a batch was packed for an order line on a waybill."""

    lifecycle = Lifecycle(
        initial="packed", transitions=(Transition("unpack", source="packed", target="unpacked"),)
    )
    searchable = ("description", "package")
    part_of = "waybill_id"

    waybill_id: WaybillId
    """The waybill the line is packed on."""
    line_number: LineNumber
    """Where the line comes on the packing list."""
    sales_order_line_id: sales.SalesOrderLineId
    """The sales order line it ships."""
    item_id: items.ItemId
    """What is packed."""
    description: Name
    """What the packing list says, as the order line does."""
    batch_id: stock.BatchId
    """The batch it is packed from."""
    quantity: Quantity
    """How many are packed, in the item's unit."""
    unit: UnitCode
    """The item's unit, which the quantity counts."""
    package: Name | None = None
    """Which package it is in, as marked on the box: 2 of 4."""
    net_weight: Kilograms | None = None
    """What the goods weigh without packing, in kilograms."""
    value: Money
    """What the goods are worth at the order's price, for the customs declaration."""
    notes: Text | None = None
    """Anything the packing list should say about these goods."""


@module.document
class ShippingDocument(Document):
    """A document a destination requires, held for a waybill: a permit or certificate."""

    lifecycle = Lifecycle(initial="held")

    waybill_id: WaybillId
    """The waybill it is held for."""
    requirement_id: RequirementId
    """What the destination requires that it meets."""
    reference: Name
    """Its number, as issued."""
    issued: date | None = None
    """When it was issued."""
    expires: date | None = None
    """The last day it is good for, if it lapses."""


@module.event
class RequirementAdded(Event):
    """A destination's requirement was recorded."""

    requirement_id: RequirementId


@module.event
class RequirementRetired(Event):
    """A destination no longer requires a document."""

    requirement_id: RequirementId


@module.event
class WaybillOpened(Event):
    """Packing began for a shipment."""

    waybill_id: WaybillId
    code: str


@module.event
class LinePacked(Event):
    """Stock was packed for an order line."""

    packing_line_id: PackingLineId
    waybill_id: WaybillId


@module.event
class PackingLineAmended(Event):
    """A packing line was changed. `changed` names the fields that differ."""

    packing_line_id: PackingLineId
    changed: tuple[str, ...]


@module.event
class LineUnpacked(Event):
    """Packed stock was taken out again."""

    packing_line_id: PackingLineId


@module.event
class PackingClosed(Event):
    """Packing finished, with the shipment's pieces, weight, size and temperature range."""

    waybill_id: WaybillId


@module.event
class PackingReopened(Event):
    """Packing was reopened to change what is packed."""

    waybill_id: WaybillId


@module.event
class WaybillDetailsFilled(Event):
    """Details of a waybill were filled in. `changed` names the fields that differ."""

    waybill_id: WaybillId
    changed: tuple[str, ...]


@module.event
class FreightBooked(Event):
    """An airline took the booking and issued the waybill number."""

    waybill_id: WaybillId


@module.event
class ShippingDocumentHeld(Event):
    """A document the destination requires was received."""

    shipping_document_id: Annotated[UUID, References("airfreight.shipping_document")]
    waybill_id: WaybillId


@module.event
class WaybillCompleted(Event):
    """Everything the shipment needs is in place."""

    waybill_id: WaybillId


@module.event
class ShipmentDispatched(Event):
    """The shipment left the UK."""

    waybill_id: WaybillId
    dispatched: date


class Missing(BaseModel):
    """What a waybill still needs before it can be completed."""

    model_config = ConfigDict(frozen=True, use_attribute_docstrings=True)

    fields: tuple[str, ...]
    """Fields not yet filled in that its next step needs."""
    documents: tuple[str, ...]
    """Documents required for it that are not yet held."""


class AmendItemShipping(Command[ItemShipping]):
    """Replace what customs and the airline must be told about an item, and what its
    shipment needs. Every field is required; send null to clear one."""

    amends = "item_id"
    item_id: items.ItemId
    version: int
    """The version of the item these were read at. Refused if it has changed since."""
    commodity_code: CommodityCode | None
    country_of_origin: Country | None
    customs_description: Name | None
    un_number: Annotated[UnNumber | None, Field(title="UN number")]
    hazard_class: HazardClass | None
    packing_group: PackingGroup | None
    proper_shipping_name: Name | None
    biological_substance: bool
    dry_ice: bool
    temperature_logger: bool
    health_certificate: bool
    export_licence: bool
    packaging: Packaging


@module.event
class ItemShippingAmended(Event):
    """What customs or the airline are told about an item was changed. `changed` names the
    fields that differ."""

    item_id: items.ItemId
    changed: tuple[str, ...]


class AddRequirement(Command[Requirement]):
    country: Country | None = None
    """None for a document required whatever the destination."""
    document: Name
    condition: Condition = Condition.ALWAYS
    note: Name | None = None


class RetireRequirement(Command[Requirement]):
    moves = "retire"
    requirement_id: RequirementId


class OpenWaybill(Command[Waybill]):
    sales_order_id: sales.SalesOrderId
    destination: Country | None = None
    """The order's destination if left out."""
    code: Code | None = None
    """Numbered WB0001, WB0002 and on if left out."""


class PackLine(Command[PackingLine]):
    waybill_id: WaybillId
    sales_order_line_id: Annotated[
        UUID,
        References("sales.sales_order_line", where={"sales_order_id": "waybill_id.sales_order_id"}),
    ]
    batch_id: Annotated[
        UUID, References("stock.batch", where={"item_id": "sales_order_line_id.item_id"})
    ]
    quantity: Quantity
    package: Name | None = None
    net_weight: Kilograms | None = None
    """The item's net weight for the quantity, as stock records it, if left out."""
    notes: Text | None = None


class AmendPackingLine(Command[PackingLine]):
    """Replace which package a packed line is in, its net weight and its notes, while the
    waybill is being packed. Every field is required; send null to clear one."""

    amends = "packing_line_id"
    packing_line_id: PackingLineId
    version: int
    """The version this line was read at. Refused if it has changed since."""
    package: Name | None
    net_weight: Kilograms | None
    notes: Text | None


class UnpackLine(Command[PackingLine]):
    moves = "unpack"
    packing_line_id: PackingLineId


class FillWaybill(Command[Waybill]):
    """Fill in a waybill's details until its freight is booked. Every field is required;
    send null to leave one empty."""

    amends = "waybill_id"
    waybill_id: WaybillId
    version: int
    """The version these details were read at. Refused if the waybill has changed since."""
    ship_to_id: AddressRef
    notify_party_id: NotifyParty
    origin_airport: Airport | None
    destination_airport: Airport | None
    forwarder: Name | None
    pieces: Packages | None
    gross_weight: Kilograms | None
    volume: CubicMetres | None
    dimensions: Name | None
    temperature: TemperatureRange | None
    dry_ice: Kilograms | None
    temperature_logger: Name | None
    instructions: Text | None


class ClosePacking(Command[Waybill]):
    moves = "close_packing"
    waybill_id: WaybillId


class ReopenPacking(Command[Waybill]):
    moves = "reopen_packing"
    waybill_id: WaybillId


class BookFreight(Command[Waybill]):
    moves = "book"
    waybill_id: WaybillId
    airline: Name
    awb_number: Annotated[AwbNumber, Field(title="AWB number")]
    flight: Name | None = None
    flight_date: date | None = None


class HoldShippingDocument(Command[ShippingDocument]):
    waybill_id: WaybillId
    requirement_id: RequirementId
    reference: Name
    issued: date | None = None
    expires: date | None = None


class CompleteWaybill(Command[Waybill]):
    moves = "complete"
    waybill_id: WaybillId


class DispatchWaybill(Command[Waybill]):
    moves = "dispatch"
    waybill_id: WaybillId
    dispatched: date | None = None
    """Today if left out."""


class WhatIsMissing(Query[Missing]):
    waybill_id: WaybillId


@module.command
def amend_item_shipping(command: AmendItemShipping, context: Context) -> ItemShipping:
    """Change what customs and the airline must be told about an item, or what its
    shipment needs.

    A UN number needs its class and proper shipping name, and a class, packing group or
    proper shipping name needs a UN number. A biological substance and dry ice each have
    a field of their own, so their UN numbers are not given as the item's.
    """
    held = context.extension(ItemShipping, command.item_id, version=command.version)
    _require_declarable(command)
    amended, changed = context.revise(held, **amendments(command))
    context.emit(ItemShippingAmended(item_id=command.item_id, changed=changed))
    return amended


@module.command
def add_requirement(command: AddRequirement, context: Context) -> Requirement:
    """Record a document a destination country requires, or that every destination does."""
    if command.country is None and any(
        found.country is None for found in context.find(Requirement, document=command.document)
    ):
        raise Rejected(f"{command.document} is already required whatever the destination")
    requirement = context.create(Requirement.model_validate(command.model_dump()))
    context.emit(RequirementAdded(requirement_id=requirement.id))
    return requirement


@module.command
def retire_requirement(command: RetireRequirement, context: Context) -> Requirement:
    """Record that a destination no longer requires a document."""
    retired = context.transition(context.get(Requirement, command.requirement_id), "retire")
    context.emit(RequirementRetired(requirement_id=retired.id))
    return retired


@module.command
def open_waybill(command: OpenWaybill, context: Context) -> Waybill:
    """Begin packing an order for shipment, to its destination unless another is given,
    landing at the customer's airport, as its terms name it."""
    order = context.get(sales.SalesOrder, command.sales_order_id)
    if order.state != "open":
        raise Rejected(f"order {order.code} is {order.state}")
    terms = context.extension(sales.CustomerTerms, order.customer_id)
    waybill = context.create(
        Waybill(
            code=command.code or next_code(context, Waybill, "WB"),
            sales_order_id=order.id,
            consignee_id=order.customer_id,
            ship_to_id=order.ship_to_id,
            destination=command.destination or order.destination,
            destination_airport=terms.destination_airport,
            programme=order.programme,
            customs_value=Money.zero(order.currency),
        )
    )
    context.emit(WaybillOpened(waybill_id=waybill.id, code=waybill.code))
    return waybill


@module.command
def pack_line(command: PackLine, context: Context) -> PackingLine:
    """Pack stock of a batch for an order line, which sets it aside.

    No more can be packed for a line than was sold on it, and only stock that has been
    received, and has not expired, can be packed. Goods that must not freeze are not
    packed with goods shipped frozen.
    """
    waybill = _packing(context, command.waybill_id)
    line = context.get(sales.SalesOrderLine, command.sales_order_line_id)
    if line.sales_order_id != waybill.sales_order_id:
        raise Rejected(f"that line is on another order than waybill {waybill.code}'s")
    batch = context.get(stock.Batch, command.batch_id)
    if batch.item_id != line.item_id:
        item = context.get(items.Item, line.item_id)
        raise Rejected(f"batch {batch.batch} is not of {item.code}, which the line is for")
    packed = packed_for(context, line.id)
    if packed + command.quantity > line.quantity:
        raise Rejected(
            f"{line.quantity} were sold on this line and {packed} are packed already; "
            f"{line.quantity - packed} more can be packed, not {command.quantity}"
        )
    _require_together(context, waybill, line.item_id)
    context.execute(stock.AllocateStock(batch_id=batch.id, quantity=command.quantity))
    weighs = context.extension(stock.ItemStock, line.item_id).net_weight
    numbers = [other.line_number for other in context.find(PackingLine, waybill_id=waybill.id)]
    packing = context.create(
        PackingLine(
            waybill_id=waybill.id,
            line_number=next_line(numbers),
            sales_order_line_id=line.id,
            item_id=line.item_id,
            description=line.description,
            batch_id=batch.id,
            quantity=command.quantity,
            unit=line.unit,
            package=command.package,
            net_weight=command.net_weight or (weighs * command.quantity if weighs else None),
            value=sales.net_price(line, command.quantity),
            notes=command.notes,
        )
    )
    _worked_out(context, waybill)
    context.emit(LinePacked(packing_line_id=packing.id, waybill_id=waybill.id))
    return packing


@module.command
def amend_packing_line(command: AmendPackingLine, context: Context) -> PackingLine:
    """Change which package a packed line is in, its net weight or its notes."""
    packing = context.get(PackingLine, command.packing_line_id, version=command.version)
    _packing(context, packing.waybill_id)
    if packing.state != "packed":
        raise Rejected(f"packing line {packing.line_number} is {packing.state}")
    amended, changed = context.revise(packing, **amendments(command))
    context.emit(PackingLineAmended(packing_line_id=packing.id, changed=changed))
    return amended


@module.command
def unpack_line(command: UnpackLine, context: Context) -> PackingLine:
    """Take packed stock out again, putting it back in stock."""
    packing = context.get(PackingLine, command.packing_line_id)
    waybill = _packing(context, packing.waybill_id)
    context.execute(stock.ReleaseStock(batch_id=packing.batch_id, quantity=packing.quantity))
    unpacked = context.transition(packing, "unpack")
    _worked_out(context, waybill)
    context.emit(LineUnpacked(packing_line_id=unpacked.id))
    return unpacked


@module.command
def fill_waybill(command: FillWaybill, context: Context) -> Waybill:
    """Fill in where a waybill goes, who is told, its pieces, weight, size, temperature
    range, dry ice, temperature logger and handling instructions."""
    waybill = context.get(Waybill, command.waybill_id, version=command.version)
    if waybill.state not in {"packing", "packed"}:
        raise Rejected(
            f"waybill {waybill.code} is {waybill.state}; its details are fixed once the "
            "freight is booked"
        )
    require_address(context, waybill.consignee_id, command.ship_to_id, "ship_to_id")
    filled, changed = context.revise(waybill, **amendments(command))
    weighed = refreshed(
        context, filled, {"chargeable_weight": chargeable(filled.gross_weight, filled.volume)}
    )
    declared = _worked_out(context, weighed)
    context.emit(WaybillDetailsFilled(waybill_id=waybill.id, changed=changed))
    return declared


@module.command
def close_packing(command: ClosePacking, context: Context) -> Waybill:
    """Finish packing. The pieces, weight, size and temperature range must be filled in.

    The range must keep every item packed within its temperature limits, and the dry ice
    and temperature logger the goods need must be given.
    """
    waybill = context.get(Waybill, command.waybill_id)
    lines = _packed(context, waybill.id)
    if not lines:
        raise Rejected(f"nothing is packed on waybill {waybill.code}")
    closed = context.transition(waybill, "close_packing")
    _require_suited(closed, _needs(context, [line.item_id for line in lines]))
    context.emit(PackingClosed(waybill_id=closed.id))
    return closed


@module.command
def reopen_packing(command: ReopenPacking, context: Context) -> Waybill:
    """Reopen packing to change what is packed, before the freight is booked."""
    reopened = context.transition(context.get(Waybill, command.waybill_id), "reopen_packing")
    context.emit(PackingReopened(waybill_id=reopened.id))
    return reopened


@module.command
def book_freight(command: BookFreight, context: Context) -> Waybill:
    """Record the airline's booking and the waybill number it issued, whose check digit
    must be right."""
    serial, check = command.awb_number[4:11], command.awb_number[11]
    if int(serial) % 7 != int(check):
        said = f"ends in {check}, but its check digit should be {int(serial) % 7}"
        raise Rejected(
            f"awb_number {command.awb_number} {said}", problems=(Problem(("awb_number",), said),)
        )
    booked = context.transition(
        context.get(Waybill, command.waybill_id),
        "book",
        airline=command.airline,
        awb_number=command.awb_number,
        flight=command.flight,
        flight_date=command.flight_date,
    )
    context.emit(FreightBooked(waybill_id=booked.id))
    return booked


@module.command
def hold_shipping_document(command: HoldShippingDocument, context: Context) -> ShippingDocument:
    """Record that a document required for a waybill has been received. One that has
    already lapsed is refused, and so is one the waybill does not need."""
    waybill = context.get(Waybill, command.waybill_id)
    if waybill.state in {"complete", "dispatched"}:
        raise Rejected(f"waybill {waybill.code} is {waybill.state}")
    requirement = context.get(Requirement, command.requirement_id)
    if requirement.country not in (None, waybill.destination):
        raise Rejected(
            f"{requirement.document} is required by {requirement.country}; "
            f"waybill {waybill.code} goes to {waybill.destination}"
        )
    if requirement.id not in {found.id for found in _required(context, waybill)}:
        raise Rejected(
            f"{requirement.document} is required only {_WHEN[requirement.condition]}; "
            f"nothing packed on waybill {waybill.code} needs it"
        )
    if context.find(ShippingDocument, waybill_id=waybill.id, requirement_id=requirement.id):
        raise Rejected(f"waybill {waybill.code} already holds its {requirement.document}")
    if command.expires is not None and command.expires < context.now.date():
        raise Rejected(f"{requirement.document} {command.reference} lapsed on {command.expires}")
    held = context.create(ShippingDocument.model_validate(command.model_dump()))
    context.emit(ShippingDocumentHeld(shipping_document_id=held.id, waybill_id=waybill.id))
    return held


@module.command
def complete_waybill(command: CompleteWaybill, context: Context) -> Waybill:
    """Mark a booked waybill complete, once every document required for it is held."""
    waybill = context.get(Waybill, command.waybill_id)
    lacking = _documents_missing(context, waybill)
    if lacking:
        raise Rejected(
            f"waybill {waybill.code} to {waybill.destination} needs {', '.join(lacking)}, "
            "which it does not hold yet"
        )
    completed = context.transition(waybill, "complete")
    context.emit(WaybillCompleted(waybill_id=completed.id))
    return completed


@module.command
def dispatch_waybill(command: DispatchWaybill, context: Context) -> Waybill:
    """Record that a complete shipment left the UK, issuing its stock and counting its
    order lines as shipped. Refused once the customer's import permit has lapsed."""
    waybill = context.get(Waybill, command.waybill_id)
    on = command.dispatched or context.now.date()
    terms = context.extension(sales.CustomerTerms, waybill.consignee_id)
    lapsed = terms.import_permit_expires
    if lapsed is not None and lapsed < on:
        customer = context.get(parties.Party, waybill.consignee_id)
        raise Rejected(
            f"{customer.code}'s import permit {terms.import_permit} lapsed on {lapsed}; record "
            "its renewal in the customer's terms before the goods leave"
        )
    dispatched = context.transition(waybill, "dispatch", dispatched=on)
    for packing in _packed(context, waybill.id):
        context.execute(
            stock.IssueStock(
                batch_id=packing.batch_id, quantity=packing.quantity, on=on, reference=waybill.code
            )
        )
        context.execute(
            sales.ShipOrderLine(
                sales_order_line_id=packing.sales_order_line_id, quantity=packing.quantity
            )
        )
    context.emit(ShipmentDispatched(waybill_id=waybill.id, dispatched=on))
    return dispatched


@module.query
def what_is_missing(query: WhatIsMissing, reader: Reader) -> Missing:
    """What a waybill still needs: fields its next step needs, and documents not yet held.

    Dry ice and a temperature logger are counted as needed while packing when the goods
    need them."""
    waybill = reader.get(Waybill, query.waybill_id)
    following = [t for t in waybill.lifecycle.transitions if t.source == waybill.state]
    needed = dict.fromkeys(field for t in following for field in t.requires)
    if waybill.state == "packing":
        needs = _needs(reader, [line.item_id for line in _packed(reader, waybill.id)])
        needed |= dict.fromkeys(("dry_ice",) if needs.dry_ice else ())
        needed |= dict.fromkeys(("temperature_logger",) if needs.logger else ())
    blank = tuple(field for field in needed if getattr(waybill, field) in (None, ""))
    return Missing(fields=blank, documents=tuple(_documents_missing(reader, waybill)))


def chargeable(gross_weight: Decimal | None, volume: Decimal | None) -> Decimal | None:
    """What an airline charges by: the gross weight, or the volume's weight at 6,000 cm³ to
    the kilo if that is more, rounded up to the half kilo. None until the weight is known."""
    if gross_weight is None:
        return None
    by_volume = volume * 1_000_000 / VOLUMETRIC if volume is not None else gross_weight
    weight = max(gross_weight, by_volume)
    return (weight * 2).to_integral_value(rounding=ROUND_CEILING) / 2


def packed_for(reader: Reader, sales_order_line_id: UUID) -> int:
    """How much has been packed for an order line, on every waybill."""
    found = reader.find(PackingLine, sales_order_line_id=sales_order_line_id, state="packed")
    return sum(line.quantity for line in found)


def packed(reader: Reader, waybill_id: UUID) -> list[PackingLine]:
    """What is packed on a waybill."""
    return _packed(reader, waybill_id)


def _packed(reader: Reader, waybill_id: UUID) -> list[PackingLine]:
    return reader.find(PackingLine, waybill_id=waybill_id, state="packed")


def _worked_out(context: Context, waybill: Waybill) -> Waybill:
    """Work out again, from what is packed on a waybill, what its goods are declared at for
    customs and the dangerous goods the airline must be told of."""
    lines = _packed(context, waybill.id)
    value = Money.total((line.value for line in lines), waybill.customs_value.currency)
    needs = _needs(context, [line.item_id for line in lines])
    declared = list(needs.declared)
    if needs.biological:
        declared.append("UN3373 Biological substance, category B")
    if waybill.dry_ice is not None:
        declared.append(f"UN1845 Dry ice, class 9, {waybill.dry_ice} kg")
    dangerous = "\n".join(declared) or None
    return refreshed(context, waybill, {"customs_value": value, "dangerous_goods": dangerous})


def _packing(context: Context, waybill_id: UUID) -> Waybill:
    waybill = context.get(Waybill, waybill_id)
    if waybill.state != "packing":
        raise Rejected(f"waybill {waybill.code} is {waybill.state}; reopen packing to change it")
    return waybill


_WHEN = {
    Condition.ALWAYS: "for every shipment",
    Condition.HEALTH_CERTIFICATE: "for goods that need a veterinary health certificate",
    Condition.EXPORT_LICENCE: "for goods that need an export licence",
    Condition.DANGEROUS_GOODS: "for dangerous goods",
}


@dataclass(frozen=True)
class _Needs:
    """What the items on a waybill need to fly, each list naming the items by code."""

    limits: tuple[tuple[str, Decimal | None, Decimal | None], ...]
    """Each item kept within temperature limits, with the coldest and warmest it may be."""
    frozen: tuple[str, ...]
    """Items kept frozen or flown on dry ice."""
    unfreezable: tuple[str, ...]
    """Items that freezing ruins."""
    dry_ice: tuple[str, ...]
    logger: tuple[str, ...]
    declared: tuple[str, ...]
    """A line for each UN number, as a shipper's declaration lists dangerous goods."""
    biological: bool
    health_certificate: bool
    export_licence: bool

    def brings(self, condition: Condition) -> bool:
        """Whether these items make a document with this condition required."""
        return {
            Condition.ALWAYS: True,
            Condition.HEALTH_CERTIFICATE: self.health_certificate,
            Condition.EXPORT_LICENCE: self.export_licence,
            Condition.DANGEROUS_GOODS: bool(self.declared),
        }[condition]


def _needs(reader: Reader, item_ids: list[UUID]) -> _Needs:
    """What these items need to fly, as stock and airfreight record them."""
    limits: list[tuple[str, Decimal | None, Decimal | None]] = []
    frozen: list[str] = []
    unfreezable: list[str] = []
    dry_ice: list[str] = []
    logger: list[str] = []
    declared: set[str] = set()
    biological = health_certificate = export_licence = False
    for item_id in dict.fromkeys(item_ids):
        code = reader.get(items.Item, item_id).code
        kept = reader.extension(stock.ItemStock, item_id)
        flown = reader.extension(ItemShipping, item_id)
        if kept.temperature_min is not None or kept.temperature_max is not None:
            limits.append((code, kept.temperature_min, kept.temperature_max))
        if kept.storage is stock.Storage.FROZEN or flown.dry_ice:
            frozen.append(code)
        unfreezable += [code] if kept.do_not_freeze else []
        dry_ice += [code] if flown.dry_ice else []
        logger += [code] if flown.temperature_logger else []
        if flown.un_number is not None:
            group = f", PG {flown.packing_group}" if flown.packing_group else ""
            said = f"{flown.un_number} {flown.proper_shipping_name}, class {flown.hazard_class}"
            declared.add(said + group)
        biological |= flown.biological_substance
        health_certificate |= flown.health_certificate
        export_licence |= flown.export_licence
    return _Needs(
        limits=tuple(limits),
        frozen=tuple(frozen),
        unfreezable=tuple(unfreezable),
        dry_ice=tuple(dry_ice),
        logger=tuple(logger),
        declared=tuple(sorted(declared)),
        biological=biological,
        health_certificate=health_certificate,
        export_licence=export_licence,
    )


def _keeps(temperature: TemperatureRange, low: Decimal | None, high: Decimal | None) -> bool:
    """Whether a temperature range keeps goods within these limits, in °C."""
    if low is None and high is None:
        return True
    bounds = _KEPT_AT[temperature]
    if bounds is None:
        return False
    coldest, warmest = bounds
    return (low is None or (coldest is not None and coldest >= low)) and (
        high is None or (warmest is not None and warmest <= high)
    )


def _span(low: Decimal | None, high: Decimal | None) -> str:
    if low is None:
        return f"{high} °C or colder"
    if high is None:
        return f"{low} °C or warmer"
    return f"{low} to {high} °C"


def _listed(codes: tuple[str, ...], verb: str, verbs: str) -> str:
    return f"{', '.join(codes)} {verbs if len(codes) == 1 else verb}"


def _require_together(reader: Reader, waybill: Waybill, item_id: UUID) -> None:
    """Refuse to pack goods that must not freeze with goods shipped frozen."""
    here = _needs(reader, [line.item_id for line in _packed(reader, waybill.id)])
    adding = _needs(reader, [item_id])
    if adding.unfreezable and here.frozen:
        clash = f"{', '.join(here.frozen)}, shipped frozen"
    elif adding.frozen and here.unfreezable:
        clash = f"{', '.join(here.unfreezable)}, which must not freeze"
    else:
        return
    code = (adding.unfreezable or adding.frozen)[0]
    raise Rejected(
        f"{code} cannot be packed: waybill {waybill.code} carries {clash}; pack it on a "
        "waybill of its own"
    )


def _require_suited(waybill: Waybill, needs: _Needs) -> None:
    """Refuse to close a waybill whose temperature range, dry ice or logger does not suit
    what is packed on it."""
    problems: list[Problem] = []
    if waybill.temperature is not None:
        unkept = [
            f"{code} ({_span(low, high)})"
            for code, low, high in needs.limits
            if not _keeps(waybill.temperature, low, high)
        ]
        if unkept:
            said = f"{waybill.temperature} would not keep {', '.join(unkept)} within its limits"
            problems.append(Problem(("temperature",), said))
    if needs.dry_ice and waybill.dry_ice is None:
        said = f"is needed: {_listed(needs.dry_ice, 'fly', 'flies')} on dry ice"
        problems.append(Problem(("dry_ice",), said))
    if waybill.dry_ice is not None and not needs.dry_ice:
        problems.append(Problem(("dry_ice",), "is given, but nothing packed flies on dry ice"))
    if needs.logger and waybill.temperature_logger is None:
        said = f"is needed: {_listed(needs.logger, 'fly', 'flies')} with a temperature logger"
        problems.append(Problem(("temperature_logger",), said))
    refuse(problems)


def _require_declarable(command: AmendItemShipping) -> None:
    """Refuse dangerous goods details that are incomplete or misplaced."""
    problems: list[Problem] = []
    own = {"UN3373": "biological_substance", "UN1845": "dry_ice"}
    if command.un_number in own:
        said = f"is not given here: declare it through {own[command.un_number]}"
        problems.append(Problem(("un_number",), said))
    elif command.un_number is None:
        problems += [
            Problem((field,), "needs a UN number")
            for field in ("hazard_class", "packing_group", "proper_shipping_name")
            if getattr(command, field) is not None
        ]
    else:
        problems += [
            Problem((field,), f"is needed with {command.un_number}")
            for field in ("hazard_class", "proper_shipping_name")
            if getattr(command, field) is None
        ]
    refuse(problems)


def _required(reader: Reader, waybill: Waybill) -> list[Requirement]:
    """Every document required for a waybill: by its destination or by every destination,
    for every shipment or because of what is packed on it."""
    needs = _needs(reader, [line.item_id for line in _packed(reader, waybill.id)])
    return [
        requirement
        for requirement in reader.find(Requirement, state="active")
        if requirement.country in (None, waybill.destination)
        and needs.brings(requirement.condition)
    ]


def _documents_missing(reader: Reader, waybill: Waybill) -> list[str]:
    held = {d.requirement_id for d in reader.find(ShippingDocument, waybill_id=waybill.id)}
    return [r.document for r in _required(reader, waybill) if r.id not in held]
