"""A month at Kestrel, as the README tells it: the setup, then the work, to practise on.

`set_up` gives a new deployment what Kestrel needs before anyone works: a chart of
accounts, exchange rates, customers and manufacturers with their contacts, addresses and
terms, items with what each module keeps about them, the destinations' rules, and roles
for its people and agents. `enrol` signs those
people and agents up. `play` then does the first weeks of the month as they would: the
sourcing agent loads price lists, Tenders quotes and wins a tender, a deposit arrives,
purchase orders go out (one of them over the approval limit, waiting for the managing
director), a manufacturer confirms one, and goods start to arrive. What is left
(approving, receiving, packing, the waybill, dispatch and the invoice) is the work to try
in a client.

Every name here is invented.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from remit import access, items, ledger, parties
from remit.kernel import Actor, Command, Document, Extension, Kernel, Money, Rejected

from . import airfreight, fx, invoicing, pricelists, purchasing, sales, stock
from .common import Incoterm, PaymentTerms, ShipmentMethod

SETUP = Actor.system("setup", permissions={"*"})

READ = "*.read.*"
ROLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "MD": ("Managing director", ("*",)),
    "TENDERS": (
        "Tenders",
        (
            READ,
            "parties.*",
            "sales.open_quotation",
            "sales.amend_quotation",
            "sales.add_quotation_line",
            "sales.amend_quotation_line",
            "sales.remove_quotation_line",
            "sales.reopen_quotation",
            "sales.send_quotation",
            "sales.win_quotation",
            "sales.lose_quotation",
            "sales.withdraw_quotation",
            "sales.amend_sales_order",
            "sales.amend_sales_order_line",
            "sales.record_deposit",
            "sales.amend_customer_terms",
            "purchasing.amend_supplier_terms",
            "purchasing.raise_purchase_order",
            "purchasing.amend_purchase_order",
            "purchasing.add_purchase_order_line",
            "purchasing.amend_purchase_order_line",
            "purchasing.remove_purchase_order_line",
            "purchasing.send_purchase_order",
            "purchasing.confirm_purchase_order",
            "purchasing.cancel_purchase_order",
        ),
    ),
    "LOGISTICS": (
        "Logistics",
        (
            READ,
            "purchasing.receive_goods",
            "airfreight.*",
            "stock.amend_batch",
            "stock.write_off_batch",
            "invoicing.raise_invoice",
        ),
    ),
    "SOURCING": (
        "Sourcing agent",
        (
            READ,
            "pricelists.*",
            "items.add_item",
            "items.amend_item",
            "stock.amend_item_stock",
            "purchasing.amend_item_purchasing",
            "sales.amend_item_sales",
        ),
    ),
    "EXPORT_DOCUMENTS": (
        "Export documents agent",
        (
            READ,
            "airfreight.fill_waybill",
            "airfreight.hold_shipping_document",
            "airfreight.amend_item_shipping",
        ),
    ),
}
"""Who may do what. The managing director alone approves quotations below the margin
floor or over a customer's credit limit and purchase orders over the limit, approves
manufacturers to supply, decides which account each item's sales are recorded in,
cancels orders and records customers' payments; agents cannot touch a quotation at all."""


@dataclass(frozen=True)
class Person:
    """Someone to enrol: who they are, and how their identity provider knows them."""

    name: str
    kind: access.MemberKind
    role: str
    subject: str


@dataclass(frozen=True)
class Staff:
    """Kestrel's people and agents, as the actors they sign in as."""

    md: Actor
    tenders: Actor
    logistics: Actor
    sourcing: Actor
    export_documents: Actor


@dataclass(frozen=True)
class Played:
    """What the first weeks left behind, for whoever carries on."""

    tender: sales.Quotation
    order: sales.SalesOrder
    thin: sales.Quotation
    """A second quotation, below the margin floor, waiting for approval."""
    over_limit: purchasing.PurchaseOrder
    """A purchase order over the approval limit, waiting for approval."""
    part_received: purchasing.PurchaseOrder
    received: purchasing.PurchaseOrder


@dataclass(frozen=True)
class Contact:
    """Someone at a customer or manufacturer, and how to reach them."""

    name: str
    role: str
    email: str
    phone: str | None = None


@dataclass(frozen=True)
class Organisation:
    """A customer or manufacturer, with the people to deal with and where it is."""

    code: str
    name: str
    country: str
    contacts: tuple[Contact, ...]
    addresses: tuple[tuple[str, tuple[str, ...]], ...]
    """Each address's label and its lines, without the country. The first is where
    invoices go, the last where goods go."""


