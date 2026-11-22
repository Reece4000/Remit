"""A year of Kestrel's trading, made up the same way every time, for screens to show.

`play` starts where the month's setup leaves off and works through the year as Kestrel's
people and agents would, every change a command the kernel checks: the sourcing agent
loads the catalogue and the manufacturers' prices and keeps them current, and with the
export documents agent and the managing director records how each item is kept, bought,
flown, sold and booked; Tenders records each customer's and manufacturer's terms, rates
are recorded each Monday, Tenders quotes and wins or loses, deposits arrive, purchase
orders go out once a week, goods arrive with their paperwork, shipments are packed a
waybill for each temperature they travel at, booked, documented and dispatched, invoices
are raised and customers pay. Manufacturers' approvals and customers' import permits are
renewed as they run out, some permits a few days late; what is to go to a customer whose
permit has lapsed waits for the renewal. The kernel is given a clock that `play` moves
forward, so every document, event and audit entry carries the time it would have had.

Choices are drawn from one seeded random generator, in the order the work happens, so
the same volume, dates and seed make the same history. Work that falls due after the
end is left undone, which leaves the last weeks' documents in every state between draft
and paid. Exchange rates wander from the setup's rates and return to them by the end, so
that the month the seed then plays prices as `kestrel.month` expects.
"""

import heapq
import math
import random
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from typing import Any, Literal
from uuid import UUID

from remit import items, parties
from remit.kernel import Actor, Command, Document, Kernel, Money

from . import airfreight, fx, invoicing, pricelists, purchasing, sales, stock
from .catalogue import (
    AIRLINES,
    BUYERS,
    END_USERS,
    FORWARDERS,
    HAULIERS,
    MAKERS,
    PRODUCTS,
    PROGRAMMES,
    REQUIREMENTS,
    Buyer,
    Family,
    Maker,
    Product,
)
from .common import Incoterm, PaymentTerms, ShipmentMethod
from .month import PARTIES, RATES, SETUP, Organisation, Staff, describe, register, terms

V, E, P, R = Family.VACCINE, Family.ELISA, Family.PCR, Family.RAPID
G, C, Q, K = Family.REAGENT, Family.CONSUMABLE, Family.EQUIPMENT, Family.COLD_CHAIN

THEMES: tuple[tuple[tuple[Family, ...], int], ...] = (
    ((V,), 22),
    ((E,), 14),
    ((P,), 8),
    ((R,), 5),
    ((G,), 5),
    ((C,), 8),
    ((Q,), 10),
    ((K,), 6),
    ((E, G, C), 8),
    ((P, G, C), 6),
    ((Q, C), 4),
    ((V, K), 4),
)
"""What a tender asks for, and how often: one kind of product, or a laboratory's set."""

type Fate = Literal["won", "lost", "withdrawn", "unanswered"]
FATES: dict[Fate, int] = {"won": 42, "lost": 35, "withdrawn": 9, "unanswered": 14}
"""How tenders end, and how often: an unanswered one stays sent."""

_TICK = timedelta(seconds=40)
"""How long each command takes, so that no two share a moment."""

_DISEASES: tuple[tuple[str, str, tuple[sales.Species, ...], bool], ...] = (
    # A word in the product's name, the disease, the species, and whether its pathogen's
    # genetic material is export controlled.
    ("Newcastle", "Newcastle disease", (sales.Species.POULTRY,), True),
    ("Avian influenza", "Avian influenza", (sales.Species.POULTRY, sales.Species.DUCKS), True),
    ("bursal", "Infectious bursal disease", (sales.Species.POULTRY,), False),
    ("bronchitis", "Infectious bronchitis", (sales.Species.POULTRY,), False),
    ("Marek", "Marek's disease", (sales.Species.POULTRY,), False),
    ("Fowl", "Fowl pox and cholera", (sales.Species.POULTRY,), False),
    ("laryngotracheitis", "Infectious laryngotracheitis", (sales.Species.POULTRY,), False),
    ("Egg drop", "Egg drop syndrome", (sales.Species.POULTRY,), False),
    ("coryza", "Infectious coryza", (sales.Species.POULTRY,), False),
    ("Duck", "Duck plague", (sales.Species.DUCKS,), False),
    ("Mycoplasma g", "Avian mycoplasmosis", (sales.Species.POULTRY,), False),
    ("Mycoplasma s", "Avian mycoplasmosis", (sales.Species.POULTRY,), False),
    ("Salmonella", "Salmonellosis", (sales.Species.POULTRY, sales.Species.PIGS), False),
    (
        "Foot-and-mouth",
        "Foot-and-mouth disease",
        tuple(sales.Species)[:5],
        True,
    ),
    ("Peste", "Peste des petits ruminants", (sales.Species.SHEEP, sales.Species.GOATS), True),
    ("Lumpy", "Lumpy skin disease", (sales.Species.CATTLE, sales.Species.BUFFALO), True),
    ("Capripox", "Lumpy skin disease and pox", tuple(sales.Species)[:4], True),
    ("pox", "Sheep and goat pox", (sales.Species.SHEEP, sales.Species.GOATS), True),
    ("Orf", "Orf", (sales.Species.SHEEP, sales.Species.GOATS), False),
    ("pleuropneumonia", "Contagious caprine pleuropneumonia", (sales.Species.GOATS,), False),
    ("Anthrax", "Anthrax", tuple(sales.Species)[:4], False),
    ("Blackleg", "Blackleg", (sales.Species.CATTLE, sales.Species.SHEEP), False),
    (
        "septicaemia",
        "Haemorrhagic septicaemia",
        (sales.Species.CATTLE, sales.Species.BUFFALO),
        False,
    ),
    (
        "Brucella",
        "Brucellosis",
        (sales.Species.CATTLE, sales.Species.SHEEP, sales.Species.GOATS, sales.Species.CAMELS),
        False,
    ),
    (
        "Rift Valley",
        "Rift Valley fever",
        (sales.Species.CATTLE, sales.Species.SHEEP, sales.Species.GOATS, sales.Species.CAMELS),
        True,
    ),
    ("enterotoxaemia", "Clostridial disease", (sales.Species.SHEEP, sales.Species.GOATS), False),
    (
        "Bluetongue",
        "Bluetongue",
        (sales.Species.CATTLE, sales.Species.SHEEP, sales.Species.GOATS),
        False,
    ),
    ("African swine", "African swine fever", (sales.Species.PIGS,), True),
    ("Classical swine", "Classical swine fever", (sales.Species.PIGS,), True),
    ("PRRS", "PRRS", (sales.Species.PIGS,), False),
    ("ircovirus", "Porcine circovirus", (sales.Species.PIGS,), False),
    ("hyopneumoniae", "Enzootic pneumonia", (sales.Species.PIGS,), False),
    ("erysipelas", "Swine erysipelas", (sales.Species.PIGS,), False),
    ("Rabies", "Rabies", (sales.Species.DOGS, sales.Species.CATS), False),
    ("distemper", "Canine distemper", (sales.Species.DOGS,), False),
    ("Canine", "Canine parvovirus", (sales.Species.DOGS,), False),
    ("Lepto", "Leptospirosis", (sales.Species.CATTLE, sales.Species.PIGS), False),
    ("Paratuberculosis", "Johne's disease", (sales.Species.CATTLE, sales.Species.SHEEP), False),
    ("tuberc", "Tuberculosis", (sales.Species.CATTLE,), False),
    ("Bovine viral", "Bovine viral diarrhoea", (sales.Species.CATTLE,), False),
    ("IBR", "Infectious bovine rhinotracheitis", (sales.Species.CATTLE,), False),
    ("Q fever", "Q fever", (sales.Species.CATTLE, sales.Species.SHEEP, sales.Species.GOATS), True),
    ("Coxiella", "Q fever", (sales.Species.CATTLE, sales.Species.SHEEP, sales.Species.GOATS), True),
    (
        "Toxoplasma",
        "Toxoplasmosis",
        (sales.Species.SHEEP, sales.Species.GOATS, sales.Species.CATS),
        False,
    ),
    ("Neospora", "Neosporosis", (sales.Species.CATTLE, sales.Species.DOGS), False),
    ("Fasciola", "Liver fluke", (sales.Species.CATTLE, sales.Species.SHEEP), False),
)
"""What the vaccines and test kits are for, found by a word in their names, first match
first."""

