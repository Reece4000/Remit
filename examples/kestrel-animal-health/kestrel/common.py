"""What Kestrel's own modules share. Not a module: plain code that each of them imports."""

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import Field, StringConstraints

from remit import ledger, parties
from remit.kernel import Command, Context, Document, Money, Problem, Reader, References, Rejected

Quantity = Annotated[int, Field(gt=0, le=1_000_000)]
"""How many of an item, in its unit: whole vials, kits or instruments."""

Counted = Annotated[int, Field(ge=0, le=1_000_000)]
"""How many of an item have been through a step so far, such as received or shipped."""

LineNumber = Annotated[int, Field(ge=1, le=9999)]
"""A line's place on its document: 1, 2, 3 in the order the lines were added."""

Percent = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
"""A share of a price taken off it, in per cent: 7.5 for seven and a half per cent."""

MarginPercent = Annotated[Decimal, Field(le=100, decimal_places=2)]
"""What is earned as a share of what is charged, in per cent. Negative below cost."""

Days = Annotated[int, Field(ge=0, le=730)]
"""A number of days, such as how long a manufacturer takes to supply something."""

Celsius = Annotated[Decimal, Field(ge=-90, le=60, max_digits=4, decimal_places=1)]
"""A temperature, in °C."""

Packages = Annotated[int, Field(ge=1, le=10_000)]
"""How many boxes, cartons or crates."""

PackSize = Annotated[int, Field(ge=1, le=10_000)]
"""How many of an item's unit go together: in a pack, or in each step of an order."""

Kilograms = Annotated[Decimal, Field(gt=0, max_digits=9, decimal_places=2)]
"""A weight, in kilograms."""

Centimetres = Annotated[Decimal, Field(gt=0, max_digits=6, decimal_places=1)]
"""A length, in centimetres."""

Airport = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
"""An airport's three-letter IATA code, such as LHR or NBO."""

ContactRef = Annotated[UUID | None, References("parties.contact")]
AddressRef = Annotated[UUID | None, References("parties.address")]
MemberRef = Annotated[UUID | None, References("access.member")]


class Incoterm(StrEnum):
    """Who pays for carriage and insurance, and where the goods pass from seller to buyer,
    as the International Chamber of Commerce's Incoterms 2020 define them."""

    EXW = "EXW"
    """Ex works: the buyer collects from the seller's premises."""
    FCA = "FCA"
    """Free carrier: the seller hands the goods, cleared for export, to the buyer's carrier
    at the named place, such as an airport."""
    CPT = "CPT"
    """Carriage paid to: the seller pays carriage to the named destination; the risk passes
    when the goods are handed to the first carrier."""
    CIP = "CIP"
    """Carriage and insurance paid to: as CPT, and the seller insures the goods in transit."""
    DAP = "DAP"
    """Delivered at place: the seller delivers to the named place, not unloaded or cleared."""
    DPU = "DPU"
    """Delivered at place unloaded: as DAP, and unloaded."""
    DDP = "DDP"
    """Delivered duty paid: the seller delivers, cleared for import with duties paid."""
    FAS = "FAS"
    """Free alongside ship: by sea, alongside the vessel at the named port."""
    FOB = "FOB"
    """Free on board: by sea, loaded on the vessel at the named port."""
    CFR = "CFR"
    """Cost and freight: by sea, the seller pays carriage to the named port."""
    CIF = "CIF"
    """Cost, insurance and freight: as CFR, and the seller insures the goods."""


class PaymentTerms(StrEnum):
    """When an invoice must be paid."""

    ADVANCE = "advance"
    """In full before the goods are shipped: due on the day it is invoiced."""
    LETTER_OF_CREDIT = "letter_of_credit"
    """By the customer's bank under a documentary credit, against the shipping documents:
    due on the day it is invoiced."""
    NET_30 = "net_30"
    """Within 30 days of the invoice."""
    NET_60 = "net_60"
    """Within 60 days of the invoice."""
    NET_90 = "net_90"
    """Within 90 days of the invoice."""

    @property
    def days(self) -> int:
        """How many days after the invoice payment is due."""
        return {"net_30": 30, "net_60": 60, "net_90": 90}.get(self.value, 0)