ACCOUNTS = (
    ("1100", "Trade debtors", ledger.AccountType.ASSET, None),
    ("1200", "Bank, sterling", ledger.AccountType.ASSET, "GBP"),
    ("1210", "Bank, US dollars", ledger.AccountType.ASSET, "USD"),
    ("1220", "Bank, euros", ledger.AccountType.ASSET, "EUR"),
    ("1300", "Stock", ledger.AccountType.ASSET, "GBP"),
    ("2100", "Trade creditors", ledger.AccountType.LIABILITY, None),
    ("2300", "Customer deposits", ledger.AccountType.LIABILITY, None),
    ("3000", "Opening balances", ledger.AccountType.EQUITY, "GBP"),
    ("4000", "Sales, zero-rated exports", ledger.AccountType.INCOME, None),
    ("4010", "Sales, vaccines", ledger.AccountType.INCOME, None),
    ("4020", "Sales, diagnostic kits", ledger.AccountType.INCOME, None),
    ("4030", "Sales, laboratory supplies", ledger.AccountType.INCOME, None),
    ("4040", "Sales, equipment and cold chain", ledger.AccountType.INCOME, None),
    ("4100", "Carriage and insurance charged", ledger.AccountType.INCOME, None),
    ("5000", "Cost of sales", ledger.AccountType.EXPENSE, "GBP"),
    ("5100", "Carriage inwards", ledger.AccountType.EXPENSE, "GBP"),
    ("7800", "Exchange gains and losses", ledger.AccountType.EXPENSE, "GBP"),
    ("7900", "Stock written off", ledger.AccountType.EXPENSE, "GBP"),
)

RATES = {"USD": "0.7900", "EUR": "0.8500", "DKK": "0.1140"}
"""Pounds per unit, from the first of the month."""

PARTIES = (
    Organisation(
        "C-NVDL",
        "Novadel Veterinary Diagnostic Laboratory",
        "VN",
        (Contact("Tran Minh Chau", "Procurement officer", "procurement@novadel-vdl.example"),),
        (
            ("Billing", ("Novadel Veterinary Diagnostic Laboratory", "Dong Da District", "Hanoi")),
            ("Delivery", ("Novadel VDL, Central Laboratory", "Soc Son District", "Hanoi")),
        ),
    ),
    Organisation(
        "C-DLD",
        "Dolden Livestock Development Agency",
        "TH",
        (Contact("Arunee Wongsakul", "Head of procurement", "tenders@dolden-lda.example"),),
        (
            (
                "Billing and delivery",
                ("Dolden Livestock Development Agency", "Ratchathewi", "Bangkok"),
            ),
        ),
    ),
    Organisation(
        "S-NORDVET",
        "Nordvet Biologics A/S",
        "DK",
        (Contact("Freja Lindqvist", "Export sales", "export@nordvet-biologics.example"),),
        (("Head office", ("Nordvet Biologics A/S", "Industrivej 41", "2605 Brøndby")),),
    ),
    Organisation(
        "S-AURELIAN",
        "Aurelian Diagnostics GmbH",
        "DE",
        (Contact("Jonas Albrecht", "Order desk", "orders@aurelian-diagnostics.example"),),
        (
            (
                "Head office",
                ("Aurelian Diagnostics GmbH", "Im Neuenheimer Feld 12", "69120 Heidelberg"),
            ),
        ),
    ),
    Organisation(
        "S-CALDER",
        "Calder Scientific Ltd",
        "GB",
        (
            Contact(
                "Hannah Doyle",
                "Customer services",
                "sales@calder-scientific.example",
                "0113 496 0418",
            ),
        ),
        (("Head office", ("Calder Scientific Ltd", "Unit 7, Airedale Park", "Leeds")),),
    ),
)

ITEMS = (
    ("VAC-NDV-1000", "Newcastle disease vaccine, live, 1,000 doses", "XVI"),
    ("VAC-FMD-50", "Foot-and-mouth disease vaccine, inactivated, 50 doses", "XVI"),
    ("KIT-AI-ELISA", "Avian influenza antibody ELISA kit, 480 tests", "KT"),
    ("KIT-ASF-PCR", "African swine fever real-time PCR kit, freeze-dried, 96 reactions", "KT"),
    ("KIT-BRU-RBT", "Brucella Rose Bengal test, 1,000 tests", "KT"),
    ("EQP-THERMO", "Real-time PCR thermocycler, 96 wells", "EA"),
    ("EQP-BSC2", "Biosafety cabinet, class II, 1.2 m", "EA"),
    ("EQP-COOLBOX", "Sample transport cool box, validated 72 hours", "EA"),
    ("EQP-CENTRIFUGE", "Refrigerated benchtop centrifuge", "EA"),
)