_HAZARDS = {
    "UN1170": ("Ethanol solution", airfreight.PackingGroup.II),
    "UN1219": ("Isopropanol", airfreight.PackingGroup.II),
    "UN1230": ("Methanol", airfreight.PackingGroup.II),
    "UN1090": ("Acetone", airfreight.PackingGroup.II),
    "UN1307": ("Xylenes", airfreight.PackingGroup.III),
    "UN3334": ("Aviation regulated liquid, n.o.s. (formaldehyde solution)", None),
    "UN1791": ("Hypochlorite solution", airfreight.PackingGroup.III),
    "UN1789": ("Hydrochloric acid", airfreight.PackingGroup.II),
    "UN1823": ("Sodium hydroxide, solid", airfreight.PackingGroup.II),
    "UN2796": ("Sulphuric acid", airfreight.PackingGroup.II),
}
"""The proper shipping name and packing group of each UN number the catalogue carries."""

_CUSTOMERS = {
    "Veterinary Diagnostic Centre": sales.CustomerType.VETERINARY_LABORATORY,
    "Agrovet Trading Co.": sales.CustomerType.VETERINARY_DISTRIBUTOR,
    "Livestock Development Board": sales.CustomerType.VETERINARY_SERVICE,
    "Animal Health Supplies": sales.CustomerType.VETERINARY_DISTRIBUTOR,
    "Institute of Veterinary Science": sales.CustomerType.RESEARCH_INSTITUTE,
    "Poultry Health Cooperative": sales.CustomerType.COOPERATIVE,
    "Smallholder Livestock Programme": sales.CustomerType.DEVELOPMENT_PROGRAMME,
    "Laboratory Services": sales.CustomerType.COMMERCIAL_LABORATORY,
}
"""What kind of customer each kind of name belongs to."""

_CREDIT = {
    sales.CustomerType.VETERINARY_SERVICE: 600_000,
    sales.CustomerType.DEVELOPMENT_PROGRAMME: 500_000,
    sales.CustomerType.VETERINARY_DISTRIBUTOR: 300_000,
    sales.CustomerType.VETERINARY_LABORATORY: 250_000,
    sales.CustomerType.RESEARCH_INSTITUTE: 150_000,
    sales.CustomerType.COMMERCIAL_LABORATORY: 120_000,
    sales.CustomerType.COOPERATIVE: 80_000,
}
"""About how much credit each kind of customer is given, in its currency."""

_REVENUE = {
    Family.VACCINE: "4010",
    Family.ELISA: "4020",
    Family.PCR: "4020",
    Family.RAPID: "4020",
    Family.REAGENT: "4030",
    Family.CONSUMABLE: "4030",
    Family.EQUIPMENT: "4040",
    Family.COLD_CHAIN: "4040",
}
_GROUP = {
    Family.VACCINE: sales.ProductGroup.VACCINES,
    Family.ELISA: sales.ProductGroup.DIAGNOSTICS,
    Family.PCR: sales.ProductGroup.DIAGNOSTICS,
    Family.RAPID: sales.ProductGroup.DIAGNOSTICS,
    Family.REAGENT: sales.ProductGroup.REAGENTS,
    Family.CONSUMABLE: sales.ProductGroup.CONSUMABLES,
    Family.EQUIPMENT: sales.ProductGroup.EQUIPMENT,
    Family.COLD_CHAIN: sales.ProductGroup.COLD_CHAIN,
}


@dataclass(frozen=True)
class Volume:
    """How much history to make."""

    quotations: int
    customers: int
    products: int
    seed: int = 1907


FULL = Volume(quotations=220, customers=len(BUYERS), products=len(PRODUCTS))
"""The seed's year: 220 quotations to 80 customers, over the whole catalogue."""


class Clock:
    """The time the kernel records, which `play` moves forward as it works."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True)
class _Plan:
    """A tender to come: who it is for, how it will end, and who pays."""

    buyer: Buyer
    fate: Fate
    thin: bool
    """Whether it is priced so close to cost that it needs approval."""
    programme: str | None


@dataclass
class _Deal:
    """A quotation, followed until it is won, lost or withdrawn."""

    plan: _Plan
    quotation: sales.Quotation


@dataclass
class _Order:
    """A won quotation's order, followed through buying and shipping to payment."""

    plan: _Plan
    order: sales.SalesOrder
    lines: list[sales.SalesOrderLine]
    split: bool
    """Whether it ships in two consignments, the first as soon as half is in."""
    abandon: bool
    """Whether the customer cancels once everything has arrived."""
    stock: dict[UUID, list[tuple[UUID, int]]]
    """The batches received for each order line, and how many of each."""
    packed: set[UUID]
    """The order lines on a waybill."""
    shipping: bool = False
    waybills: int = 0
    """How many of its waybills are packed and not yet dispatched."""


@dataclass
class _Purchase:
    """A purchase order, followed until everything on it has arrived."""

    order: purchasing.PurchaseOrder
    maker: Maker
    lines: list[purchasing.PurchaseOrderLine]


def play(
    kernel: Kernel, staff: Staff, clock: Clock, start: date, end: date, volume: Volume
) -> None:
    """Make a history from `start`, the day the month's setup ran, to the day before `end`."""
    _History(kernel, staff, clock, start, end, volume).run()