class ShipmentMethod(StrEnum):
    """How goods travel."""

    AIR = "air"
    """By air freight, under an air waybill."""
    SEA = "sea"
    """By sea freight, under a bill of lading."""
    ROAD = "road"
    """By road, under a CMR consignment note."""
    COURIER = "courier"
    """By express courier, door to door."""


def amount(unit_price: Money, quantity: int, discount_percent: Decimal) -> Money:
    """What a quantity comes to at a unit price, less a discount, to the minor unit."""
    return unit_price.times(quantity * (100 - discount_percent) / 100)


def percent(part: Money, whole: Money) -> Decimal | None:
    """One amount as a share of another, in per cent to two places; none of nothing."""
    if whole.is_zero:
        return None
    return (part.amount * 100 / whole.amount).quantize(Decimal("0.01"))


def amendments(command: Command[Any]) -> dict[str, object]:
    """What a command that amends a document replaces: each of its fields, as it holds
    them, but the one naming the document and the version."""
    named = (command.amends, "version")
    return {
        field: getattr(command, field) for field in type(command).model_fields if field not in named
    }


def refreshed[D: Document](context: Context, document: D, values: Mapping[str, object]) -> D:
    """A document with values worked out from others, written only if any of them differ."""
    if all(getattr(document, field) == value for field, value in values.items()):
        return document
    return context.update(document, **values)


def require_contact(reader: Reader, party_id: UUID, contact_id: UUID | None) -> None:
    """Refuse a contact who is not a current contact of the party."""
    if contact_id is None:
        return
    contact = reader.get(parties.Contact, contact_id)
    if contact.party_id != party_id or contact.state != "current":
        said = f"is not a current contact of {reader.get(parties.Party, party_id).code}"
        raise Rejected(f"contact_id {said}", problems=(Problem(("contact_id",), said),))


def require_address(reader: Reader, party_id: UUID, address_id: UUID | None, field: str) -> None:
    """Refuse an address, given in `field`, that is not a current address of the party."""
    if address_id is None:
        return
    address = reader.get(parties.Address, address_id)
    if address.party_id != party_id or address.state != "current":
        said = f"is not a current address of {reader.get(parties.Party, party_id).code}"
        raise Rejected(f"{field} {said}", problems=(Problem((field,), said),))


def refuse(problems: Sequence[Problem]) -> None:
    """Refuse a command for each field at fault, if any is, naming them all at once."""
    if problems:
        said = "; ".join(f"{problem.path[0]} {problem.message}" for problem in problems)
        raise Rejected(said, problems=tuple(problems))


def require_unrepeated(field: str, values: Sequence[object]) -> Problem | None:
    """The problem with a list of choices that names one twice, if it does."""
    repeated = sorted({str(value) for value in values if values.count(value) > 1})
    return Problem((field,), f"names {', '.join(repeated)} twice") if repeated else None


def require_currency(currency: str, **amounts: Money) -> None:
    """Refuse amounts that are not in a document's currency, naming each field at fault."""
    wrong = {
        field: money.currency for field, money in amounts.items() if money.currency != currency
    }
    if wrong:
        raise Rejected(
            f"give {', '.join(wrong)} in {currency}",
            problems=tuple(Problem((f,), f"is in {c}, not {currency}") for f, c in wrong.items()),
        )


def next_code(reader: Reader, kind: type[Document], prefix: str) -> str:
    """The code after the highest numbered one of this kind: Q0007 follows Q0006.

    Two people numbering at once may be given the same code. Codes are unique, so the
    second is refused, and succeeds when it is sent again.
    """
    numbered = {"gte": f"{prefix}0000", "lte": f"{prefix}9999"}
    page = reader.page(kind, where={"code": numbered}, order="-code", limit=1)
    if not page.items:
        return f"{prefix}0001"
    last = str(page.items[0].model_dump()["code"])
    digits = last.removeprefix(prefix)
    if not re.fullmatch(r"\d{4}", digits):
        raise Rejected(f"give the code yourself: the last one, {last}, is not numbered")
    return f"{prefix}{int(digits) + 1:04d}"


def next_line(lines: list[int]) -> int:
    """The number for a line added after these: one more than the highest."""
    return max(lines, default=0) + 1


def account(reader: Reader, code: str) -> UUID:
    """The ledger account with this code, which the module's settings name."""
    found = reader.find(ledger.Account, code=code)
    if not found:
        raise Rejected(f"there is no ledger account {code}; open it, or name another in settings")
    return found[0].id