PRICES = (
    # Manufacturer, item, cost per unit, the manufacturer's own code, lead time in days.
    ("S-NORDVET", "VAC-NDV-1000", "DKK 310.00", "NV-ND1000", 21),
    ("S-NORDVET", "VAC-FMD-50", "DKK 1450.00", "NV-FMD50", 35),
    ("S-AURELIAN", "KIT-AI-ELISA", "EUR 620.00", "AD-AIAB-480", 14),
    ("S-AURELIAN", "KIT-ASF-PCR", "EUR 845.00", "AD-ASF-96", 14),
    ("S-CALDER", "EQP-THERMO", "GBP 18400.00", "CS-QPCR96", 42),
    ("S-CALDER", "EQP-BSC2", "GBP 7250.00", "CS-BSC2-12", 56),
    ("S-CALDER", "EQP-COOLBOX", "GBP 185.00", "CS-CB72", 7),
    ("S-CALDER", "EQP-CENTRIFUGE", "GBP 4950.00", "CS-RC4", 28),
)

_COLD: dict[str, object] = {
    "storage": stock.Storage.CHILLED,
    "temperature_min": Decimal(2),
    "temperature_max": Decimal(8),
    "do_not_freeze": True,
    "expiry_tracked": True,
    "certificate_required": True,
    "temperature_logger": True,
    "packaging": airfreight.Packaging.INSULATED_SHIPPER,
}
_VACCINE: dict[str, object] = {
    **_COLD,
    "light_sensitive": True,
    "purchase_unit": "XBX",
    "pack_size": 10,
    "commodity_code": "30024200",
    "country_of_origin": "DK",
    "customs_description": "Veterinary vaccine",
    "health_certificate": True,
    "product_group": sales.ProductGroup.VACCINES,
    "veterinary_only": True,
    "sales_multiple": 10,
    "revenue_account": "4010",
}
_KIT: dict[str, object] = {
    **_COLD,
    "commodity_code": "38221900",
    "country_of_origin": "DE",
    "customs_description": "Diagnostic test kit for veterinary use",
    "product_group": sales.ProductGroup.DIAGNOSTICS,
    "revenue_account": "4020",
}
_EQUIPMENT: dict[str, object] = {
    "country_of_origin": "GB",
    "packaging": airfreight.Packaging.CRATE,
    "product_group": sales.ProductGroup.EQUIPMENT,
    "revenue_account": "4040",
}
ITEM_DETAILS: dict[str, dict[str, object]] = {
    "VAC-NDV-1000": {
        **_VACCINE,
        "shelf_life_days": 540,
        "minimum_shelf_life_days": 180,
        "bin": "CR1-A01",
        "net_weight": Decimal("0.02"),
        "gross_weight": Decimal("0.05"),
        "minimum_order": 100,
        "species": (sales.Species.POULTRY,),
        "disease_area": "Newcastle disease",
        "list_price": Money.of("52.00", "GBP"),
        "registration_number": "Vm 41268/4012",
    },
    "VAC-FMD-50": {
        **_VACCINE,
        "shelf_life_days": 730,
        "minimum_shelf_life_days": 240,
        "bin": "CR1-A02",
        "net_weight": Decimal("0.06"),
        "gross_weight": Decimal("0.09"),
        "minimum_order": 50,
        "species": tuple(sales.Species)[:5],
        "disease_area": "Foot-and-mouth disease",
        "list_price": Money.of("210.00", "GBP"),
        "registration_number": "Vm 41268/4027",
    },
    "KIT-AI-ELISA": {
        **_KIT,
        "shelf_life_days": 450,
        "minimum_shelf_life_days": 120,
        "bin": "CR1-A03",
        "net_weight": Decimal("0.90"),
        "gross_weight": Decimal("1.60"),
        "length_cm": Decimal(30),
        "width_cm": Decimal(20),
        "height_cm": Decimal(15),
        "species": (sales.Species.POULTRY, sales.Species.DUCKS),
        "disease_area": "Avian influenza",
        "list_price": Money.of("690.00", "GBP"),
    },
    "KIT-ASF-PCR": {
        **_KIT,
        "shelf_life_days": 365,
        "minimum_shelf_life_days": 90,
        "bin": "CR1-A04",
        "net_weight": Decimal("0.35"),
        "gross_weight": Decimal("0.60"),
        "export_licence": True,
        "species": (sales.Species.PIGS,),
        "disease_area": "African swine fever",
        "list_price": Money.of("890.00", "GBP"),
    },
    "KIT-BRU-RBT": {
        **_KIT,
        "shelf_life_days": 365,
        "minimum_shelf_life_days": 90,
        "bin": "CR1-A05",
        "net_weight": Decimal("0.40"),
        "gross_weight": Decimal("0.70"),
        "species": (sales.Species.CATTLE, sales.Species.SHEEP, sales.Species.GOATS),
        "disease_area": "Brucellosis",
        "list_price": Money.of("440.00", "GBP"),
    },
    "EQP-THERMO": {
        **_EQUIPMENT,
        "bin": "W2-C01",
        "net_weight": Decimal(28),
        "gross_weight": Decimal(42),
        "length_cm": Decimal(60),
        "width_cm": Decimal(55),
        "height_cm": Decimal(50),
        "commodity_code": "90278900",
        "customs_description": "Real-time PCR instrument",
        "list_price": Money.of("23500.00", "GBP"),
    },
    "EQP-BSC2": {
        **_EQUIPMENT,
        "bin": "W2-B11",
        "net_weight": Decimal(210),
        "gross_weight": Decimal(285),
        "length_cm": Decimal(140),
        "width_cm": Decimal(85),
        "height_cm": Decimal(160),
        "commodity_code": "84146000",
        "customs_description": "Biological safety cabinet",
        "list_price": Money.of("9800.00", "GBP"),
    },
    "EQP-COOLBOX": {
        **_EQUIPMENT,
        "bin": "W2-B12",
        "net_weight": Decimal("4.20"),
        "gross_weight": Decimal("5.40"),
        "commodity_code": "39231090",
        "packaging": airfreight.Packaging.CARTON,
        "product_group": sales.ProductGroup.COLD_CHAIN,
        "list_price": Money.of("290.00", "GBP"),
    },
    "EQP-CENTRIFUGE": {
        **_EQUIPMENT,
        "bin": "W2-C02",
        "net_weight": Decimal(38),
        "gross_weight": Decimal(52),
        "commodity_code": "84211970",
        "list_price": Money.of("6400.00", "GBP"),
    },
}
"""What Kestrel's modules keep about each item: how it is kept, bought, flown and sold,
and where its sales are recorded."""