class _History:
    def __init__(
        self, kernel: Kernel, staff: Staff, clock: Clock, start: date, end: date, volume: Volume
    ) -> None:
        self.kernel, self.staff, self.clock = kernel, staff, clock
        self.start, self.end = start, end
        self.rng = random.Random(volume.seed)  # noqa: S311  made-up history, not secrecy
        self.queue: list[tuple[datetime, int, Callable[[], None]]] = []
        self.scheduled = 0
        self.floor = kernel.settings(sales.SalesSettings).margin_floor
        self.limit = kernel.settings(purchasing.PurchasingSettings).approval_over
        every = max(1, len(PRODUCTS) // volume.products)
        self.products = PRODUCTS[::every][: volume.products]
        self.buyers = BUYERS[: volume.customers]
        self.makers_of: dict[str, list[Maker]] = {}
        self.range_of: dict[str, list[Product]] = {}
        self._assign()
        self.makers = [m for m in MAKERS if self.range_of.get(m.code)]
        self.discontinued = {p.code for i, p in enumerate(self.products) if i % 61 == 30}
        self.item: dict[str, UUID] = {}
        self.product_of: dict[UUID, Product] = {}
        self.party: dict[str, UUID] = {}
        self.maker_by_party: dict[UUID, Maker] = {}
        self.contacts: dict[str, list[UUID]] = {}
        self.addresses: dict[str, list[UUID]] = {}
        self.cost: dict[tuple[str, str], Decimal] = {}
        self.price: dict[tuple[str, str], pricelists.Price] = {}
        self.requirements: dict[str | None, list[airfreight.Requirement]] = {}
        self.details: dict[str, dict[str, object]] = {}
        self.credit: dict[str, Money | None] = {}
        self.permits: dict[str, date] = {}
        self.countries: dict[str, str] = {}
        self.kinds: dict[str, sales.CustomerType] = {}
        self.rates: dict[str, list[tuple[date, Decimal]]] = {}
        self.wanting: dict[str, list[tuple[_Order, sales.SalesOrderLine]]] = {}
        self.order_of_line: dict[UUID, _Order] = {}
        self.counter = 0
        self.serials = {prefix: self.rng.randint(1_000_000, 8_000_000) for _, prefix, _ in AIRLINES}
        self.priya, self.morgan = UUID(staff.tenders.id), UUID(staff.md.id)
        self.at(start, 9, self.set_up)
        plans = [self._plan_deal() for _ in range(volume.quotations)]
        span = (end - start).days
        days = sorted(self.rng.uniform(5, span - 2) for _ in plans)
        for plan, offset in zip(plans, days, strict=True):
            opens = start + timedelta(days=offset)
            self.at(opens, 9, partial(self.quote, plan), self.rng.randint(0, 150))

    # The calendar.

    @property
    def today(self) -> date:
        return self.clock.now.date()

    def at(self, day: date, hour: int, step: Callable[[], None], minute: int = 0) -> None:
        """Do `step` on a working day at a time, unless that falls after the end."""
        while day.weekday() >= 5:
            day += timedelta(days=1)
        if day >= self.end:
            return
        when = datetime.combine(day, time(hour), tzinfo=UTC) + timedelta(minutes=minute)
        heapq.heappush(self.queue, (when, self.scheduled, step))
        self.scheduled += 1

    def later(self, low: int, high: int, hour: int, step: Callable[[], None]) -> None:
        self.at(self.today + timedelta(days=self.rng.randint(low, high)), hour, step)

    def run(self) -> None:
        while self.queue:
            when, _, step = heapq.heappop(self.queue)
            self.clock.now = max(self.clock.now, when)
            step()

    def do[T](self, actor: Actor, command: Command[T]) -> T:
        self.clock.now += _TICK
        return self.kernel.execute(command, actor=actor).result

    def amend[T](
        self, actor: Actor, kind: type[Command[T]], document: Document, **given: object
    ) -> T:
        """Amend a document as a client does: every field as it holds it now, but those given."""
        named = kind.amends
        if named is None:
            raise TypeError(f"{kind.__name__} amends nothing")
        current = self.latest(document)
        held = {f: getattr(current, f) for f in kind.model_fields if f not in (named, "version")}
        fields = held | given | {named: current.id, "version": current.version}
        return self.do(actor, kind.model_validate(fields))

    def latest[D: Document](self, document: D) -> D:
        return self.kernel.get(type(document), document.id, actor=SETUP)

    def amend_extension(
        self,
        actor: Actor,
        kind: type[Command[Any]],
        host: type[Document],
        host_id: UUID,
        **given: object,
    ) -> None:
        """Amend what an extension adds to a document as a client does: every field as the
        document holds it now, but those given."""
        current = self.kernel.get(self.kernel.registry.extended(host), host_id, actor=SETUP)
        named = str(kind.amends)
        held = {f: getattr(current, f) for f in kind.model_fields if f not in (named, "version")}
        self.do(
            actor, kind.model_validate(held | given | {named: host_id, "version": current.version})
        )

    def number(self) -> int:
        self.counter += 1
        return self.counter

    # What the year starts with.

    def _assign(self) -> None:
        """Share each family's products among the manufacturers making it, by their share;
        one in seven is made by a second manufacturer too."""
        for family in Family:
            makers = [m for m in MAKERS if family in m.families]
            turns = [m for m in makers for _ in range(m.share)]
            products = [p for p in self.products if p.family is family]
            for index, product in enumerate(products):
                first = turns[index % len(turns)]
                offered = [first]
                if index % 7 == 3 and len(makers) > 1:
                    offered.append(makers[(makers.index(first) + 1) % len(makers)])
                self.makers_of[product.code] = offered
                for maker in offered:
                    self.range_of.setdefault(maker.code, []).append(product)

    def _plan_deal(self) -> _Plan:
        weights = [3 if index < 14 else 1 for index in range(len(self.buyers))]
        (buyer,) = self.rng.choices(self.buyers, weights=weights)
        (fate,) = self.rng.choices(list(FATES), list(FATES.values()))
        thin = self.rng.random() < 0.1
        programme = self.rng.choice(PROGRAMMES) if self.rng.random() < 0.25 else None
        return _Plan(buyer, fate, thin, programme)

    def set_up(self) -> None:
        """Register the customers and manufacturers, add the catalogue, price it, record
        each destination's requirements, and put the year's routine in the diary."""
        self._diary()
        known = {organisation.code for organisation in PARTIES}
        for organisation in [m.organisation for m in self.makers] + [
            b.organisation for b in self.buyers
        ]:
            self._register(organisation, known)
        for maker in self.makers:
            self.maker_by_party[self.party[maker.code]] = maker
        self._terms(known)
        for product in self.products:
            added = self.do(
                self.staff.sourcing,
                items.AddItem(
                    code=product.code,
                    kind=items.ItemKind.GOODS,
                    name=product.name,
                    unit=product.unit,
                    description=product.description,
                ),
            )
            self.item[product.code] = added.id
            self.product_of[added.id] = product
            for rank, maker in enumerate(self.makers_of[product.code]):
                premium = Decimal(1) + Decimal(self.rng.randint(-4, 9)) / 100 * rank
                cost = _nice(product.cost * premium / self.rate(maker.currency, self.start))
                self._price(maker, product, cost, "Price list", self.rng.randint(*maker.lead_time))
            maker = self.makers_of[product.code][0]
            details = _details(self.rng, product, maker) | {"supplier_id": self.party[maker.code]}
            self.details[product.code] = details
            describe(self.kernel, self.staff, added, details)
            self.clock.now += _TICK
        countries = dict.fromkeys(b.organisation.country for b in self.buyers)
        for country in countries:
            for document, note in REQUIREMENTS.get(country, ()):
                self.do(
                    self.staff.logistics,
                    airfreight.AddRequirement(country=country, document=document, note=note),
                )
        for requirement in self.kernel.page(airfreight.Requirement, actor=SETUP, limit=200).items:
            self.requirements.setdefault(requirement.country, []).append(requirement)

    def _terms(self, known: set[str]) -> None:
        """Record each manufacturer's terms and approval, and each customer's terms, as
        Tenders and the managing director would; and put in the diary the approvals and
        import permits to renew as they run out, the month's parties' among them."""
        for code, details in terms(self.start, self.priya).items():
            until, expires = details.get("approved_until"), details.get("import_permit_expires")
            limit, kind = details.get("credit_limit"), details.get("customer_type")
            if isinstance(until, date):
                self._renew_approval(code, until)
            if isinstance(expires, date):
                self._renew_permit(code, expires)
            self.credit[code] = limit if isinstance(limit, Money) else None
            if isinstance(kind, sales.CustomerType):
                self.kinds[code] = kind
        for number, maker in enumerate(self.makers, start=1):
            if maker.code in known:
                continue
            until = self.start + timedelta(days=self.rng.randint(120, 480))
            supplied = {
                "supplier_currency": maker.currency,
                "supplier_payment_terms": PaymentTerms.NET_60
                if self.rng.random() < 0.3
                else PaymentTerms.NET_30,
                "supplier_incoterm": Incoterm.DAP
                if maker.organisation.country == "GB"
                else Incoterm.CPT,
                "supplier_named_place": "Kestrel Animal Health, Reading",
                "lead_time_days": sum(maker.lead_time) // 2,
                "minimum_order_value": Money.of(
                    self.rng.choice((250, 500, 1000, 2500)), maker.currency
                ),
                "account_number": f"KES{self.rng.randint(1000, 9999)}",
                "ordering_email": maker.organisation.contacts[0].email,
                "approved_until": until,
                "approval_reference": f"GDP-QA-{self.start:%Y}-{number:03d}",
            }
            describe(self.kernel, self.staff, self._party(maker.code), supplied)
            self.clock.now += _TICK
            self._renew_approval(maker.code, until)
        for buyer in self.buyers:
            if buyer.code in known:
                continue
            kind = next(
                t for words, t in _CUSTOMERS.items() if buyer.organisation.name.endswith(words)
            )
            credit = None
            if buyer.terms.days:
                # Between 60% and 140% of what its kind is usually given, in tens of thousands.
                scaled = _CREDIT[kind] * self.rng.randint(6, 14) // 100_000 * 10_000
                credit = Money.of(scaled, buyer.currency)
            expires = self.start + timedelta(days=self.rng.randint(40, 420))
            customer = {
                "customer_type": kind,
                "customer_currency": buyer.currency,
                "customer_payment_terms": buyer.terms,
                "credit_limit": credit,
                "customer_incoterm": Incoterm.CIP,
                "customer_named_place": f"{buyer.city} airport ({buyer.airport})",
                "destination_airport": buyer.airport,
                "salesperson_id": self.rng.choice((self.priya, self.priya, self.morgan)),
                "import_permit": _permit(self.rng, buyer.organisation.country, self.start),
                "import_permit_expires": expires,
                "documents_required": tuple(
                    self.rng.sample(tuple(sales.CustomerDocument), self.rng.choice((0, 1, 1, 2)))
                ),
                "vendor_number": f"V-{self.rng.randint(10000, 99999)}"
                if self.rng.random() < 0.5
                else None,
            }
            describe(self.kernel, self.staff, self._party(buyer.code), customer)
            self.clock.now += _TICK
            self.credit[buyer.code] = credit
            self.kinds[buyer.code] = kind
            self._renew_permit(buyer.code, expires)

    def _party(self, code: str) -> parties.Party:
        return self.kernel.get(parties.Party, self.party[code], actor=SETUP)

    def _renew_approval(self, code: str, until: date) -> None:
        """Have the managing director renew a manufacturer's approval a little before it runs
        out, after its review, for another year."""
        renewed = until + timedelta(days=365)
        self.at(
            until - timedelta(days=self.rng.randint(3, 20)),
            9,
            partial(self.renew_approval, code, renewed),
        )

    def _renew_permit(self, code: str, expires: date) -> None:
        """Have Tenders record a customer's renewed import permit around when the old one
        runs out: mostly before, sometimes a few days after."""
        self.permits[code] = expires
        arrives = expires + timedelta(days=self.rng.randint(-25, 12))
        self.at(arrives, 10, partial(self.renew_permit, code, expires + timedelta(days=365)))

    def renew_approval(self, code: str, until: date) -> None:
        self.amend_extension(
            self.staff.md,
            purchasing.ApproveSupplier,
            parties.Party,
            self.party[code],
            approved_until=until,
            approval_reference=f"GDP-QA-{self.today:%Y}-{self.number():03d}",
        )
        self._renew_approval(code, until)

    def renew_permit(self, code: str, expires: date) -> None:
        country = self.countries[code]
        self.amend_extension(
            self.staff.tenders,
            sales.AmendCustomerTerms,
            parties.Party,
            self.party[code],
            import_permit=_permit(self.rng, country, self.today),
            import_permit_expires=expires,
        )
        self._renew_permit(code, expires)

    def _register(self, organisation: Organisation, known: set[str]) -> None:
        if organisation.code in known:
            party = self.kernel.page(
                parties.Party, actor=SETUP, where={"code": organisation.code}
            ).items[0]
        else:
            party = register(self.kernel, self.staff.tenders, organisation)
            self.clock.now += _TICK
        where = {"party_id": party.id}
        self.party[organisation.code] = party.id
        self.countries[organisation.code] = organisation.country
        self.contacts[organisation.code] = [
            c.id for c in self.kernel.page(parties.Contact, actor=SETUP, where=where).items
        ]
        self.addresses[organisation.code] = [
            a.id for a in self.kernel.page(parties.Address, actor=SETUP, where=where).items
        ]

    def _diary(self) -> None:
        """Rates each Monday, purchase orders each Tuesday, expired stock written off on
        each month's last working day, three rounds of new price lists, and the
        discontinued products withdrawn mid-year."""
        mondays = [
            self.start + timedelta(days=d)
            for d in range(1, (self.end - self.start).days)
            if (self.start + timedelta(days=d)).weekday() == 0
        ]
        for currency, opening in RATES.items():
            steps = [self.rng.gauss(0, 0.007) for _ in mondays]
            walked = [sum(steps[: k + 1]) for k in range(len(steps))]
            rates = [(self.start, Decimal(opening))]
            for k, monday in enumerate(mondays):
                drift = walked[k] - walked[-1] * (k + 1) / len(mondays)
                rate = (Decimal(opening) * Decimal(math.exp(drift))).quantize(Decimal("0.0001"))
                rates.append((monday, rate))
            self.rates[currency] = rates
        for monday in mondays:
            self.at(monday, 8, partial(self.record_rates, monday))
        for d in range(1, (self.end - self.start).days):
            day = self.start + timedelta(days=d)
            if day.weekday() == 1:
                self.at(day, 10, self.buy)
        for round_ in range(3):
            day = self.start + timedelta(days=91 * (round_ + 1))
            self.at(day, 8, partial(self.reprice, round_), 30)
        self.at(self.start + timedelta(days=150), 8, self.withdraw, 45)
        month = self.start.replace(day=1)
        while month < self.end:
            following = (month + timedelta(days=31)).replace(day=1)
            last = following - timedelta(days=1)
            while last.weekday() >= 5:
                last -= timedelta(days=1)
            self.at(last, 16, self.month_end)
            month = following

    def _price(
        self, maker: Maker, product: Product, cost: Decimal, source: str, lead_time: int
    ) -> None:
        price = self.do(
            self.staff.sourcing,
            pricelists.RecordPrice(
                supplier_id=self.party[maker.code],
                item_id=self.item[product.code],
                cost=Money.of(cost, maker.currency),
                effective=self.today,
                reference=f"{maker.code[2:5]}-{product.code.split('-', 1)[1]}",
                source=source,
                lead_time_days=lead_time,
            ),
        )
        self.cost[maker.code, product.code] = cost
        self.price[maker.code, product.code] = price

    def rate(self, currency: str, on: date) -> Decimal:
        """Pounds per unit on a day, as fx will find it."""
        if currency == "GBP":
            return Decimal(1)
        recorded = self.rates[currency]
        return recorded[bisect_right([d for d, _ in recorded], on) - 1][1]

    # The routine.

    def record_rates(self, monday: date) -> None:
        for currency, recorded in self.rates.items():
            rate = dict(recorded)[monday]
            self.do(self.staff.md, fx.RecordRate(currency=currency, effective=monday, rate=rate))

    def reprice(self, round_: int) -> None:
        """A third of the manufacturers send new lists, changing about a third of prices."""
        for maker in self.makers[round_::3]:
            for product in self.range_of[maker.code]:
                if product.code in self.discontinued or self.rng.random() > 0.35:
                    continue
                old = self.cost[maker.code, product.code]
                new = _nice(old * Decimal(self.rng.randint(97, 107)) / 100)
                if new != old:
                    lead = self.price[maker.code, product.code].lead_time_days or 14
                    self._price(maker, product, new, "Price list update", lead)

    def withdraw(self) -> None:
        """Discontinued products' prices are withdrawn, and the products blocked for sale
        and purchase."""
        for (_, code), price in self.price.items():
            if code in self.discontinued:
                self.do(self.staff.sourcing, pricelists.WithdrawPrice(price_id=price.id))
        for code in sorted(self.discontinued):
            for kind, field_name in (
                (sales.AmendItemSales, "blocked_for_sale"),
                (purchasing.AmendItemPurchasing, "blocked_for_purchase"),
            ):
                self.amend_extension(
                    self.staff.sourcing, kind, items.Item, self.item[code], **{field_name: True}
                )

    # Quotations.

    def quote(self, plan: _Plan) -> None:
        """Open a quotation, price its lines, then add carriage and insurance to it."""
        buyer, day = plan.buyer, self.today
        planned = self._plan(buyer)
        (incoterm,) = self.rng.choices(
            (Incoterm.CIP, Incoterm.CPT, Incoterm.FCA, Incoterm.DAP, Incoterm.EXW),
            (55, 15, 12, 12, 6),
        )
        named_place = {
            Incoterm.FCA: "London, Heathrow airport (LHR)",
            Incoterm.EXW: "Kestrel Animal Health, Reading",
        }.get(incoterm, f"{buyer.city} airport ({buyer.airport})")
        short = buyer.code[2:6]
        quotation = self.do(
            self.staff.tenders,
            sales.OpenQuotation(
                customer_id=self.party[buyer.code],
                destination=buyer.organisation.country,
                quoted=day,
                valid_until=day + timedelta(days=self.rng.choice((30, 45, 60))),
                requested_delivery=day + timedelta(days=self.rng.randint(45, 120)),
                contact_id=self.contacts[buyer.code][0],
                bill_to_id=self.addresses[buyer.code][0],
                ship_to_id=self.addresses[buyer.code][-1],
                salesperson_id=self.rng.choice((self.priya, self.priya, self.priya, self.morgan)),
                incoterm=incoterm,
                named_place=named_place,
                cold_chain=any(p.storage is not stock.Storage.AMBIENT for _, p, _ in planned),
                tender=f"{short}/T/{day:%Y}/{self.number():03d}"
                if self.rng.random() < 0.7
                and all(self.details[p.code]["tender_eligible"] for _, p, _ in planned)
                else None,
                programme=plan.programme,
                end_user=self.rng.choice(END_USERS) if self.rng.random() < 0.3 else None,
                notes="Prices include export packing and temperature loggers."
                if self.rng.random() < 0.2
                else None,
            ),
        )
        markup = (1.03, 1.11) if plan.thin else (1.17, 1.55)
        for maker, product, quantity in planned:
            cost = self.cost[maker.code, product.code] * self.rate(maker.currency, day)
            factor = Decimal(str(round(self.rng.uniform(*markup), 3)))
            price = _nice(cost * factor / self.rate(buyer.currency, day))
            discount = (
                Decimal(self.rng.choice(("2.5", "5", "7.5", "10")))
                if not plan.thin and self.rng.random() < 0.15
                else Decimal(0)
            )
            self.do(
                self.staff.tenders,
                sales.AddQuotationLine(
                    quotation_id=quotation.id,
                    item_id=self.item[product.code],
                    quantity=quantity,
                    unit_price=Money.of(price, buyer.currency),
                    discount_percent=discount,
                    supplier_id=None
                    if maker is self.makers_of[product.code][0]
                    else self.party[maker.code],
                ),
            )
        weight = sum((p.weight * q for _, p, q in planned), Decimal(0))
        carried = incoterm in (Incoterm.CIP, Incoterm.CPT, Incoterm.DAP)
        if carried or weight < 15:
            quotation = self.latest(quotation)
            freight = max(Decimal(180), weight * Decimal("6.2")) / self.rate(buyer.currency, day)
            insurance = quotation.net.amount * Decimal("0.00385")
            self.amend(
                self.staff.tenders,
                sales.AmendQuotation,
                quotation,
                freight=Money.of(_nice(freight) if carried else 0, buyer.currency),
                insurance=Money.of(
                    _nice(max(insurance, Decimal(25))) if incoterm is Incoterm.CIP else 0,
                    buyer.currency,
                ),
                shipment_method=ShipmentMethod.COURIER if weight < 15 else ShipmentMethod.AIR,
            )
        deal = _Deal(plan, quotation)
        if self.rng.random() < 0.15:
            self.later(1, 1, 11, partial(self.revise, deal))
        if plan.fate == "withdrawn" and self.rng.random() < 0.25:
            self.later(3, 8, 16, partial(self.withdraw_quotation, deal))
        else:
            self.later(2, 6, 14, partial(self.send, deal))

    def _plan(self, buyer: Buyer) -> list[tuple[Maker, Product, int]]:
        """What a tender asks for: one kind of product, or a laboratory's set of a few, each
        from one manufacturer of it, and from one already quoting for the set if it can.
        A customer not authorised to receive veterinary medicines is not offered them."""
        medicines = self.kinds[buyer.code].receives_medicines
        planned: list[tuple[Maker, Product, int]] = []
        while not planned:
            (families,) = self.rng.choices([t for t, _ in THEMES], [w for _, w in THEMES])
            (count,) = self.rng.choices(
                range(1, 21), [9, 9, 9, 8, 8, 7, 6, 5, 4, 4] + [2] * 6 + [1] * 4
            )
            for rank, family in enumerate(families):
                offered = {m.code: self._offered(m, family, medicines) for m in self.makers}
                makers = [m for m in self.makers if offered[m.code]]
                quoting = [m for m in dict.fromkeys(m for m, _, _ in planned) if offered[m.code]]
                if quoting:
                    maker = quoting[0]
                elif makers:
                    (maker,) = self.rng.choices(makers, [m.share for m in makers])
                else:
                    continue
                share = count if len(families) == 1 else max(1, count // (2 if rank == 0 else 4))
                chosen = self.rng.sample(offered[maker.code], min(share, len(offered[maker.code])))
                planned += [(maker, p, _quantity(self.rng, p)) for p in chosen]
        return planned

    def _offered(self, maker: Maker, family: Family, medicines: bool) -> list[Product]:
        return [
            p
            for p in self.range_of[maker.code]
            if p.family is family
            and p.code not in self.discontinued
            and (medicines or not self.details[p.code].get("veterinary_only"))
        ]

    def revise(self, deal: _Deal) -> None:
        """Before sending, change a line's quantity or discount, or take one off."""
        lines = self.kernel.page(
            sales.QuotationLine,
            actor=SETUP,
            where={"quotation_id": deal.quotation.id, "state": "current"},
        ).items
        line = self.rng.choice(lines)
        if len(lines) >= 3 and self.rng.random() < 0.3:
            self.do(self.staff.tenders, sales.RemoveQuotationLine(quotation_line_id=line.id))
        elif self.rng.random() < 0.5:
            self.amend(
                self.staff.tenders, sales.AmendQuotationLine, line, quantity=line.quantity * 2
            )
        else:
            self.amend(
                self.staff.tenders,
                sales.AmendQuotationLine,
                line,
                discount_percent=line.discount_percent + 5,
                notes="Discount agreed for the full quantity.",
            )

    def send(self, deal: _Deal) -> None:
        """Send it, once the managing director has approved it if it earns too little or
        comes to more on credit than the customer's limit."""
        quotation = self.latest(deal.quotation)
        # Held to the floor unrounded, as sales holds it, not by the rounded percentage.
        revenue = quotation.cost + quotation.margin
        thin = revenue.is_zero or quotation.margin.amount / revenue.amount < self.floor
        limit = self.credit.get(deal.plan.buyer.code)
        over = limit is not None and quotation.payment_terms.days > 0 and quotation.total > limit
        if quotation.state == "draft" and (thin or over):
            self.later(1, 2, 10, partial(self.approve, deal))
            return
        deal.quotation = self.do(
            self.staff.tenders, sales.SendQuotation(quotation_id=quotation.id, sent=self.today)
        )
        follow = {
            "won": (7, 35, self.win),
            "lost": (10, 40, self.lose),
            "withdrawn": (5, 30, self.withdraw_quotation),
        }.get(deal.plan.fate)
        if follow:
            low, high, step = follow
            self.later(low, high, 11, partial(step, deal))

    def approve(self, deal: _Deal) -> None:
        self.do(self.staff.md, sales.ApproveQuotation(quotation_id=deal.quotation.id))
        if self.rng.random() < 0.25:
            # Tenders reopens it to raise a price, and it is approved again.
            self.do(self.staff.tenders, sales.ReopenQuotation(quotation_id=deal.quotation.id))
            line = self.kernel.page(
                sales.QuotationLine,
                actor=SETUP,
                where={"quotation_id": deal.quotation.id, "state": "current"},
            ).items[0]
            raised = _nice(line.unit_price.amount * Decimal("1.04"))
            if raised != line.unit_price.amount:
                self.amend(
                    self.staff.tenders,
                    sales.AmendQuotationLine,
                    line,
                    unit_price=Money.of(raised, line.unit_price.currency),
                )
            self.do(self.staff.md, sales.ApproveQuotation(quotation_id=deal.quotation.id))
        self.later(0, 1, 15, partial(self.send, deal))

    def lose(self, deal: _Deal) -> None:
        (reason,) = self.rng.choices(tuple(sales.LossReason), (50, 20, 15, 15))
        self.do(
            self.staff.tenders,
            sales.LoseQuotation(quotation_id=deal.quotation.id, reason=reason),
        )

    def withdraw_quotation(self, deal: _Deal) -> None:
        quotation = self.latest(deal.quotation)
        if quotation.state in ("draft", "approved", "sent"):
            self.do(self.staff.tenders, sales.WithdrawQuotation(quotation_id=quotation.id))

    # Orders.

    def win(self, deal: _Deal) -> None:
        plan, day = deal.plan, self.today
        buyer = plan.buyer
        deposit = Decimal(0)
        if plan.programme and self.rng.random() < 0.7:
            deposit = Decimal("0.3")
        elif buyer.terms.value == "advance":
            deposit = Decimal("0.5")
        reference = (
            f"{buyer.code[2:6]}-PO-{day:%Y}-{self.number():04d}"
            if self.rng.random() < 0.7
            else None
        )
        order = self.do(
            self.staff.tenders,
            sales.WinQuotation(
                quotation_id=deal.quotation.id,
                deposit=deposit,
                customer_po=reference,
                customer_po_date=day - timedelta(days=self.rng.randint(0, 5))
                if reference
                else None,
                ordered=day,
            ),
        )
        lines = list(
            self.kernel.page(
                sales.SalesOrderLine, actor=SETUP, where={"sales_order_id": order.id}, limit=50
            ).items
        )
        placed = _Order(
            plan,
            order,
            lines,
            split=len(lines) >= 4 and self.rng.random() < 0.2,
            abandon=not deposit and self.rng.random() < 0.03,
            stock={},
            packed=set(),
        )
        for line in lines:
            self.order_of_line[line.id] = placed
        if not deposit and not placed.abandon and self.rng.random() < 0.04:
            self.later(2, 6, 11, partial(self.cancel, placed))
            return
        if order.deposit_due:
            self.later(3, 12, 10, partial(self.deposit, placed, order.deposit_due))
        else:
            self.ready(placed)
        if reference is None:
            late = f"{buyer.code[2:6]}-LPO-{self.number():05d}"
            self.later(2, 8, 14, partial(self.amend_order, placed, late))
        if self.rng.random() < 0.08:
            self.later(1, 10, 15, partial(self.repromise, placed))

    def ready(self, placed: _Order) -> None:
        """Hand the order's lines to purchasing, by manufacturer."""
        for line in placed.lines:
            maker = self.maker_by_party[line.supplier_id]
            self.wanting.setdefault(maker.code, []).append((placed, line))

    def deposit(self, placed: _Order, due: Money) -> None:
        self.do(
            self.staff.tenders,
            sales.RecordDeposit(sales_order_id=placed.order.id, amount=due, received=self.today),
        )
        self.ready(placed)

    def amend_order(self, placed: _Order, reference: str) -> None:
        """The customer's purchase order number arrives after the order."""
        order = self.latest(placed.order)
        if order.state == "open":
            self.amend(
                self.staff.tenders,
                sales.AmendSalesOrder,
                order,
                customer_po=reference,
                customer_po_date=self.today,
            )

    def repromise(self, placed: _Order) -> None:
        """A manufacturer's delay moves a line's promised delivery."""
        line = self.latest(self.rng.choice(placed.lines))
        if line.state == "open" and line.promised_delivery is not None:
            self.amend(
                self.staff.tenders,
                sales.AmendSalesOrderLine,
                line,
                promised_delivery=line.promised_delivery + timedelta(days=14),
                notes="Manufacturer's delivery delayed by two weeks.",
            )

    def cancel(self, placed: _Order) -> None:
        self.do(self.staff.md, sales.CancelOrder(sales_order_id=placed.order.id))

    # Purchasing.

    def buy(self) -> None:
        """Raise purchase orders for everything waiting, one to each manufacturer, but one
        for each order a programme pays for."""
        for maker in self.makers:
            wanted = self.wanting.pop(maker.code, [])
            groups: dict[str, list[tuple[_Order, sales.SalesOrderLine]]] = {}
            for placed, line in wanted:
                key = placed.order.code if placed.plan.programme else ""
                groups.setdefault(key, []).append((placed, line))
            for group in groups.values():
                self._purchase(maker, group)

    def _purchase(self, maker: Maker, group: list[tuple[_Order, sales.SalesOrderLine]]) -> None:
        orders = {placed.order.code: placed.order for placed, _ in group}
        alone = next(iter(orders.values())) if len(orders) == 1 else None
        lead = max(
            self.price[maker.code, self.product_of[line.item_id].code].lead_time_days or 14
            for _, line in group
        )
        order = self.do(
            self.staff.tenders,
            purchasing.RaisePurchaseOrder(
                supplier_id=self.party[maker.code],
                sales_order_id=alone.id if alone else None,
                contact_id=self.contacts[maker.code][0],
                buyer_id=self.priya,
                ordered=self.today,
                expected=self.today + timedelta(days=lead),
                shipment_method=ShipmentMethod.ROAD
                if maker.organisation.country in ("GB", "DE", "NL", "FR", "BE", "DK", "AT")
                else ShipmentMethod.AIR,
                freight=Money.of(_nice(Decimal(self.rng.randint(45, 380))), maker.currency)
                if self.rng.random() < 0.2
                else None,
            ),
        )
        lines = [
            self.do(
                self.staff.tenders,
                purchasing.AddPurchaseOrderLine(
                    purchase_order_id=order.id, sales_order_line_id=line.id
                ),
            )
            for _, line in group
        ]
        if self.rng.random() < 0.1:
            lines[0] = self.amend(
                self.staff.tenders,
                purchasing.AmendPurchaseOrderLine,
                lines[0],
                discount_percent=Decimal(3),
                notes="Volume discount agreed by email.",
            )
        purchase = _Purchase(self.latest(order), maker, lines)
        if self.rng.random() < 0.02:
            self.do(self.staff.tenders, purchasing.CancelPurchaseOrder(purchase_order_id=order.id))
            self.wanting.setdefault(maker.code, []).extend(group)
            return
        if purchase.order.total_base > self.limit:
            self.later(1, 1, 9, partial(self.approve_purchase, purchase))
        else:
            self.later(0, 1, 15, partial(self.send_purchase, purchase))

    def approve_purchase(self, purchase: _Purchase) -> None:
        self.do(self.staff.md, purchasing.ApprovePurchaseOrder(purchase_order_id=purchase.order.id))
        self.at(self.today, 11, partial(self.send_purchase, purchase))

    def send_purchase(self, purchase: _Purchase) -> None:
        purchase.order = self.do(
            self.staff.tenders, purchasing.SendPurchaseOrder(purchase_order_id=purchase.order.id)
        )
        if self.rng.random() < 0.85:
            self.later(1, 4, 12, partial(self.confirm, purchase))
        lead = (purchase.order.expected or self.today) - purchase.order.ordered
        arrives = self.today + lead + timedelta(days=self.rng.randint(-2, 5))
        everything = [(line, line.quantity) for line in purchase.lines]
        split = [i for i, line in enumerate(purchase.lines) if line.quantity >= 4]
        if split and self.rng.random() < 0.15:
            index = self.rng.choice(split)
            first = purchase.lines[index].quantity * 3 // 5
            early = [*everything[:index], (purchase.lines[index], first), *everything[index + 1 :]]
            self.at(arrives, 11, partial(self.receive, purchase, early))
            rest = [(purchase.lines[index], purchase.lines[index].quantity - first)]
            self.at(
                arrives + timedelta(days=self.rng.randint(5, 12)),
                11,
                partial(self.receive, purchase, rest),
            )
        else:
            self.at(arrives, 11, partial(self.receive, purchase, everything))

    def confirm(self, purchase: _Purchase) -> None:
        order = self.latest(purchase.order)
        if order.state != "sent":
            return
        prefix = purchase.maker.code[2:5]
        self.do(
            self.staff.tenders,
            purchasing.ConfirmPurchaseOrder(
                purchase_order_id=order.id,
                supplier_reference=f"{prefix}-{self.today:%y}-{self.number():05d}",
                confirmed=self.today,
            ),
        )
        if self.rng.random() < 0.12 and order.expected is not None:
            self.later(1, 5, 16, partial(self.delay, purchase))

    def delay(self, purchase: _Purchase) -> None:
        """The manufacturer moves its delivery date."""
        order = self.latest(purchase.order)
        if order.state in ("sent", "confirmed") and order.expected is not None:
            self.amend(
                self.staff.tenders,
                purchasing.AmendPurchaseOrder,
                order,
                expected=order.expected + timedelta(days=7),
                notes="Delivery moved by a week at the manufacturer's request.",
            )

    def receive(
        self, purchase: _Purchase, delivered: list[tuple[purchasing.PurchaseOrderLine, int]]
    ) -> None:
        """Receive a delivery: each line as a batch, a batch shared by lines of one item."""
        maker, day = purchase.maker, self.today
        note = f"{maker.code[2:5]}-DN-{self.number():06d}"
        batches: dict[tuple[UUID, Decimal, Decimal], tuple[str, date]] = {}
        touched: list[_Order] = []
        for line, quantity in delivered:
            product = self.product_of[line.item_id]
            key = (line.item_id, line.unit_cost.amount, line.discount_percent)
            if key not in batches:
                made = day - timedelta(days=self.rng.randint(20, 90))
                batches[key] = (f"{maker.code[2:4]}{day:%y%m}-{self.number():04d}", made)
            batch, made = batches[key]
            cold = product.storage is not stock.Storage.AMBIENT
            receipt = self.do(
                self.staff.logistics,
                purchasing.ReceiveGoods(
                    purchase_order_line_id=line.id,
                    quantity=quantity,
                    batch=batch,
                    expiry=made + timedelta(days=product.shelf_life)
                    if product.shelf_life
                    else None,
                    temperature=_temperature(self.rng, product.storage),
                    received=day,
                    manufactured=made if product.shelf_life else None,
                    certificate=f"COA-{batch}" if product.shelf_life else None,
                    delivery_note=note,
                    carrier=self.rng.choice(HAULIERS),
                    packages=max(1, min(10_000, math.ceil(product.weight * quantity / 12))),
                    data_logger=f"TL-{self.rng.randint(10, 99)}-{self.rng.randint(10000, 99999)}"
                    if cold
                    else None,
                ),
            )
            placed = self.order_of_line[line.sales_order_line_id]
            received = placed.stock.setdefault(line.sales_order_line_id, [])
            received.append((receipt.batch_id, quantity))
            if placed not in touched:
                touched.append(placed)
            if self.rng.random() < 0.08:
                self.later(1, 6, 15, partial(self.move_batch, receipt.batch_id))
        for placed in touched:
            self.consider_shipping(placed)

    def move_batch(self, batch_id: UUID) -> None:
        batch = self.kernel.get(stock.Batch, batch_id, actor=SETUP)
        moved = _bin(self.rng, self.product_of[batch.item_id])
        if batch.state == "in_stock" and moved != batch.location:
            self.amend(self.staff.logistics, stock.AmendBatch, batch, location=moved)

    # Shipping.

    def consider_shipping(self, placed: _Order) -> None:
        """Ship what has arrived once all of it has, or half of it for a split order."""
        if placed.shipping:
            return
        remaining = [line for line in placed.lines if line.id not in placed.packed]
        ready = [
            line
            for line in remaining
            if sum(q for _, q in placed.stock.get(line.id, [])) >= line.quantity
        ]
        if not ready:
            return
        if len(ready) == len(remaining) and placed.abandon and not placed.packed:
            placed.shipping = True
            self.later(2, 5, 11, partial(self.abandon, placed))
        elif len(ready) == len(remaining) or (
            placed.split and not placed.packed and len(ready) * 2 >= len(remaining)
        ):
            placed.shipping = True
            self.later(1, 3, 9, partial(self.pack, placed, ready))

    def abandon(self, placed: _Order) -> None:
        """The customer cancels after the goods arrived; what is chilled is later found to
        have warmed in the store, and written off."""
        self.do(self.staff.md, sales.CancelOrder(sales_order_id=placed.order.id))
        for line in placed.lines:
            product = self.product_of[line.item_id]
            if product.storage is stock.Storage.CHILLED:
                for batch_id, _ in placed.stock[line.id]:
                    self.later(14, 28, 10, partial(self.write_off, batch_id))

    def write_off(self, batch_id: UUID) -> None:
        batch = self.kernel.get(stock.Batch, batch_id, actor=SETUP)
        if batch.state != "in_stock" or batch.allocated:
            return
        self.amend(
            self.staff.logistics,
            stock.AmendBatch,
            batch,
            notes="Quarantined: the cold room logger shows 14 °C for six hours.",
        )
        self.do(self.staff.logistics, stock.WriteOffBatch(batch_id=batch.id, on=self.today))

    def month_end(self) -> None:
        """Write off what is in stock past its expiry, as the month closes."""
        expired = self.kernel.page(
            stock.Batch,
            actor=SETUP,
            where={"state": "in_stock", "expiry": {"lt": self.today}},
            limit=200,
        ).items
        for batch in expired:
            if not batch.allocated:
                self.do(self.staff.logistics, stock.WriteOffBatch(batch_id=batch.id, on=self.today))

    def pack(self, placed: _Order, lines: list[sales.SalesOrderLine]) -> None:
        """Pack what has arrived, a waybill for each way it must travel: frozen, chilled,
        or at room temperature with what needs none."""
        travels: dict[stock.Storage, list[sales.SalesOrderLine]] = {}
        for line in lines:
            travels.setdefault(self.product_of[line.item_id].storage, []).append(line)
        for storage in sorted(travels, key=list(stock.Storage).index):
            placed.waybills += 1
            self._pack(placed, storage, travels[storage])

    def _pack(
        self, placed: _Order, storage: stock.Storage, lines: list[sales.SalesOrderLine]
    ) -> None:
        """Pack lines on a waybill of their own, and fill in, close and check it."""
        waybill = self.do(
            self.staff.logistics, airfreight.OpenWaybill(sales_order_id=placed.order.id)
        )
        shipped = [(line, self.product_of[line.item_id]) for line in lines]
        details = [self.details[product.code] for _, product in shipped]
        raw = sum((p.weight * line.quantity for line, p in shipped), Decimal(0))
        factor = Decimal("1.7") if storage is not stock.Storage.AMBIENT else Decimal("1.15")
        pieces = max(1, min(10_000, math.ceil(raw * factor / 20)))
        gross = (raw * factor + Decimal("1.2") * pieces).quantize(Decimal("0.01"))
        packings: list[airfreight.PackingLine] = []
        for index, (line, product) in enumerate(shipped):
            left = line.quantity
            for batch_id, received in placed.stock[line.id]:
                quantity = min(received, left)
                if not quantity:
                    break
                left -= quantity
                net = max(Decimal("0.01"), product.weight * quantity * Decimal("0.6"))
                packings.append(
                    self.do(
                        self.staff.logistics,
                        airfreight.PackLine(
                            waybill_id=waybill.id,
                            sales_order_line_id=line.id,
                            batch_id=batch_id,
                            quantity=quantity,
                            package=f"{index % pieces + 1} of {pieces}",
                            net_weight=net.quantize(Decimal("0.01")),
                        ),
                    )
                )
            placed.packed.add(line.id)
        if self.rng.random() < 0.15:
            self.amend(
                self.staff.logistics,
                airfreight.AmendPackingLine,
                packings[-1],
                notes="Packed with its own temperature logger.",
            )
        limited = any("temperature_max" in held for held in details)
        temperature = {
            stock.Storage.FROZEN: airfreight.TemperatureRange.FRO,
            stock.Storage.CHILLED: airfreight.TemperatureRange.COL,
        }.get(
            storage,
            airfreight.TemperatureRange.CRT if limited else airfreight.TemperatureRange.AMB,
        )
        frozen = any(held.get("dry_ice") for held in details)
        logged = any(held.get("temperature_logger") for held in details)
        heavy = gross / pieces > 40
        filled: dict[str, object] = {
            "origin_airport": "MAN" if self.rng.random() < 0.15 else "LHR",
            "forwarder": self.rng.choice(FORWARDERS) if self.rng.random() < 0.4 else None,
            "pieces": pieces,
            "gross_weight": gross,
            "volume": max(Decimal("0.010"), gross * Decimal("0.007")).quantize(Decimal("0.001")),
            "dimensions": f"{pieces} pallets of 120 x 80 x 100 cm"
            if heavy
            else f"{pieces} boxes of 60 x 40 x 40 cm",
            "temperature": temperature,
            "dry_ice": Decimal(5 * pieces) if frozen else None,
            "temperature_logger": f"TL-{self.rng.randint(10, 99)}-{self.rng.randint(10000, 99999)}"
            if logged
            else None,
            "instructions": {
                airfreight.TemperatureRange.FRO: "Keep frozen at -20 °C or colder.",
                airfreight.TemperatureRange.COL: "Keep at 2 to 8 °C. Do not freeze.",
                airfreight.TemperatureRange.CRT: "Keep at 15 to 25 °C.",
            }.get(temperature),
        }
        self.amend(self.staff.export_documents, airfreight.FillWaybill, waybill, **filled)
        self.do(self.staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id))
        if self.rng.random() < 0.04:
            self.do(self.staff.logistics, airfreight.ReopenPacking(waybill_id=waybill.id))
            self.amend(
                self.staff.export_documents,
                airfreight.FillWaybill,
                waybill,
                gross_weight=gross + Decimal("1.50"),
                instructions="Reweighed after a damaged box was repacked.",
            )
            self.do(self.staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id))
        self.later(1, 1, 10, partial(self.book, placed, waybill))

    def book(self, placed: _Order, waybill: airfreight.Waybill) -> None:
        airline, prefix, flights = self.rng.choice(AIRLINES)
        self.serials[prefix] += self.rng.randint(1, 40)
        serial = self.serials[prefix]
        flies = self.today + timedelta(days=self.rng.randint(1, 3))
        while flies.weekday() >= 5:
            flies += timedelta(days=1)
        self.do(
            self.staff.logistics,
            airfreight.BookFreight(
                waybill_id=waybill.id,
                airline=airline,
                awb_number=f"{prefix}-{serial:07d}{serial % 7}",
                flight=f"{flights} {self.rng.choice((101, 203, 317, 405, 611))}",
                flight_date=flies,
            ),
        )
        self.later(0, 1, 14, partial(self.document, placed, waybill))
        self.at(flies, 17, partial(self.dispatch, placed, waybill))

    def document(self, placed: _Order, waybill: airfreight.Waybill) -> None:
        """Hold every document the waybill needs, by its destination or for its goods, then
        complete it."""
        missing = self.kernel.ask(airfreight.WhatIsMissing(waybill_id=waybill.id), actor=SETUP)
        country = placed.plan.buyer.organisation.country
        for requirement in self.requirements.get(country, []) + self.requirements.get(None, []):
            if requirement.document not in missing.documents:
                continue
            issued = self.today - timedelta(days=self.rng.randint(0, 10))
            lasting = "permit" in requirement.document or "licence" in requirement.document
            self.do(
                self.staff.export_documents,
                airfreight.HoldShippingDocument(
                    waybill_id=waybill.id,
                    requirement_id=requirement.id,
                    reference=f"{requirement.country or 'GB'}-{issued:%Y}-{self.number():05d}",
                    issued=issued,
                    expires=issued + timedelta(days=180) if lasting else None,
                ),
            )
        self.do(self.staff.logistics, airfreight.CompleteWaybill(waybill_id=waybill.id))

    def dispatch(self, placed: _Order, waybill: airfreight.Waybill) -> None:
        """Dispatch on the day of the flight, and invoice it; a day later each day while the
        waybill is incomplete or the customer's import permit has lapsed."""
        lapsed = self.permits[placed.plan.buyer.code] < self.today
        if lapsed or self.latest(waybill).state != "complete":
            self.later(1, 1, 17, partial(self.dispatch, placed, waybill))
            return
        self.do(
            self.staff.logistics,
            airfreight.DispatchWaybill(waybill_id=waybill.id, dispatched=self.today),
        )
        invoice = self.do(
            self.staff.logistics, invoicing.RaiseInvoice(waybill_id=waybill.id, issued=self.today)
        )
        placed.waybills -= 1
        placed.shipping = placed.waybills > 0
        if invoice.state == "issued" and self.rng.random() < 0.9:
            pays = max(invoice.due, self.today) + timedelta(days=self.rng.randint(-3, 25))
            self.at(max(pays, self.today + timedelta(days=2)), 12, partial(self.pay, invoice))
        self.consider_shipping(placed)

    def pay(self, invoice: invoicing.Invoice) -> None:
        self.do(
            self.staff.md,
            invoicing.RecordPayment(
                invoice_id=invoice.id,
                paid=self.today,
                reference=f"TT-{self.today:%y%m}-{self.number():05d}",
            ),
        )


