"""What Kestrel's modules add to items and parties, and the work it changes.

Kestrel keeps its master data as extensions of the standard modules' items and parties:
how an item is kept, bought, flown, sold and booked, and each customer's and
manufacturer's terms. Each is amended by a command of its own, which refuses details
that contradict one another. These tests hold each to that, then show the work that
follows from what is kept: batches refused when too short dated, purchase orders rounded
to the manufacturer's packs, waybills told what they need by what is packed on them,
quotations refused for what may not be sold, and goods held back once a customer's
import permit has lapsed.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import kestrel
import pytest
from conftest import ROOT, every
from kestrel import airfreight, invoicing, month, pricelists, purchasing, sales, stock
from kestrel.common import Incoterm, PaymentTerms
from kestrel.month import Staff
from pydantic import ValidationError
from test_kestrel import ISSUER, PEOPLE, TODAY, USD, do, latest

from remit import items, ledger, parties
from remit.kernel import Document, Extension, Kernel, Money, PostgresStore, Rejected, Unauthorised


@pytest.fixture
def kernel(postgres: PostgresStore) -> Kernel:
    application = kestrel.application()
    postgres.install(application.installed)
    return application.kernel(postgres)


@pytest.fixture
def staff(kernel: Kernel) -> Staff:
    staff = month.enrol(kernel, ISSUER, PEOPLE)
    month.set_up(kernel, staff, TODAY)
    return staff


def keep(
    kernel: Kernel, staff: Staff, kind: type[Extension[Any]], host: Document, **given: Any
) -> Any:
    """Amend what an extension keeps about a document as its keeper does from a client:
    every field as the document holds it now, but those given."""
    command, keeper = month.KEPT_BY[kind]
    current = kernel.get(kernel.registry.extended(type(host)), host.id, actor=ROOT)
    named = str(command.amends)
    held = {f: getattr(current, f) for f in command.model_fields if f not in (named, "version")}
    fields = held | given | {named: host.id, "version": current.version}
    return do(kernel, keeper(staff), command.model_validate(fields))


def kept(kernel: Kernel, host: Document) -> dict[str, Any]:
    """A document with every field its extensions add, as a client reads it."""
    return kernel.get(kernel.registry.extended(type(host)), host.id, actor=ROOT).model_dump()


REFUSALS = (
    # The extension, the item or party, the details given, and the problems they make.
    (
        stock.ItemStock,
        "VAC-NDV-1000",
        {"temperature_min": Decimal(10)},
        {("temperature_min",): "is above temperature_max, 8 °C"},
    ),
    (
        stock.ItemStock,
        "VAC-NDV-1000",
        {"minimum_shelf_life_days": 600, "gross_weight": Decimal("0.01")},
        {
            ("minimum_shelf_life_days",): "is more than the shelf life of 540 days",
            ("gross_weight",): "is less than the net weight, 0.02 kg",
        },
    ),
    (
        stock.ItemStock,
        "VAC-NDV-1000",
        {"storage": stock.Storage.FROZEN},
        {("do_not_freeze",): "contradicts storage: it is kept frozen"},
    ),
    (
        stock.ItemStock,
        "EQP-CENTRIFUGE",
        {"minimum_shelf_life_days": 30, "length_cm": Decimal(70)},
        {
            ("minimum_shelf_life_days",): "needs the shelf life it is part of",
            ("width_cm",): "is needed with the other dimensions",
            ("height_cm",): "is needed with the other dimensions",
        },
    ),
    (
        airfreight.ItemShipping,
        "KIT-AI-ELISA",
        {"un_number": "UN3373"},
        {("un_number",): "is not given here: declare it through biological_substance"},
    ),
    (
        airfreight.ItemShipping,
        "KIT-AI-ELISA",
        {"un_number": "UN1170", "packing_group": airfreight.PackingGroup.II},
        {
            ("hazard_class",): "is needed with UN1170",
            ("proper_shipping_name",): "is needed with UN1170",
        },
    ),
    (
        airfreight.ItemShipping,
        "KIT-AI-ELISA",
        {"hazard_class": airfreight.HazardClass.FLAMMABLE_LIQUID},
        {("hazard_class",): "needs a UN number"},
    ),
    (
        pricelists.ItemSourcing,
        "VAC-NDV-1000",
        {"supplier_id": "S-CALDER"},
        {("supplier_id",): "names S-CALDER, which has no current price for VAC-NDV-1000"},
    ),
    (
        purchasing.ItemPurchasing,
        "VAC-NDV-1000",
        {"purchase_unit": None, "minimum_order": 105},
        {
            ("purchase_unit",): "is needed for a pack of 10",
            ("minimum_order",): "must be a whole number of packs of 10",
        },
    ),
    (
        sales.ItemSales,
        "VAC-NDV-1000",
        {
            "species": (sales.Species.POULTRY, sales.Species.POULTRY),
            "list_price": Money.of("66.00", "USD"),
        },
        {
            ("species",): "names poultry twice",
            ("list_price",): "is in USD, not the base currency, GBP",
        },
    ),
    (
        invoicing.ItemInvoicing,
        "VAC-NDV-1000",
        {"revenue_account": "1200"},
        {("revenue_account",): "is not an income account: it is asset"},
    ),
    (
        invoicing.ItemInvoicing,
        "VAC-NDV-1000",
        {"revenue_account": "4999"},
        {("revenue_account",): "is not an account in the ledger"},
    ),
    (
        sales.CustomerTerms,
        "C-NVDL",
        {
            "customer_incoterm": None,
            "import_permit": None,
            "credit_limit": Money.of("250000.00", "EUR"),
        },
        {
            ("customer_named_place",): "needs the Incoterm that names it",
            ("import_permit_expires",): "needs the permit's number",
            ("credit_limit",): "is in EUR, not the customer's currency, USD",
        },
    ),
    (
        purchasing.SupplierTerms,
        "S-NORDVET",
        {"supplier_incoterm": None, "minimum_order_value": Money.of("700.00", "EUR")},
        {
            ("supplier_named_place",): "needs the Incoterm that names it",
            ("minimum_order_value",): "is in EUR, not the manufacturer's currency, DKK",
        },
    ),
    (
        purchasing.SupplierApproval,
        "S-NORDVET",
        {"approval_reference": None},
        {("approval_reference",): "is needed: give the qualification the approval rests on"},
    ),
)


@pytest.mark.parametrize(("kind", "code", "given", "problems"), REFUSALS)
def test_every_extension_refuses_details_that_contradict_one_another(
    kernel: Kernel,
    staff: Staff,
    kind: type[Extension[Any]],
    code: str,
    given: dict[str, Any],
    problems: dict[tuple[str, ...], str],
) -> None:
    host = _host(kernel, kind, code)
    named = {
        field: _party(kernel, value).id if field == "supplier_id" else value
        for field, value in given.items()
    }
    before = kept(kernel, host)
    with pytest.raises(Rejected) as refused:
        keep(kernel, staff, kind, host, **named)

    assert {problem.path: problem.message for problem in refused.value.problems} == problems
    assert kept(kernel, host) == before


def test_codes_customs_and_the_airline_read_are_refused_unless_well_formed(
    kernel: Kernel, staff: Staff
) -> None:
    kit = _item(kernel, "KIT-AI-ELISA")
    for field, value in (
        ("commodity_code", "3822"),
        ("commodity_code", "3822 19 00"),
        ("un_number", "1170"),
    ):
        with pytest.raises(ValidationError, match=field):
            keep(kernel, staff, airfreight.ItemShipping, kit, **{field: value})
    with pytest.raises(ValidationError, match="destination_airport"):
        keep(kernel, staff, sales.CustomerTerms, _party(kernel, "C-NVDL"), destination_airport="Ha")
    with pytest.raises(ValidationError, match="shelf_life_days"):
        keep(kernel, staff, stock.ItemStock, kit, shelf_life_days=0)


def test_a_service_is_not_kept_in_stock(kernel: Kernel, staff: Staff) -> None:
    service = do(
        kernel,
        ROOT,
        items.AddItem(code="SVC-COURIER", kind=items.ItemKind.SERVICE, name="Courier to LHR"),
    )
    with pytest.raises(Rejected, match=r"item SVC-COURIER is a service, which is not kept"):
        keep(kernel, staff, stock.ItemStock, service, bin="W2-A01")


def test_each_keeper_amends_only_what_is_theirs(kernel: Kernel, staff: Staff) -> None:
    vaccine = _item(kernel, "VAC-NDV-1000")
    kept_now = kept(kernel, vaccine)
    command = invoicing.AmendItemInvoicing(
        item_id=vaccine.id, version=kept_now["version"], revenue_account="4000"
    )
    with pytest.raises(Unauthorised, match=r"invoicing\.amend_item_invoicing"):
        do(kernel, staff.sourcing, command)
    amended = keep(kernel, staff, invoicing.ItemInvoicing, vaccine, revenue_account="4000")

    assert amended.revenue_account == "4000"
    assert kept(kernel, vaccine)["version"] == kept_now["version"] + 1
    with pytest.raises(Rejected, match=r"nothing would change: .* already has these details"):
        do(kernel, staff.md, command.model_copy(update={"version": kept_now["version"] + 1}))


def test_a_batch_arrives_with_its_paperwork_and_is_kept_as_its_item_is(
    kernel: Kernel, staff: Staff
) -> None:
    vaccine = _item(kernel, "VAC-NDV-1000")
    arriving = stock.ReceiveBatch(
        item_id=vaccine.id,
        batch="ND-2611-04",
        expiry=None,
        quantity=200,
        unit_cost=Money.of("35.34", "GBP"),
    )
    with pytest.raises(Rejected) as refused:
        do(kernel, staff.md, arriving)
    assert [problem.path for problem in refused.value.problems] == [("expiry",), ("certificate",)]

    received = do(
        kernel,
        staff.md,
        arriving.model_copy(
            update={"expiry": TODAY + timedelta(days=500), "certificate": "COA-ND-2611-04"}
        ),
    )
    assert (received.storage, received.location) == (stock.Storage.CHILLED, "CR1-A01")


def test_a_batch_with_less_life_left_than_the_item_must_have_is_not_packed(
    kernel: Kernel, staff: Staff
) -> None:
    played = month.play(kernel, staff, TODAY)
    vaccine = _item(kernel, "VAC-NDV-1000")
    short, long = (
        _receive(kernel, staff, vaccine, f"ND-2611-{days}", TODAY + timedelta(days=days))
        for days in (179, 181)
    )
    waybill = do(kernel, staff.logistics, airfreight.OpenWaybill(sales_order_id=played.order.id))
    line = _line(kernel, played.order, "VAC-NDV-1000")
    packing = airfreight.PackLine(
        waybill_id=waybill.id, sales_order_line_id=line.id, batch_id=short.id, quantity=100
    )
    expires = TODAY + timedelta(days=179)
    with pytest.raises(
        Rejected,
        match=rf"batch ND-2611-179 expires on {expires}, 179 days from now; VAC-NDV-1000 "
        r"must have at least 180 days left when it is packed",
    ):
        do(kernel, staff.logistics, packing)
    do(kernel, staff.logistics, packing.model_copy(update={"batch_id": long.id}))


def test_purchase_orders_follow_the_manufacturers_terms_and_packs(
    kernel: Kernel, staff: Staff
) -> None:
    order = _ordered(kernel, staff, "C-DLD", ("VAC-NDV-1000", 30), ("VAC-FMD-50", 50))
    maker = _party(kernel, "S-NORDVET")
    raised = do(kernel, staff.tenders, purchasing.RaisePurchaseOrder(supplier_id=maker.id))
    assert (raised.currency, raised.incoterm, raised.payment_terms) == (
        "DKK",
        Incoterm.DAP,
        PaymentTerms.NET_30,
    )
    assert (raised.named_place, raised.expected) == (
        "Kestrel Animal Health, Reading",
        TODAY + timedelta(days=30),
    )

    ndv = _line(kernel, order, "VAC-NDV-1000")
    adding = purchasing.AddPurchaseOrderLine(
        purchase_order_id=raised.id, sales_order_line_id=ndv.id
    )
    with pytest.raises(Rejected) as refused:
        do(kernel, staff.tenders, adding.model_copy(update={"quantity": 50}))
    assert [problem.message for problem in refused.value.problems] == [
        "is below the manufacturer's minimum order of 100"
    ]
    with pytest.raises(
        Rejected, match=r"30 more are needed, which the manufacturer's packs make 100, not 110"
    ):
        do(kernel, staff.tenders, adding.model_copy(update={"quantity": 110}))
    # Thirty were sold; the manufacturer sells no fewer than a hundred, in boxes of ten.
    assert do(kernel, staff.tenders, adding).quantity == 100

    fmd = _item(kernel, "VAC-FMD-50")
    keep(kernel, staff, purchasing.ItemPurchasing, fmd, blocked_for_purchase=True)
    with pytest.raises(Rejected, match=r"item VAC-FMD-50 is blocked for purchase"):
        do(
            kernel,
            staff.tenders,
            purchasing.AddPurchaseOrderLine(
                purchase_order_id=raised.id,
                sales_order_line_id=_line(kernel, order, "VAC-FMD-50").id,
            ),
        )


def test_only_an_approved_manufacturer_is_ordered_from(kernel: Kernel, staff: Staff) -> None:
    maker = _party(kernel, "S-NORDVET")
    raising = purchasing.RaisePurchaseOrder(supplier_id=maker.id)
    keep(
        kernel,
        staff,
        purchasing.SupplierApproval,
        maker,
        approved_until=None,
        approval_reference=None,
    )
    with pytest.raises(Rejected, match=r"S-NORDVET is not an approved supplier; approve it"):
        do(kernel, staff.tenders, raising)

    lapsed = TODAY - timedelta(days=1)
    keep(
        kernel,
        staff,
        purchasing.SupplierApproval,
        maker,
        approved_until=lapsed,
        approval_reference="GDP-QA-2025-014",
    )
    with pytest.raises(Rejected, match=rf"S-NORDVET's approval lapsed on {lapsed}; renew it"):
        do(kernel, staff.tenders, raising)


def test_a_quotation_is_on_the_customers_terms_and_at_list_price(
    kernel: Kernel, staff: Staff
) -> None:
    customer = _party(kernel, "C-DLD")
    quotation = do(
        kernel, staff.tenders, sales.OpenQuotation(customer_id=customer.id, destination="TH")
    )
    assert (quotation.currency, quotation.payment_terms) == ("EUR", PaymentTerms.LETTER_OF_CREDIT)
    assert (quotation.incoterm, quotation.named_place) == (
        Incoterm.CIP,
        "Bangkok, Suvarnabhumi airport (BKK)",
    )
    assert quotation.salesperson_id is not None

    boxes = _item(kernel, "EQP-COOLBOX")
    line = do(
        kernel,
        staff.tenders,
        sales.AddQuotationLine(quotation_id=quotation.id, item_id=boxes.id, quantity=4),
    )
    # Listed at GBP 290.00, which is EUR 341.18 at 85p.
    assert line.unit_price == Money.of("341.18", "EUR")

    with pytest.raises(Rejected) as refused:
        do(
            kernel,
            staff.tenders,
            sales.AddQuotationLine(
                quotation_id=quotation.id, item_id=_item(kernel, "VAC-NDV-1000").id, quantity=15
            ),
        )
    assert [(p.path, p.message) for p in refused.value.problems] == [
        (("quantity",), "must be a whole multiple of 10, as VAC-NDV-1000 is sold")
    ]

    keep(kernel, staff, sales.ItemSales, boxes, blocked_for_sale=True)
    with pytest.raises(Rejected, match=r"item EQP-COOLBOX is blocked for sale; it cannot be"):
        do(
            kernel,
            staff.tenders,
            sales.AddQuotationLine(quotation_id=quotation.id, item_id=boxes.id, quantity=2),
        )


def test_veterinary_medicines_are_sold_only_to_those_who_may_receive_them(
    kernel: Kernel, staff: Staff
) -> None:
    laboratory = do(
        kernel,
        staff.tenders,
        parties.RegisterParty(
            code="C-LARK",
            kind=parties.PartyKind.ORGANISATION,
            name="Larkspur Laboratory Services",
        ),
    )
    with pytest.raises(Rejected, match=r"currency is needed: C-LARK has none in its terms"):
        do(
            kernel,
            staff.tenders,
            sales.OpenQuotation(customer_id=laboratory.id, destination="KE"),
        )
    quotation = do(
        kernel,
        staff.tenders,
        sales.OpenQuotation(customer_id=laboratory.id, currency="GBP", destination="KE"),
    )
    vaccine = _item(kernel, "VAC-NDV-1000")
    adding = sales.AddQuotationLine(quotation_id=quotation.id, item_id=vaccine.id, quantity=100)
    with pytest.raises(Rejected, match=r"and C-LARK has no customer type recorded; it may be"):
        do(kernel, staff.tenders, adding)

    keep(
        kernel,
        staff,
        sales.CustomerTerms,
        laboratory,
        customer_type=sales.CustomerType.COMMERCIAL_LABORATORY,
    )
    with pytest.raises(
        Rejected,
        match=r"VAC-NDV-1000 is a veterinary medicine, and C-LARK is a commercial laboratory; "
        r"it may be sold only to a customer authorised to receive one",
    ):
        do(kernel, staff.tenders, adding)
    kits = sales.AddQuotationLine(
        quotation_id=quotation.id, item_id=_item(kernel, "KIT-AI-ELISA").id, quantity=2
    )
    assert do(kernel, staff.tenders, kits).quantity == 2


def test_a_tender_is_answered_only_with_what_it_may_take(kernel: Kernel, staff: Staff) -> None:
    centrifuge = _item(kernel, "EQP-CENTRIFUGE")
    keep(kernel, staff, sales.ItemSales, centrifuge, tender_eligible=False)
    quotation = do(
        kernel,
        staff.tenders,
        sales.OpenQuotation(
            customer_id=_party(kernel, "C-DLD").id, destination="TH", tender="DLD/T/2026/031"
        ),
    )
    with pytest.raises(
        Rejected,
        match=rf"EQP-CENTRIFUGE cannot be offered in a tender, and quotation {quotation.code} "
        r"answers tender DLD/T/2026/031",
    ):
        do(
            kernel,
            staff.tenders,
            sales.AddQuotationLine(quotation_id=quotation.id, item_id=centrifuge.id, quantity=1),
        )


def test_a_quotation_on_credit_over_the_customers_limit_must_be_approved(
    kernel: Kernel, staff: Staff
) -> None:
    quotation = do(
        kernel,
        staff.tenders,
        sales.OpenQuotation(
            customer_id=_party(kernel, "C-NVDL").id,
            destination="VN",
            valid_until=TODAY + timedelta(days=30),
        ),
    )
    do(
        kernel,
        staff.tenders,
        sales.AddQuotationLine(
            quotation_id=quotation.id, item_id=_item(kernel, "EQP-THERMO").id, quantity=9
        ),
    )
    total = latest(kernel, quotation).total
    # Nine thermocyclers listed at GBP 23,500.00 come to USD 267,721.56 at 79p.
    assert total == Money.of("267721.56", "USD")
    with pytest.raises(
        Rejected,
        match=rf"comes to {total} on credit, over C-NVDL's limit of USD 250000.00; it must be",
    ):
        do(kernel, staff.tenders, sales.SendQuotation(quotation_id=quotation.id))
    do(kernel, staff.md, sales.ApproveQuotation(quotation_id=quotation.id))
    sent = do(kernel, staff.tenders, sales.SendQuotation(quotation_id=quotation.id))
    assert sent.state == "sent"


def test_an_item_is_bought_from_its_default_manufacturer_unless_another_is_named(
    kernel: Kernel, staff: Staff
) -> None:
    vaccine = _item(kernel, "VAC-NDV-1000")
    nordvet, aurelian = _party(kernel, "S-NORDVET"), _party(kernel, "S-AURELIAN")
    do(
        kernel,
        staff.sourcing,
        pricelists.RecordPrice(
            supplier_id=aurelian.id,
            item_id=vaccine.id,
            cost=Money.of("39.00", "EUR"),
            effective=TODAY,
        ),
    )
    quotation = do(
        kernel,
        staff.tenders,
        sales.OpenQuotation(customer_id=_party(kernel, "C-DLD").id, destination="TH"),
    )
    adding = sales.AddQuotationLine(quotation_id=quotation.id, item_id=vaccine.id, quantity=100)
    assert do(kernel, staff.tenders, adding).supplier_id == nordvet.id

    keep(kernel, staff, pricelists.ItemSourcing, vaccine, supplier_id=None)
    with pytest.raises(Rejected, match=r"all offer VAC-NDV-1000; name the supplier"):
        do(kernel, staff.tenders, adding)
    named = adding.model_copy(update={"supplier_id": aurelian.id})
    assert do(kernel, staff.tenders, named).cost == Money.of("39.00", "EUR").convert(
        "GBP", rate=Decimal("0.85")
    ).times(100)


def test_goods_ruined_by_freezing_are_not_packed_with_goods_flown_frozen(
    kernel: Kernel, staff: Staff
) -> None:
    played = month.play(kernel, staff, TODAY)
    kits, vaccine = _item(kernel, "KIT-ASF-PCR"), _item(kernel, "VAC-NDV-1000")
    keep(
        kernel,
        staff,
        stock.ItemStock,
        kits,
        storage=stock.Storage.FROZEN,
        temperature_min=None,
        temperature_max=Decimal(-20),
        do_not_freeze=False,
    )
    keep(
        kernel,
        staff,
        airfreight.ItemShipping,
        kits,
        dry_ice=True,
        packaging=airfreight.Packaging.DRY_ICE_SHIPPER,
    )
    frozen = _receive(kernel, staff, kits, "ASF-2611-02", TODAY + timedelta(days=300))
    chilled = _receive(kernel, staff, vaccine, "ND-2611-09", TODAY + timedelta(days=400))
    waybill = do(kernel, staff.logistics, airfreight.OpenWaybill(sales_order_id=played.order.id))
    do(
        kernel,
        staff.logistics,
        airfreight.PackLine(
            waybill_id=waybill.id,
            sales_order_line_id=_line(kernel, played.order, "KIT-ASF-PCR").id,
            batch_id=frozen.id,
            quantity=6,
        ),
    )
    with pytest.raises(
        Rejected,
        match=r"VAC-NDV-1000 cannot be packed: waybill WB0001 carries KIT-ASF-PCR, shipped "
        r"frozen; pack it on a waybill of its own",
    ):
        do(
            kernel,
            staff.logistics,
            airfreight.PackLine(
                waybill_id=waybill.id,
                sales_order_line_id=_line(kernel, played.order, "VAC-NDV-1000").id,
                batch_id=chilled.id,
                quantity=100,
            ),
        )

    filled = _fill(kernel, staff, waybill, temperature=airfreight.TemperatureRange.COL)
    with pytest.raises(Rejected) as refused:
        do(kernel, staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id))
    assert {p.path: p.message for p in refused.value.problems} == {
        ("temperature",): "COL would not keep KIT-ASF-PCR (-20 °C or colder) within its limits",
        ("dry_ice",): "is needed: KIT-ASF-PCR flies on dry ice",
        ("temperature_logger",): "is needed: KIT-ASF-PCR flies with a temperature logger",
    }
    filled = _fill(
        kernel,
        staff,
        filled,
        temperature=airfreight.TemperatureRange.FRO,
        dry_ice=Decimal(15),
        temperature_logger="TL-40-30577",
    )
    assert filled.dangerous_goods == "UN1845 Dry ice, class 9, 15 kg"
    assert do(kernel, staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id)).state == (
        "packed"
    )


def test_a_waybill_declares_the_dangerous_goods_packed_and_needs_their_declaration(
    kernel: Kernel, staff: Staff
) -> None:
    played = month.play(kernel, staff, TODAY)
    kits, rapid = _item(kernel, "KIT-AI-ELISA"), _item(kernel, "KIT-BRU-RBT")
    keep(
        kernel,
        staff,
        airfreight.ItemShipping,
        kits,
        un_number="UN1170",
        hazard_class=airfreight.HazardClass.FLAMMABLE_LIQUID,
        packing_group=airfreight.PackingGroup.II,
        proper_shipping_name="Ethanol solution",
        packaging=airfreight.Packaging.UN_SPECIFICATION,
    )
    keep(kernel, staff, airfreight.ItemShipping, rapid, biological_substance=True)
    waybill = do(kernel, staff.logistics, airfreight.OpenWaybill(sales_order_id=played.order.id))
    for item, code in ((kits, "KIT-AI-ELISA"), (rapid, "KIT-BRU-RBT")):
        batch = _receive(kernel, staff, item, f"{code[4:7]}-2611-01", TODAY + timedelta(days=300))
        do(
            kernel,
            staff.logistics,
            airfreight.PackLine(
                waybill_id=waybill.id,
                sales_order_line_id=_line(kernel, played.order, code).id,
                batch_id=batch.id,
                quantity=_line(kernel, played.order, code).quantity,
            ),
        )

    assert latest(kernel, waybill).dangerous_goods == (
        "UN1170 Ethanol solution, class 3, PG II\nUN3373 Biological substance, category B"
    )
    missing = kernel.ask(airfreight.WhatIsMissing(waybill_id=waybill.id), actor=ROOT)
    assert "Shipper's declaration for dangerous goods" in missing.documents
    assert "Veterinary health certificate" not in missing.documents


def test_goods_wait_for_the_customers_import_permit_and_are_booked_as_kept(
    kernel: Kernel, staff: Staff
) -> None:
    played = month.play(kernel, staff, TODAY)
    customer = _party(kernel, "C-NVDL")
    waybill = do(kernel, staff.logistics, airfreight.OpenWaybill(sales_order_id=played.order.id))
    assert waybill.destination_airport == "HAN"
    boxes = _line(kernel, played.order, "EQP-COOLBOX")
    stocked = every(kernel, stock.Batch, batch=f"COOLBOX-{TODAY:%y%m}-01")[0]
    do(
        kernel,
        staff.logistics,
        airfreight.PackLine(
            waybill_id=waybill.id, sales_order_line_id=boxes.id, batch_id=stocked.id, quantity=20
        ),
    )
    _fill(kernel, staff, waybill, temperature=airfreight.TemperatureRange.AMB)
    do(kernel, staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id))
    do(
        kernel,
        staff.logistics,
        airfreight.BookFreight(
            waybill_id=waybill.id, airline="Kittiwake Air Cargo", awb_number="917-48213373"
        ),
    )
    certificate = every(kernel, airfreight.Requirement, document="Veterinary health certificate")
    with pytest.raises(
        Rejected,
        match=r"Veterinary health certificate is required only for goods that need a veterinary "
        r"health certificate; nothing packed on waybill WB0001 needs it",
    ):
        _hold(kernel, staff, waybill, certificate[0])
    missing = kernel.ask(airfreight.WhatIsMissing(waybill_id=waybill.id), actor=ROOT)
    assert missing.documents == ("Veterinary import permit", "Certificate of origin")
    for requirement in every(kernel, airfreight.Requirement, country="VN"):
        _hold(kernel, staff, waybill, requirement)
    do(kernel, staff.logistics, airfreight.CompleteWaybill(waybill_id=waybill.id))

    lapsed = TODAY - timedelta(days=1)
    keep(kernel, staff, sales.CustomerTerms, customer, import_permit_expires=lapsed)
    dispatching = airfreight.DispatchWaybill(waybill_id=waybill.id)
    with pytest.raises(
        Rejected,
        match=rf"C-NVDL's import permit VN-IP-2026-0412 lapsed on {lapsed}; record its renewal",
    ):
        do(kernel, staff.logistics, dispatching)
    keep(
        kernel,
        staff,
        sales.CustomerTerms,
        customer,
        import_permit="VN-IP-2027-0031",
        import_permit_expires=TODAY + timedelta(days=365),
    )
    do(kernel, staff.logistics, dispatching)

    invoice = do(kernel, staff.logistics, invoicing.RaiseInvoice(waybill_id=waybill.id))
    [line] = every(kernel, invoicing.InvoiceLine, invoice_id=invoice.id)
    assert (line.commodity_code, line.country_of_origin) == ("39231090", "GB")
    # The cool boxes' sales are recorded with the equipment's, the carriage apart.
    earned = line.amount.convert("GBP", rate=USD)
    charged = (invoice.freight + invoice.insurance).convert("GBP", rate=USD)
    assert (_balance(kernel, "4040"), _balance(kernel, "4100")) == (-earned.amount, -charged.amount)
    assert _balance(kernel, "4000") == 0


def _host(kernel: Kernel, kind: type[Extension[Any]], code: str) -> Document:
    return (
        _party(kernel, code)
        if kind in kernel.registry.extensions(parties.Party)
        else _item(kernel, code)
    )


def _ordered(
    kernel: Kernel, staff: Staff, customer: str, *lines: tuple[str, int]
) -> sales.SalesOrder:
    """An order won from a customer for these items, at their list prices."""
    party = _party(kernel, customer)
    country = every(kernel, parties.Address, party_id=party.id)[0].country
    opening = sales.OpenQuotation(
        customer_id=party.id, destination=country, valid_until=TODAY + timedelta(days=30)
    )
    quotation = do(kernel, staff.tenders, opening)
    for code, quantity in lines:
        do(
            kernel,
            staff.tenders,
            sales.AddQuotationLine(
                quotation_id=quotation.id, item_id=_item(kernel, code).id, quantity=quantity
            ),
        )
    do(kernel, staff.tenders, sales.SendQuotation(quotation_id=quotation.id))
    return do(kernel, staff.tenders, sales.WinQuotation(quotation_id=quotation.id))


def _receive(
    kernel: Kernel, staff: Staff, item: items.Item, batch: str, expiry: object
) -> stock.Batch:
    return do(
        kernel,
        staff.md,
        stock.ReceiveBatch.model_validate(
            {
                "item_id": item.id,
                "batch": batch,
                "expiry": expiry,
                "quantity": 500,
                "unit_cost": Money.of("40.00", "GBP"),
                "certificate": f"COA-{batch}",
            }
        ),
    )


def _fill(
    kernel: Kernel, staff: Staff, waybill: airfreight.Waybill, **given: object
) -> airfreight.Waybill:
    current = latest(kernel, waybill)
    command = airfreight.FillWaybill
    held = {
        f: getattr(current, f) for f in command.model_fields if f not in ("waybill_id", "version")
    }
    sized = {
        "origin_airport": "LHR",
        "pieces": 2,
        "gross_weight": Decimal("24.00"),
        "dimensions": "2 boxes of 60 x 40 x 40 cm",
    }
    fields = held | sized | given | {"waybill_id": current.id, "version": current.version}
    return do(kernel, staff.export_documents, command.model_validate(fields))


def _hold(
    kernel: Kernel, staff: Staff, waybill: airfreight.Waybill, requirement: airfreight.Requirement
) -> airfreight.ShippingDocument:
    return do(
        kernel,
        staff.export_documents,
        airfreight.HoldShippingDocument(
            waybill_id=waybill.id,
            requirement_id=requirement.id,
            reference=f"VN-2026-{requirement.document[:3].upper()}",
            issued=TODAY,
        ),
    )


def _line(kernel: Kernel, order: sales.SalesOrder, code: str) -> sales.SalesOrderLine:
    item = _item(kernel, code)
    return every(kernel, sales.SalesOrderLine, sales_order_id=order.id, item_id=item.id)[0]


def _item(kernel: Kernel, code: str) -> items.Item:
    return every(kernel, items.Item, code=code)[0]


def _party(kernel: Kernel, code: str) -> parties.Party:
    return every(kernel, parties.Party, code=code)[0]


def _balance(kernel: Kernel, code: str) -> Decimal:
    account = every(kernel, ledger.Account, code=code)[0]
    return kernel.ask(ledger.AccountBalance(account_id=account.id), actor=ROOT).base.amount