REQUIREMENTS: tuple[tuple[str | None, str, airfreight.Condition, str | None], ...] = (
    (
        "VN",
        "Veterinary import permit",
        airfreight.Condition.ALWAYS,
        "Issued by the national veterinary authority",
    ),
    ("VN", "Certificate of origin", airfreight.Condition.ALWAYS, "From a UK chamber of commerce"),
    (
        "TH",
        "Import licence",
        airfreight.Condition.ALWAYS,
        "Issued by the national livestock authority",
    ),
    (
        None,
        "Veterinary health certificate",
        airfreight.Condition.HEALTH_CERTIFICATE,
        "Signed by an official veterinarian for each consignment",
    ),
    (
        None,
        "Export licence",
        airfreight.Condition.EXPORT_LICENCE,
        "From the UK's export control authority",
    ),
    (
        None,
        "Shipper's declaration for dangerous goods",
        airfreight.Condition.DANGEROUS_GOODS,
        "Signed by someone trained to ship dangerous goods by air",
    ),
)
"""Documents required by Vietnam and Thailand, and by every destination when the goods
need them."""

KEPT_BY: dict[type[Extension[Any]], tuple[type[Command[Any]], Callable[[Staff], Actor]]] = {
    stock.ItemStock: (stock.AmendItemStock, lambda staff: staff.sourcing),
    pricelists.ItemSourcing: (pricelists.AmendItemSourcing, lambda staff: staff.sourcing),
    purchasing.ItemPurchasing: (purchasing.AmendItemPurchasing, lambda staff: staff.sourcing),
    airfreight.ItemShipping: (airfreight.AmendItemShipping, lambda staff: staff.export_documents),
    sales.ItemSales: (sales.AmendItemSales, lambda staff: staff.sourcing),
    invoicing.ItemInvoicing: (invoicing.AmendItemInvoicing, lambda staff: staff.md),
    sales.CustomerTerms: (sales.AmendCustomerTerms, lambda staff: staff.tenders),
    purchasing.SupplierTerms: (purchasing.AmendSupplierTerms, lambda staff: staff.tenders),
    purchasing.SupplierApproval: (purchasing.ApproveSupplier, lambda staff: staff.md),
}
"""Each extension Kestrel's modules add, the command that amends it, and who does."""