def _details(rng: random.Random, product: Product, maker: Maker) -> dict[str, object]:
    """What Kestrel's modules keep about a product, as its family makes plausible: how it
    is kept and for how long, how it is bought, flown and sold, and where its sales are
    recorded."""
    family, kept, life = product.family, product.storage, product.shelf_life
    kit, tuberculin = family in (E, P, R), "tuberculin" in product.name
    disease = next(
        (
            (name, species, controlled)
            for word, name, species, controlled in _DISEASES
            if word in product.name
        ),
        None,
    )
    details: dict[str, object] = {
        "storage": kept,
        "shelf_life_days": life,
        "expiry_tracked": life is not None,
        "certificate_required": life is not None,
        "bin": _bin(rng, product),
        "net_weight": (product.weight * Decimal("0.7")).quantize(Decimal("0.01")),
        "gross_weight": product.weight.quantize(Decimal("0.01")),
        "commodity_code": product.commodity.replace(" ", ""),
        "country_of_origin": maker.organisation.country,
        "product_group": _GROUP[family],
        "list_price": Money.of(_nice(product.cost * Decimal("1.45")), "GBP"),
        "tender_eligible": rng.random() >= 0.04,
        "revenue_account": _REVENUE[family],
    }
    if kept is stock.Storage.CHILLED:
        biological = product.dangerous == "UN3373"
        details |= {
            "temperature_min": Decimal(2),
            "temperature_max": Decimal(8),
            "do_not_freeze": not biological,
            "temperature_logger": True,
            "packaging": airfreight.Packaging.TRIPLE
            if biological
            else airfreight.Packaging.INSULATED_SHIPPER,
            "biological_substance": biological,
        }
    elif kept is stock.Storage.FROZEN:
        details |= {
            "temperature_max": Decimal(-20),
            "dry_ice": True,
            "temperature_logger": True,
            "packaging": airfreight.Packaging.DRY_ICE_SHIPPER,
        }
    elif family is R:
        details |= {"temperature_min": Decimal(2), "temperature_max": Decimal(30)}
    elif family is G and life is not None:
        details |= {"temperature_min": Decimal(15), "temperature_max": Decimal(25)}
    if life is not None and (family in (V, G) or kit):
        # What customers' tenders commonly ask for: a third of the life, in whole months.
        details["minimum_shelf_life_days"] = life // 3 // 30 * 30
    if product.dangerous and product.dangerous != "UN3373":
        number, hazard = product.dangerous.split(", class ")
        name, group = _HAZARDS[number]
        details |= {
            "un_number": number,
            "hazard_class": airfreight.HazardClass(hazard),
            "packing_group": group,
            "proper_shipping_name": name,
            "packaging": airfreight.Packaging.UN_SPECIFICATION,
        }
    if family is V:
        details |= {
            "light_sensitive": True,
            "health_certificate": True,
            "customs_description": "Veterinary vaccine",
            "veterinary_only": True,
            "sales_multiple": 10,
            "purchase_unit": "XBX",
            "pack_size": 10,
            "minimum_order": rng.choice((None, 50, 100)),
            "registration_number": f"Vm {rng.randint(10000, 59999)}/{rng.randint(4000, 4999)}",
        }
    elif kit:
        details["customs_description"] = "Diagnostic test kit for veterinary use"
    elif family is C:
        case = rng.choice((1, 1, 1, 5, 10))
        details |= {"purchase_unit": "XCS" if case > 1 else None, "pack_size": case}
    elif family in (Q, K):
        side = (product.weight / 200) ** (Decimal(1) / 3) * 100
        details |= {
            "length_cm": max(Decimal(5), side * Decimal("1.3")).quantize(Decimal(1)),
            "width_cm": max(Decimal(5), side).quantize(Decimal(1)),
            "height_cm": max(Decimal(5), side * Decimal("0.8")).quantize(Decimal(1)),
            "packaging": airfreight.Packaging.CRATE
            if product.weight > 30
            else airfreight.Packaging.CARTON,
        }
    if tuberculin:
        details |= {
            "product_group": sales.ProductGroup.VACCINES,
            "veterinary_only": True,
            "registration_number": f"Vm {rng.randint(10000, 59999)}/{rng.randint(4000, 4999)}",
            "revenue_account": "4010",
        }
    if disease is not None and (family is V or kit or tuberculin):
        name, species, controlled = disease
        details |= {
            "disease_area": name,
            "species": species,
            "export_licence": controlled and family is P,
        }
    return details