def terms(today: date, salesperson: UUID) -> dict[str, dict[str, object]]:
    """The terms Kestrel trades on with each customer and manufacturer, as they stand at
    the start of the month, and whether each manufacturer is approved to supply."""
    reading: dict[str, object] = {
        "supplier_incoterm": Incoterm.DAP,
        "supplier_named_place": "Kestrel Animal Health, Reading",
        "supplier_payment_terms": PaymentTerms.NET_30,
    }
    return {
        "C-NVDL": {
            "customer_type": sales.CustomerType.VETERINARY_LABORATORY,
            "customer_currency": "USD",
            "customer_payment_terms": PaymentTerms.NET_30,
            "credit_limit": Money.of("250000.00", "USD"),
            "customer_incoterm": Incoterm.CIP,
            "customer_named_place": "Hanoi, Noi Bai airport (HAN)",
            "destination_airport": "HAN",
            "salesperson_id": salesperson,
            "import_permit": "VN-IP-2026-0412",
            "import_permit_expires": today + timedelta(days=200),
            "documents_required": (
                sales.CustomerDocument.CERTIFICATE_OF_ANALYSIS,
                sales.CustomerDocument.TEMPERATURE_RECORD,
            ),
            "vendor_number": "V-20931",
        },
        "C-DLD": {
            "customer_type": sales.CustomerType.VETERINARY_SERVICE,
            "customer_currency": "EUR",
            "customer_payment_terms": PaymentTerms.LETTER_OF_CREDIT,
            "customer_incoterm": Incoterm.CIP,
            "customer_named_place": "Bangkok, Suvarnabhumi airport (BKK)",
            "destination_airport": "BKK",
            "salesperson_id": salesperson,
            "import_permit": "TH-IL-2026-1187",
            "import_permit_expires": today + timedelta(days=90),
            "documents_required": (sales.CustomerDocument.LEGALISED_INVOICE,),
        },
        "S-NORDVET": {
            **reading,
            "supplier_currency": "DKK",
            "lead_time_days": 30,
            "minimum_order_value": Money.of("5000.00", "DKK"),
            "account_number": "KES-1047",
            "ordering_email": "export@nordvet-biologics.example",
            "approved_until": today + timedelta(days=300),
            "approval_reference": "GDP-QA-2025-014",
        },
        "S-AURELIAN": {
            **reading,
            "supplier_currency": "EUR",
            "lead_time_days": 14,
            "minimum_order_value": Money.of("500.00", "EUR"),
            "account_number": "70044312",
            "ordering_email": "orders@aurelian-diagnostics.example",
            "approved_until": today + timedelta(days=400),
            "approval_reference": "GDP-QA-2025-022",
        },
        "S-CALDER": {
            **reading,
            "supplier_currency": "GBP",
            "lead_time_days": 7,
            "minimum_order_value": Money.of("250.00", "GBP"),
            "account_number": "KEST01",
            "ordering_email": "sales@calder-scientific.example",
            "approved_until": today + timedelta(days=200),
            "approval_reference": "GDP-QA-2024-031",
        },
    }


def set_up(kernel: Kernel, staff: Staff, today: date) -> None:
    """What Kestrel needs before anyone works. The sourcing agent loads the price lists."""
    month = today.replace(day=1)
    for code, name, kind, currency in ACCOUNTS:
        _do(kernel, SETUP, ledger.OpenAccount(code=code, name=name, type=kind, currency=currency))
    for currency, rate in RATES.items():
        _do(kernel, SETUP, fx.RecordRate(currency=currency, effective=month, rate=Decimal(rate)))
    for organisation in PARTIES:
        register(kernel, staff.tenders, organisation)
    for code, name, unit in ITEMS:
        _do(
            kernel,
            staff.sourcing,
            items.AddItem(code=code, kind=items.ItemKind.GOODS, name=name, unit=unit),
        )
    for supplier, item, cost, reference, lead_time in PRICES:
        _price(kernel, staff, supplier, item, cost, reference, month, "Price list", lead_time)
    for country, document, condition, note in REQUIREMENTS:
        _do(
            kernel,
            staff.logistics,
            airfreight.AddRequirement(
                country=country, document=document, condition=condition, note=note
            ),
        )
    makers = {item: supplier for supplier, item, *_ in PRICES}
    for code, details in ITEM_DETAILS.items():
        maker = {"supplier_id": _party(kernel, makers[code]).id} if code in makers else {}
        describe(kernel, staff, _item(kernel, code), details | maker)
    for code, details in terms(today, UUID(staff.tenders.id)).items():
        describe(kernel, staff, _party(kernel, code), details)
    _opening_stock(kernel, today)


def register(kernel: Kernel, actor: Actor, organisation: Organisation) -> parties.Party:
    """Register a customer or manufacturer, with its contacts and addresses."""
    party = _do(
        kernel,
        actor,
        parties.RegisterParty(
            code=organisation.code, kind=parties.PartyKind.ORGANISATION, name=organisation.name
        ),
    )
    for contact in organisation.contacts:
        _do(
            kernel,
            actor,
            parties.AddContact(
                party_id=party.id,
                name=contact.name,
                role=contact.role,
                email=contact.email,
                phone=contact.phone,
            ),
        )
    for label, lines in organisation.addresses:
        _do(
            kernel,
            actor,
            parties.AddAddress(
                party_id=party.id, label=label, lines=lines, country=organisation.country
            ),
        )
    return party


def describe(kernel: Kernel, staff: Staff, host: Document, details: Mapping[str, object]) -> int:
    """Give a document what Kestrel's modules add to it, each extension amended by whoever
    keeps it, as a client would: every field the details leave out at its default. Returns
    the version the document is left at."""
    version, unknown = host.version, set(details)
    for kind in kernel.registry.extensions(type(host)):
        command, keeper = KEPT_BY[kind]
        named = (command.amends, "version")
        defaults = {f: kind.model_fields[f].default for f in command.model_fields if f not in named}
        unknown -= defaults.keys()
        given = {f: details[f] for f in defaults if f in details and details[f] != defaults[f]}
        if given:
            fields = defaults | given | {str(command.amends): host.id, "version": version}
            version = _do(kernel, keeper(staff), command.model_validate(fields)).version
    if unknown:
        raise TypeError(f"no extension of {type(host).__name__} has {', '.join(sorted(unknown))}")
    return version


def roles(kernel: Kernel) -> dict[str, access.Role]:
    """Kestrel's roles, defined once."""
    defined: dict[str, access.Role] = {}
    for code, (name, permissions) in ROLES.items():
        found = kernel.page(access.Role, actor=SETUP, where={"code": code}).items
        defined[code] = (
            found[0]
            if found
            else _do(
                kernel, SETUP, access.DefineRole(code=code, name=name, permissions=permissions)
            )
        )
    return defined


def enrol(kernel: Kernel, issuer: str, people: dict[str, Person]) -> Staff:
    """Enrol Kestrel's people and agents, and sign each in as the API would."""
    defined = roles(kernel)
    actors: dict[str, Actor] = {}
    for key, person in people.items():
        role = defined[person.role]
        _do(
            kernel,
            SETUP,
            access.EnrolMember(
                name=person.name,
                kind=person.kind,
                issuer=issuer,
                subject=person.subject,
                roles=(role.id,),
            ),
        )
        signed_in = kernel.ask(access.SignIn(issuer=issuer, subject=person.subject), actor=SETUP)
        if signed_in is None:
            raise Rejected(f"{person.name} was enrolled but cannot sign in")
        actors[key] = signed_in
    return Staff(**actors)