def _permit(rng: random.Random, country: str, issued: date) -> str:
    """An import permit's number, as a veterinary authority issues them."""
    return f"{country}-IP-{issued:%Y}-{rng.randint(100, 9999):04d}"


def _quantity(rng: random.Random, product: Product) -> int:
    """How many of a product a tender asks for."""
    match product.family:
        case Family.VACCINE:
            return rng.choice((20, 40, 50, 100, 200, 300, 500, 1000))
        case Family.ELISA:
            return rng.randint(2, 24)
        case Family.PCR:
            return rng.randint(2, 16)
        case Family.RAPID:
            return rng.randint(5, 60)
        case Family.REAGENT:
            return rng.randint(2, 30)
        case Family.CONSUMABLE:
            return rng.randint(5, 120)
        case Family.EQUIPMENT:
            return rng.choice((1, 1, 1, 2, 2, 3, 4))
        case Family.COLD_CHAIN:
            return rng.choice((1, 2, 3)) if product.cost > 1000 else rng.randint(2, 40)


def _temperature(rng: random.Random, storage: stock.Storage) -> Decimal | None:
    match storage:
        case stock.Storage.CHILLED:
            return Decimal(rng.randint(28, 74)) / 10
        case stock.Storage.FROZEN:
            return Decimal(-rng.randint(210, 280)) / 10
        case stock.Storage.AMBIENT:
            return None


def _bin(rng: random.Random, product: Product) -> str:
    """A bin to put stock away in: a cold room, a freezer, the dangerous goods cage or the
    warehouse, by how it is kept."""
    area = (
        "CR1"
        if product.storage is stock.Storage.CHILLED
        else "FZ1"
        if product.storage is stock.Storage.FROZEN
        else "DG1"
        if product.dangerous
        else "W2"
    )
    return f"{area}-{rng.choice('ABCD')}{rng.randint(1, 12):02d}"


def _nice(amount: Decimal) -> Decimal:
    """An amount rounded as a price list would show it: pounds over 100, halves over 10."""
    if amount >= 100:
        return amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if amount >= 10:
        return (amount * 2).quantize(Decimal(1), rounding=ROUND_HALF_UP) / 2
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