def play(kernel: Kernel, staff: Staff, today: date) -> Played:
    """The first weeks of the month, as Kestrel's people and agents would do them."""
    month = today.replace(day=1)
    priya = UUID(staff.tenders.id)

    # 1. Two manufacturers send updated price lists.
    for supplier, item, cost, reference, lead_time in (
        ("S-NORDVET", "VAC-NDV-1000", "DKK 325.00", "NV-ND1000", 21),
        ("S-AURELIAN", "KIT-AI-ELISA", "EUR 640.00", "AD-AIAB-480", 14),
        ("S-AURELIAN", "KIT-ASF-PCR", "EUR 880.00", "AD-ASF-96B", 14),
    ):
        _price(kernel, staff, supplier, item, cost, reference, month, "Update", lead_time)
    discontinued = kernel.page(
        pricelists.Price,
        actor=staff.sourcing,
        where={"item_id": _item(kernel, "EQP-CENTRIFUGE").id, "state": "current"},
    ).items
    for price in discontinued:
        _do(kernel, staff.sourcing, pricelists.WithdrawPrice(price_id=price.id))

    # 2. A tender, quoted in dollars, carriage and insurance paid to Hanoi, as the
    #    laboratory's terms say. One item needs a price obtained by email, and the vaccines
    #    are discounted.
    laboratory = _party(kernel, "C-NVDL")
    contact, billing, delivery = _details(kernel, laboratory)
    tender = _do(
        kernel,
        staff.tenders,
        sales.OpenQuotation(
            customer_id=laboratory.id,
            destination="VN",
            contact_id=contact,
            bill_to_id=billing,
            ship_to_id=delivery,
            valid_until=today + timedelta(days=30),
            requested_delivery=today + timedelta(days=56),
            shipment_method=ShipmentMethod.AIR,
            cold_chain=True,
            tender="NVDL/T/2026/041",
            programme="RAHP-24-117",
            end_user="Provincial veterinary laboratories, northern region",
            freight=Money.of("4850.00", "USD"),
            insurance=Money.of("420.00", "USD"),
        ),
    )
    _price(
        kernel, staff, "S-AURELIAN", "KIT-BRU-RBT", "EUR 410.00", "AD-RBT-1000", month, "Email", 21
    )
    lines = (
        ("VAC-NDV-1000", 400, "USD 54.00", "2.5"),
        ("VAC-FMD-50", 300, "USD 245.00", "2.5"),
        ("KIT-AI-ELISA", 10, "USD 1050.00", "0"),
        ("KIT-ASF-PCR", 6, "USD 1380.00", "0"),
        ("KIT-BRU-RBT", 5, "USD 690.00", "0"),
        ("EQP-BSC2", 1, "USD 11900.00", "0"),
        ("EQP-COOLBOX", 20, "USD 310.00", "0"),
    )
    for item, quantity, price, discount in lines:
        _do(
            kernel,
            staff.tenders,
            sales.AddQuotationLine(
                quotation_id=tender.id,
                item_id=_item(kernel, item).id,
                quantity=quantity,
                unit_price=_money(price),
                discount_percent=Decimal(discount),
            ),
        )
    tender = _do(kernel, staff.tenders, sales.SendQuotation(quotation_id=tender.id))

    # 3. The tender is won against the laboratory's purchase order, and a 30% deposit
    #    arrives.
    order = _do(
        kernel,
        staff.tenders,
        sales.WinQuotation(
            quotation_id=tender.id,
            deposit=Decimal("0.3"),
            customer_po="NVDL-PO-2026-0388",
            customer_po_date=today,
        ),
    )
    if order.deposit_due:
        order = _do(
            kernel,
            staff.tenders,
            sales.RecordDeposit(sales_order_id=order.id, amount=order.deposit_due, received=today),
        )

    # 4. Purchase orders to three manufacturers on their terms, one in kroner and over
    #    the limit.
    raised: dict[str, purchasing.PurchaseOrder] = {}
    for supplier, days in (("S-NORDVET", 35), ("S-AURELIAN", 14), ("S-CALDER", 7)):
        manufacturer = _party(kernel, supplier)
        raised[supplier] = _do(
            kernel,
            staff.tenders,
            purchasing.RaisePurchaseOrder(
                supplier_id=manufacturer.id,
                sales_order_id=order.id,
                contact_id=_details(kernel, manufacturer)[0],
                buyer_id=priya,
                expected=today + timedelta(days=days),
            ),
        )
    for line in kernel.page(
        sales.SalesOrderLine, actor=staff.tenders, where={"sales_order_id": order.id}
    ).items:
        supplier = kernel.page(parties.Party, actor=staff.tenders, where={"id": line.supplier_id})
        po = raised[supplier.items[0].code]
        _do(
            kernel,
            staff.tenders,
            purchasing.AddPurchaseOrderLine(purchase_order_id=po.id, sales_order_line_id=line.id),
        )
    for supplier in ("S-AURELIAN", "S-CALDER"):
        po = raised[supplier]
        raised[supplier] = _do(
            kernel, staff.tenders, purchasing.SendPurchaseOrder(purchase_order_id=po.id)
        )
    raised["S-AURELIAN"] = _do(
        kernel,
        staff.tenders,
        purchasing.ConfirmPurchaseOrder(
            purchase_order_id=raised["S-AURELIAN"].id, supplier_reference="AB-26-55107"
        ),
    )

    # 5. Goods begin to arrive: everything from Calder, and the ELISA kits from Aurelian,
    #    with their delivery notes and certificates, put away as each item is kept.
    for supplier, only in (("S-CALDER", None), ("S-AURELIAN", "KIT-AI-ELISA")):
        po = raised[supplier]
        for line in kernel.page(
            purchasing.PurchaseOrderLine,
            actor=staff.logistics,
            where={"purchase_order_id": po.id},
        ).items:
            item = kernel.page(items.Item, actor=staff.logistics, where={"id": line.item_id})
            code = item.items[0].code
            if only and code != only:
                continue
            kept = ITEM_DETAILS[code]
            chilled = kept.get("storage") is stock.Storage.CHILLED
            life = kept.get("shelf_life_days")
            made = today - timedelta(days=30) if isinstance(life, int) else None
            batch = f"{code.split('-')[-1]}-{today:%y%m}-01"
            _do(
                kernel,
                staff.logistics,
                purchasing.ReceiveGoods(
                    purchase_order_line_id=line.id,
                    quantity=line.quantity,
                    batch=batch,
                    expiry=made + timedelta(days=life) if made and isinstance(life, int) else None,
                    temperature=Decimal("4.5") if chilled else None,
                    received=today,
                    manufactured=made,
                    certificate=f"COA-{batch}" if made else None,
                    delivery_note=f"DN-{po.code}-{line.line_number}",
                    carrier="Pennine Express Haulage",
                    packages=2 if chilled else line.quantity,
                    data_logger="TL-40-21877" if chilled else None,
                ),
            )

    # A second tender, quoted thin, which the managing director must approve.
    agency = _party(kernel, "C-DLD")
    contact, billing, _ = _details(kernel, agency)
    thin = _do(
        kernel,
        staff.tenders,
        sales.OpenQuotation(
            customer_id=agency.id,
            destination="TH",
            contact_id=contact,
            bill_to_id=billing,
            ship_to_id=billing,
            valid_until=today + timedelta(days=21),
            cold_chain=True,
            tender="DLD-2026-118",
        ),
    )
    for item, quantity, price in (
        ("KIT-AI-ELISA", 40, "EUR 690.00"),
        ("EQP-BSC2", 2, "EUR 9100.00"),
    ):
        _do(
            kernel,
            staff.tenders,
            sales.AddQuotationLine(
                quotation_id=thin.id,
                item_id=_item(kernel, item).id,
                quantity=quantity,
                unit_price=_money(price),
            ),
        )

    return Played(
        tender=_latest(kernel, tender),
        order=_latest(kernel, order),
        thin=_latest(kernel, thin),
        over_limit=_latest(kernel, raised["S-NORDVET"]),
        part_received=_latest(kernel, raised["S-AURELIAN"]),
        received=_latest(kernel, raised["S-CALDER"]),
    )


def _opening_stock(kernel: Kernel, today: date) -> None:
    """Cool boxes held from before in the overflow bin, valued at cost, and a batch of
    test kits now expired."""
    for code, batch, expiry, quantity, cost, location in (
        ("EQP-COOLBOX", "CB72-2501", None, 6, "GBP 180.00", "W1-A01"),
        ("KIT-BRU-RBT", "RBT-2410-07", today - timedelta(days=12), 4, "GBP 342.00", None),
    ):
        held = _do(
            kernel,
            SETUP,
            stock.ReceiveBatch(
                item_id=_item(kernel, code).id,
                batch=batch,
                expiry=expiry,
                quantity=quantity,
                unit_cost=_money(cost),
                location=location,
                certificate=f"COA-{batch}" if expiry else None,
            ),
        )
        _do(
            kernel,
            SETUP,
            ledger.PostJournalEntry(
                posting_date=today.replace(day=1),
                description=f"Opening stock: batch {batch}",
                postings=(
                    ledger.debit(_account(kernel, "1300"), held.value),
                    ledger.credit(_account(kernel, "3000"), held.value),
                ),
            ),
        )


def _details(kernel: Kernel, party: parties.Party) -> tuple[UUID, UUID, UUID]:
    """A party's first contact, and its first and last addresses: billing and delivery."""
    contact, *_ = kernel.page(parties.Contact, actor=SETUP, where={"party_id": party.id}).items
    addresses = kernel.page(parties.Address, actor=SETUP, where={"party_id": party.id}).items
    return contact.id, addresses[0].id, addresses[-1].id


def _price(
    kernel: Kernel,
    staff: Staff,
    supplier: str,
    item: str,
    cost: str,
    reference: str,
    effective: date,
    source: str,
    lead_time: int | None = None,
) -> None:
    _do(
        kernel,
        staff.sourcing,
        pricelists.RecordPrice(
            supplier_id=_party(kernel, supplier).id,
            item_id=_item(kernel, item).id,
            cost=_money(cost),
            effective=effective,
            reference=reference,
            source=source,
            lead_time_days=lead_time,
        ),
    )


def _do[R](kernel: Kernel, actor: Actor, command: Command[R]) -> R:
    return kernel.execute(command, actor=actor).result


def _latest[D: Document](kernel: Kernel, document: D) -> D:
    """A document as it stands now, after whatever happened to it since it was returned."""
    return kernel.page(type(document), actor=SETUP, where={"id": document.id}).items[0]


def _money(written: str) -> Money:
    currency, amount = written.split()
    return Money.of(amount, currency)


def _party(kernel: Kernel, code: str) -> parties.Party:
    return kernel.page(parties.Party, actor=SETUP, where={"code": code}).items[0]


def _item(kernel: Kernel, code: str) -> items.Item:
    return kernel.page(items.Item, actor=SETUP, where={"code": code}).items[0]


def _account(kernel: Kernel, code: str) -> UUID:
    return kernel.page(ledger.Account, actor=SETUP, where={"code": code}).items[0].id
