"""Kestrel Animal Health's month, played out on PostgreSQL, and its rules held to.

Kestrel's modules are an example's custom modules, written against the kernel's public
API and the standard modules' as any business's would be (examples/kestrel-animal-health).
This plays its month: the setup and first weeks as `kestrel.month` does them, and the
rest by hand, from approving the purchase order over the limit to the customer's
payment. Along the way it holds Kestrel's documents to their rules: the totals worked
out on every header and line, the amendments each refuses, and what each fixes once it
has gone to the customer or the manufacturer.
"""

from datetime import date, timedelta
from decimal import Decimal

import kestrel
import pytest
from conftest import ROOT, every
from kestrel import airfreight, fx, invoicing, month, pricelists, purchasing, sales, stock
from kestrel.common import Incoterm
from kestrel.month import Person, Staff

from remit import access, items, ledger, parties
from remit.kernel import (
    Actor,
    Command,
    Document,
    Kernel,
    Money,
    PostgresStore,
    Rejected,
    Unauthorised,
)

ISSUER = "https://id.example.com/realms/kestrel"
PEOPLE = {
    "md": Person("Morgan Hale", access.MemberKind.HUMAN, "MD", "md"),
    "tenders": Person("Priya Shah", access.MemberKind.HUMAN, "TENDERS", "tenders"),
    "logistics": Person("Tom Okafor", access.MemberKind.HUMAN, "LOGISTICS", "logistics"),
    "sourcing": Person("Sourcing agent", access.MemberKind.AGENT, "SOURCING", "sourcing"),
    "export_documents": Person(
        "Export documents agent", access.MemberKind.AGENT, "EXPORT_DOCUMENTS", "export"
    ),
}
TODAY = date.today()
USD = Decimal("0.7900")
"""What a dollar is worth in pounds all month, as the setup records it."""


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


def do[R](kernel: Kernel, actor: Actor, command: Command[R]) -> R:
    return kernel.execute(command, actor=actor).result


def amend[R](
    kernel: Kernel, actor: Actor, kind: type[Command[R]], document: Document, **given: object
) -> R:
    """Send an amendment as a client does: every field as the document holds it now, at
    the version it is at, with those given replaced."""
    current = latest(kernel, document)
    named = kind.amends
    assert named is not None
    held = {f: getattr(current, f) for f in kind.model_fields if f not in (named, "version")}
    command = kind.model_validate(held | {named: current.id, "version": current.version} | given)
    return do(kernel, actor, command)


def latest[D: Document](kernel: Kernel, document: D) -> D:
    return every(kernel, type(document), id=document.id)[0]


KESTREL = ("fx", "pricelists", "stock", "sales", "purchasing", "airfreight", "invoicing")


def test_every_field_of_kestrel_s_documents_is_described(kernel: Kernel) -> None:
    described = kernel.registry.describe()
    undescribed = [
        f"{name}.{field}"
        for module in KESTREL
        for name, document in described[module]["documents"].items()
        for field, schema in document["schema"]["properties"].items()
        if field not in {"id", "version", "state"} and not schema.get("description")
    ]
    assert undescribed == []


def test_every_field_kestrel_adds_to_items_and_parties_is_described(kernel: Kernel) -> None:
    described = kernel.registry.describe()
    added = {
        f"{module}.{name}.{field}": schema
        for module, name in (("items", "items.item"), ("parties", "parties.party"))
        for field, schema in described[module]["documents"][name]["schema"]["properties"].items()
        if schema.get("x-module") in KESTREL
    }
    assert len(added) > 60
    assert [field for field, schema in added.items() if not schema.get("description")] == []
    summaries = [
        extension["summary"]
        for module in KESTREL
        for extension in described[module]["extensions"].values()
    ]
    assert len(summaries) == 9
    assert all(summaries)


def test_the_first_weeks_leave_work_waiting(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)
    tender, order = played.tender, played.order

    assert tender.state == "won"
    # USD 135,430.00 listed, less 2.5% off the two vaccines, with carriage and insurance.
    assert tender.discount == Money.of("2377.50", "USD")
    assert tender.net == Money.of("133052.50", "USD")
    assert tender.total == Money.of("138322.50", "USD")
    lines = every(kernel, sales.QuotationLine, quotation_id=tender.id)
    assert tender.net == Money.total((line.amount for line in lines), "USD")
    assert tender.cost == Money.total((line.cost for line in lines), "GBP")
    assert tender.margin == tender.net.convert("GBP", rate=USD) - tender.cost
    vaccine = next(line for line in lines if line.discount_percent)
    assert vaccine.amount == vaccine.unit_price.times(vaccine.quantity * Decimal("0.975"))

    # The order is the quotation as accepted, promised by the longest lead time.
    assert (order.net, order.total, order.margin) == (tender.net, tender.total, tender.margin)
    assert order.customer_po == "NVDL-PO-2026-0388"
    assert order.promised_delivery == tender.quoted + timedelta(days=56)
    assert order.deposit_due == Money.of("41496.75", "USD")
    assert order.deposit_received == order.deposit_due
    ordered = every(kernel, sales.SalesOrderLine, sales_order_id=order.id)
    assert [line.outstanding for line in ordered] == [line.quantity for line in lines]

    assert (played.received.state, played.part_received.state) == ("received", "confirmed")
    assert played.part_received.supplier_reference == "AB-26-55107"
    assert played.over_limit.state == "draft"
    assert played.thin.state == "draft"

    # Calder's goods arrived with their paperwork, into stock at cost.
    [receipt, *_] = every(kernel, purchasing.GoodsReceipt, purchase_order_id=played.received.id)
    assert receipt.delivery_note == f"DN-{played.received.code}-1"
    batch = every(kernel, stock.Batch, id=receipt.batch_id)[0]
    assert (batch.supplier_id, batch.location) == (played.received.supplier_id, "W2-B11")
    assert batch.available == batch.on_hand == receipt.quantity
    assert batch.value == batch.unit_cost.times(batch.on_hand)


def test_the_managing_director_approves_what_is_over_the_limit(
    kernel: Kernel, staff: Staff
) -> None:
    played = month.play(kernel, staff, TODAY)

    with pytest.raises(Rejected, match=r"over the GBP 15000\.00 limit; it must be approved"):
        do(
            kernel,
            staff.tenders,
            purchasing.SendPurchaseOrder(purchase_order_id=played.over_limit.id),
        )
    with pytest.raises(Unauthorised):
        do(
            kernel,
            staff.tenders,
            purchasing.ApprovePurchaseOrder(purchase_order_id=played.over_limit.id),
        )
    do(kernel, staff.md, purchasing.ApprovePurchaseOrder(purchase_order_id=played.over_limit.id))
    sent = do(
        kernel, staff.tenders, purchasing.SendPurchaseOrder(purchase_order_id=played.over_limit.id)
    )
    assert sent.state == "sent"

    with pytest.raises(Rejected, match=r"below the 12% floor; it must be approved"):
        do(kernel, staff.tenders, sales.SendQuotation(quotation_id=played.thin.id))
    do(kernel, staff.md, sales.ApproveQuotation(quotation_id=played.thin.id))
    assert (
        do(kernel, staff.tenders, sales.SendQuotation(quotation_id=played.thin.id)).state == "sent"
    )


def test_agents_work_only_within_their_roles(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)

    with pytest.raises(Unauthorised):
        do(kernel, staff.sourcing, sales.WithdrawQuotation(quotation_id=played.thin.id))
    with pytest.raises(Unauthorised):
        amend(kernel, staff.sourcing, sales.AmendQuotation, played.thin, notes="Cheaper")
    with pytest.raises(Unauthorised):
        do(kernel, staff.export_documents, airfreight.OpenWaybill(sales_order_id=played.order.id))


def test_a_quotations_totals_follow_every_change_to_its_lines(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)
    thin = played.thin
    eur = Decimal("0.8500")

    def held_to_its_lines() -> sales.Quotation:
        quotation = latest(kernel, thin)
        lines = every(kernel, sales.QuotationLine, quotation_id=thin.id, state="current")
        listed = Money.total((line.unit_price.times(line.quantity) for line in lines), "EUR")
        net = Money.total((line.amount for line in lines), "EUR")
        assert quotation.net == net
        assert quotation.discount == listed - net
        assert quotation.total == net + quotation.freight + quotation.insurance
        assert quotation.cost == Money.total((line.cost for line in lines), "GBP")
        assert quotation.margin == net.convert("GBP", rate=eur) - quotation.cost
        return quotation

    before = held_to_its_lines()
    kits = do(
        kernel,
        staff.tenders,
        sales.AddQuotationLine(
            quotation_id=thin.id,
            item_id=_item(kernel, "KIT-ASF-PCR").id,
            quantity=12,
            unit_price=Money.of("1290.00", "EUR"),
            notes="Delivered with the ELISA kits",
        ),
    )
    # 12 kits at EUR 880.00 from Aurelian, at 85p to the euro.
    assert (kits.line_number, kits.unit, kits.unit_cost) == (3, "KT", Money.of("880.00", "EUR"))
    assert kits.cost == Money.of("8976.00", "GBP")
    assert kits.delivery == thin.quoted + timedelta(days=14)
    assert held_to_its_lines().net == before.net + Money.of("15480.00", "EUR")

    kits = amend(
        kernel, staff.tenders, sales.AmendQuotationLine, kits, quantity=10, discount_percent=5
    )
    # 10 at EUR 1,290.00 less 5%, costing 10 at EUR 880.00: it earns 28.19%.
    assert kits.amount == Money.of("12255.00", "EUR")
    assert kits.cost == Money.of("7480.00", "GBP")
    assert kits.margin_percent == Decimal("28.19")
    assert held_to_its_lines().discount == Money.of("645.00", "EUR")

    carried = amend(
        kernel,
        staff.tenders,
        sales.AmendQuotation,
        thin,
        freight=Money.of("2100.00", "EUR"),
        insurance=Money.of("160.00", "EUR"),
    )
    assert carried.total == carried.net + Money.of("2260.00", "EUR")
    held_to_its_lines()

    removed = do(kernel, staff.tenders, sales.RemoveQuotationLine(quotation_line_id=kits.id))
    assert removed.state == "removed"
    assert held_to_its_lines().net == before.net


def test_a_line_bought_elsewhere_is_costed_at_that_manufacturers_price(
    kernel: Kernel, staff: Staff
) -> None:
    played = month.play(kernel, staff, TODAY)
    elisa = _quoted(kernel, played.thin, "KIT-AI-ELISA")
    calder = _party(kernel, "S-CALDER")
    do(
        kernel,
        staff.sourcing,
        pricelists.RecordPrice(
            supplier_id=calder.id,
            item_id=elisa.item_id,
            cost=Money.of("530.00", "GBP"),
            effective=TODAY.replace(day=1),
            lead_time_days=10,
        ),
    )

    moved = amend(kernel, staff.tenders, sales.AmendQuotationLine, elisa, supplier_id=calder.id)
    assert (moved.unit_cost, moved.cost) == (
        Money.of("530.00", "GBP"),
        Money.of("21200.00", "GBP"),
    )
    assert moved.price_id != elisa.price_id
    quotation = latest(kernel, played.thin)
    assert quotation.margin - played.thin.margin == elisa.cost - moved.cost

    with pytest.raises(Rejected, match=r"S-NORDVET has no current price for KIT-AI-ELISA"):
        amend(
            kernel,
            staff.tenders,
            sales.AmendQuotationLine,
            moved,
            supplier_id=_party(kernel, "S-NORDVET").id,
        )


def test_a_quotation_changes_only_while_it_is_a_draft(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)
    thin = played.thin
    tender_line = every(kernel, sales.QuotationLine, quotation_id=played.tender.id)[0]

    with pytest.raises(
        Rejected, match=r"quotation Q\d{4} is won; its lines change only while it is a draft"
    ):
        amend(kernel, staff.tenders, sales.AmendQuotationLine, tender_line, quantity=1)
    with pytest.raises(Rejected, match=r"is won; it changes only while it is a draft"):
        amend(kernel, staff.tenders, sales.AmendQuotation, played.tender, notes="Late change")

    do(kernel, staff.md, sales.ApproveQuotation(quotation_id=thin.id))
    elisa = _quoted(kernel, thin, "KIT-AI-ELISA")
    with pytest.raises(
        Rejected,
        match=r"is approved; reopen it to change its lines, and have it approved again",
    ):
        amend(kernel, staff.tenders, sales.AmendQuotationLine, elisa, quantity=30)
    do(kernel, staff.tenders, sales.ReopenQuotation(quotation_id=thin.id))
    shorter = amend(kernel, staff.tenders, sales.AmendQuotationLine, elisa, quantity=30)
    assert shorter.quantity == 30

    with pytest.raises(Rejected, match=r"has changed since version"):
        amend(
            kernel,
            staff.tenders,
            sales.AmendQuotationLine,
            elisa,
            quantity=25,
            version=elisa.version,
        )
    with pytest.raises(Rejected, match=r"nothing would change"):
        amend(kernel, staff.tenders, sales.AmendQuotationLine, shorter)

    stranger = every(kernel, parties.Contact, party_id=played.tender.customer_id)[0]
    with pytest.raises(Rejected, match=r"contact_id is not a current contact of C-DLD") as refused:
        amend(kernel, staff.tenders, sales.AmendQuotation, thin, contact_id=stranger.id)
    assert [problem.path for problem in refused.value.problems] == [("contact_id",)]
    with pytest.raises(Rejected, match=r"give freight in EUR"):
        amend(kernel, staff.tenders, sales.AmendQuotation, thin, freight=Money.of("900", "USD"))
    with pytest.raises(Rejected, match=r"valid_until is before the quotation's date"):
        amend(
            kernel,
            staff.tenders,
            sales.AmendQuotation,
            thin,
            valid_until=thin.quoted - timedelta(days=1),
        )


def test_purchase_orders_keep_what_was_agreed_once_sent(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)
    confirmed, draft = played.part_received, played.over_limit

    later = TODAY + timedelta(days=21)
    moved = amend(kernel, staff.tenders, purchasing.AmendPurchaseOrder, confirmed, expected=later)
    assert moved.expected == later
    with pytest.raises(
        Rejected,
        match=r"is confirmed; what was agreed with the manufacturer \(incoterm\) changes only "
        r"while it is a draft",
    ):
        amend(
            kernel,
            staff.tenders,
            purchasing.AmendPurchaseOrder,
            confirmed,
            incoterm=Incoterm.EXW,
        )
    waiting = every(
        kernel, purchasing.PurchaseOrderLine, purchase_order_id=confirmed.id, state="open"
    )[0]
    with pytest.raises(Rejected, match=r"what its lines order \(quantity\) changes only while"):
        amend(kernel, staff.tenders, purchasing.AmendPurchaseOrderLine, waiting, quantity=1)
    with pytest.raises(Rejected, match=r"is received; it can no longer change"):
        amend(kernel, staff.tenders, purchasing.AmendPurchaseOrder, played.received, notes="Late")

    # While a draft, a line's discount and the delivery charge change, and the totals with
    # them: the approval limit is held to the total in pounds.
    vaccines = every(kernel, purchasing.PurchaseOrderLine, purchase_order_id=draft.id)[0]
    cheaper = amend(
        kernel, staff.tenders, purchasing.AmendPurchaseOrderLine, vaccines, discount_percent=4
    )
    assert cheaper.amount == vaccines.amount.times(Decimal("0.96"))
    charged = amend(
        kernel,
        staff.tenders,
        purchasing.AmendPurchaseOrder,
        draft,
        freight=Money.of("3200.00", "DKK"),
    )
    lines = every(kernel, purchasing.PurchaseOrderLine, purchase_order_id=draft.id)
    assert charged.net == Money.total((line.amount for line in lines), "DKK")
    assert charged.total == charged.net + Money.of("3200.00", "DKK")
    assert charged.total_base == charged.total.convert("GBP", rate=Decimal("0.1140"))
    with pytest.raises(Rejected, match=r"this line can buy 400, not 401"):
        amend(kernel, staff.tenders, purchasing.AmendPurchaseOrderLine, cheaper, quantity=401)

    removed = do(
        kernel,
        staff.tenders,
        purchasing.RemovePurchaseOrderLine(purchase_order_line_id=cheaper.id),
    )
    assert removed.state == "removed"
    assert latest(kernel, draft).net == charged.net - cheaper.amount


def test_nothing_is_bought_or_packed_beyond_what_was_sold(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)
    line = every(kernel, sales.SalesOrderLine, sales_order_id=played.order.id)[0]
    unattached = do(
        kernel,
        staff.tenders,
        purchasing.RaisePurchaseOrder(
            supplier_id=line.supplier_id, currency=line.unit_cost.currency
        ),
    )
    with pytest.raises(Rejected, match=r"paid for by programme RAHP-24-117, whose reference"):
        do(
            kernel,
            staff.tenders,
            purchasing.AddPurchaseOrderLine(
                purchase_order_id=unattached.id, sales_order_line_id=line.id
            ),
        )
    extra = do(
        kernel,
        staff.tenders,
        purchasing.RaisePurchaseOrder(
            supplier_id=line.supplier_id,
            currency=line.unit_cost.currency,
            sales_order_id=played.order.id,
        ),
    )
    assert extra.programme == "RAHP-24-117"
    with pytest.raises(Rejected, match=r"are already on purchase orders; 0 more can be bought"):
        do(
            kernel,
            staff.tenders,
            purchasing.AddPurchaseOrderLine(
                purchase_order_id=extra.id, sales_order_line_id=line.id
            ),
        )

    waybill = do(kernel, staff.logistics, airfreight.OpenWaybill(sales_order_id=played.order.id))
    assert (waybill.destination, waybill.programme) == ("VN", "RAHP-24-117")
    boxes = _line(kernel, played.order, "EQP-COOLBOX")
    batch = _batch(kernel, "EQP-COOLBOX", f"COOLBOX-{TODAY:%y%m}-01")
    with pytest.raises(Rejected, match=r"20 more can be packed, not 21"):
        do(
            kernel,
            staff.logistics,
            airfreight.PackLine(
                waybill_id=waybill.id, sales_order_line_id=boxes.id, batch_id=batch.id, quantity=21
            ),
        )
    with pytest.raises(Rejected, match=r"20 of line 7 are still to ship, not 21"):
        do(kernel, ROOT, sales.ShipOrderLine(sales_order_line_id=boxes.id, quantity=21))
    with pytest.raises(Rejected, match=r"0 of line 7 have shipped and 0 are invoiced"):
        do(kernel, ROOT, sales.InvoiceOrderLine(sales_order_line_id=boxes.id, quantity=1))


def test_batches_are_corrected_from_their_paperwork(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)
    [receipt] = every(kernel, purchasing.GoodsReceipt, purchase_order_id=played.part_received.id)
    batch = every(kernel, stock.Batch, id=receipt.batch_id)[0]
    assert (batch.certificate, receipt.data_logger) == (f"COA-{batch.batch}", "TL-40-21877")

    moved = amend(
        kernel, staff.logistics, stock.AmendBatch, batch, location="CR2-B07", notes="Top shelf"
    )
    assert (moved.location, moved.notes, moved.on_hand) == ("CR2-B07", "Top shelf", 10)
    assert batch.manufactured is not None
    with pytest.raises(Rejected, match=r"cannot expire on .*, before it"):
        amend(kernel, staff.logistics, stock.AmendBatch, moved, expiry=batch.manufactured)


def test_expired_stock_is_not_packed_and_is_written_off(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)
    expired = _batch(kernel, "KIT-BRU-RBT", "RBT-2410-07")
    waybill = do(kernel, staff.logistics, airfreight.OpenWaybill(sales_order_id=played.order.id))
    kits = _line(kernel, played.order, "KIT-BRU-RBT")
    with pytest.raises(Rejected, match=r"batch RBT-2410-07 expired on"):
        do(
            kernel,
            staff.logistics,
            airfreight.PackLine(
                waybill_id=waybill.id, sales_order_line_id=kits.id, batch_id=expired.id, quantity=4
            ),
        )

    written_off = do(kernel, staff.logistics, stock.WriteOffBatch(batch_id=expired.id))
    assert (written_off.state, written_off.on_hand, written_off.value) == (
        "written_off",
        0,
        Money.of("0.00", "GBP"),
    )
    assert _balance(kernel, "7900") == Decimal("1368.00")
    with pytest.raises(Rejected, match=r"batch RBT-2410-07 is written_off; it can no longer"):
        amend(kernel, staff.logistics, stock.AmendBatch, written_off, location="BIN")


def test_work_can_be_undone_or_abandoned_before_it_is_final(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)

    first, _ = every(kernel, sales.QuotationLine, quotation_id=played.thin.id)
    removed = do(kernel, staff.tenders, sales.RemoveQuotationLine(quotation_line_id=first.id))
    do(kernel, staff.md, sales.ApproveQuotation(quotation_id=played.thin.id))
    reopened = do(kernel, staff.tenders, sales.ReopenQuotation(quotation_id=played.thin.id))
    do(kernel, staff.md, sales.ApproveQuotation(quotation_id=played.thin.id))
    do(kernel, staff.tenders, sales.SendQuotation(quotation_id=played.thin.id))
    lost = do(
        kernel,
        staff.tenders,
        sales.LoseQuotation(quotation_id=played.thin.id, reason=sales.LossReason.PRICE),
    )
    spare = do(
        kernel,
        staff.tenders,
        sales.OpenQuotation(customer_id=played.thin.customer_id, currency="EUR", destination="TH"),
    )
    withdrawn = do(kernel, staff.tenders, sales.WithdrawQuotation(quotation_id=spare.id))
    assert (removed.state, reopened.state, lost.state, withdrawn.state) == (
        "removed",
        "draft",
        "lost",
        "withdrawn",
    )
    assert lost.lost_reason is sales.LossReason.PRICE

    cancelled = do(
        kernel,
        staff.tenders,
        purchasing.CancelPurchaseOrder(purchase_order_id=played.over_limit.id),
    )
    assert {
        line.state
        for line in every(kernel, purchasing.PurchaseOrderLine, purchase_order_id=cancelled.id)
    } == {"cancelled"}
    [licence] = every(kernel, airfreight.Requirement, country="TH", document="Import licence")
    retired = do(kernel, staff.logistics, airfreight.RetireRequirement(requirement_id=licence.id))
    assert (cancelled.state, retired.state) == ("cancelled", "retired")
    with pytest.raises(Rejected, match=r"holds a deposit of USD 41496.75; it cannot be cancelled"):
        do(kernel, staff.md, sales.CancelOrder(sales_order_id=played.order.id))

    waybill = do(kernel, staff.logistics, airfreight.OpenWaybill(sales_order_id=played.order.id))
    boxes = _line(kernel, played.order, "EQP-COOLBOX")
    batch = _batch(kernel, "EQP-COOLBOX", f"COOLBOX-{TODAY:%y%m}-01")
    packing = do(
        kernel,
        staff.logistics,
        airfreight.PackLine(
            waybill_id=waybill.id, sales_order_line_id=boxes.id, batch_id=batch.id, quantity=20
        ),
    )
    assert latest(kernel, waybill).customs_value == Money.of("6200.00", "USD")
    labelled = amend(kernel, staff.logistics, airfreight.AmendPackingLine, packing, package="1-20")
    assert labelled.package == "1-20"
    amend(
        kernel,
        staff.export_documents,
        airfreight.FillWaybill,
        waybill,
        pieces=20,
        gross_weight=Decimal("96.00"),
        dimensions="20 boxes of 50 x 40 x 40 cm",
        temperature=airfreight.TemperatureRange.AMB,
    )
    do(kernel, staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id))
    with pytest.raises(Rejected, match=r"waybill WB0001 is packed; reopen packing to change it"):
        amend(kernel, staff.logistics, airfreight.AmendPackingLine, labelled, package="1-10")
    packing_again = do(kernel, staff.logistics, airfreight.ReopenPacking(waybill_id=waybill.id))
    unpacked = do(kernel, staff.logistics, airfreight.UnpackLine(packing_line_id=packing.id))
    assert (packing_again.state, unpacked.state) == ("packing", "unpacked")
    assert _batch(kernel, "EQP-COOLBOX", batch.batch).allocated == 0
    assert latest(kernel, waybill).customs_value == Money.of("0.00", "USD")


def test_the_month_ends_with_a_zero_rated_invoice_traced_to_its_quotation_and_paid(
    kernel: Kernel, staff: Staff
) -> None:
    played = month.play(kernel, staff, TODAY)
    do(kernel, staff.md, purchasing.ApprovePurchaseOrder(purchase_order_id=played.over_limit.id))
    do(kernel, staff.tenders, purchasing.SendPurchaseOrder(purchase_order_id=played.over_limit.id))
    for po in (played.over_limit, played.part_received):
        for line in every(
            kernel, purchasing.PurchaseOrderLine, purchase_order_id=po.id, state="open"
        ):
            do(
                kernel,
                staff.logistics,
                purchasing.ReceiveGoods(
                    purchase_order_line_id=line.id,
                    quantity=line.quantity - line.received,
                    batch=f"B-{line.id.hex[:6].upper()}",
                    expiry=date(TODAY.year + 2, 1, 31),
                    storage=stock.Storage.CHILLED,
                    temperature=Decimal("5.0"),
                    certificate=f"COA-{line.id.hex[:6].upper()}",
                ),
            )

    waybill = do(kernel, staff.logistics, airfreight.OpenWaybill(sales_order_id=played.order.id))
    for line in every(kernel, sales.SalesOrderLine, sales_order_id=played.order.id):
        (receipt,) = [
            r
            for po_line in every(kernel, purchasing.PurchaseOrderLine, sales_order_line_id=line.id)
            for r in every(kernel, purchasing.GoodsReceipt, purchase_order_line_id=po_line.id)
        ]
        do(
            kernel,
            staff.logistics,
            airfreight.PackLine(
                waybill_id=waybill.id,
                sales_order_line_id=line.id,
                batch_id=receipt.batch_id,
                quantity=line.quantity,
            ),
        )
    with pytest.raises(
        Rejected, match=r"fill in pieces, gross_weight, dimensions, temperature first"
    ):
        do(kernel, staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id))
    waybill = amend(
        kernel,
        staff.export_documents,
        airfreight.FillWaybill,
        waybill,
        origin_airport="LHR",
        destination_airport="HAN",
        pieces=9,
        gross_weight=Decimal("412.50"),
        volume=Decimal("3.120"),
        dimensions="9 boxes of 120 x 80 x 36 cm",
        temperature=airfreight.TemperatureRange.COL,
    )
    # 3.12 m³ at 6,000 cm³ to the kilo is 520 kg, more than the 412.5 kg they weigh.
    assert waybill.chargeable_weight == Decimal(520)
    assert waybill.customs_value == played.order.net
    with pytest.raises(Rejected, match=r"temperature_logger is needed: VAC-NDV-1000, "):
        do(kernel, staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id))
    amend(
        kernel,
        staff.export_documents,
        airfreight.FillWaybill,
        waybill,
        temperature_logger="TL-40-30112",
    )
    do(kernel, staff.logistics, airfreight.ClosePacking(waybill_id=waybill.id))
    with pytest.raises(Rejected, match=r"ends in 4, but its check digit should be 3") as refused:
        do(
            kernel,
            staff.logistics,
            airfreight.BookFreight(
                waybill_id=waybill.id, airline="Kittiwake Air Cargo", awb_number="917-48213374"
            ),
        )
    assert [problem.path for problem in refused.value.problems] == [("awb_number",)]
    do(
        kernel,
        staff.logistics,
        airfreight.BookFreight(
            waybill_id=waybill.id, airline="Kittiwake Air Cargo", awb_number="917-48213373"
        ),
    )

    # Vietnam requires its two; the vaccines need a health certificate, and the African
    # swine fever kits an export licence, wherever they go.
    required = (
        "Veterinary import permit",
        "Certificate of origin",
        "Veterinary health certificate",
        "Export licence",
    )
    with pytest.raises(Rejected, match=rf"waybill WB0001 to VN needs {', '.join(required)},"):
        do(kernel, staff.logistics, airfreight.CompleteWaybill(waybill_id=waybill.id))
    missing = kernel.ask(
        airfreight.WhatIsMissing(waybill_id=waybill.id), actor=staff.export_documents
    )
    assert missing.documents == required
    with pytest.raises(Rejected, match=r"VN-2025-0193 lapsed on"):
        do(
            kernel,
            staff.export_documents,
            airfreight.HoldShippingDocument(
                waybill_id=waybill.id,
                requirement_id=every(kernel, airfreight.Requirement, country="VN")[0].id,
                reference="VN-2025-0193",
                expires=TODAY - timedelta(days=40),
            ),
        )
    for requirement in every(kernel, airfreight.Requirement):
        if requirement.document not in required:
            continue
        do(
            kernel,
            staff.export_documents,
            airfreight.HoldShippingDocument(
                waybill_id=waybill.id,
                requirement_id=requirement.id,
                reference="VN-2026-0193",
                issued=TODAY,
                expires=TODAY + timedelta(days=90),
            ),
        )
    do(kernel, staff.logistics, airfreight.CompleteWaybill(waybill_id=waybill.id))

    with pytest.raises(Rejected, match=r"waybill WB0001 is complete: an export is zero-rated only"):
        do(kernel, staff.logistics, invoicing.RaiseInvoice(waybill_id=waybill.id))
    do(kernel, staff.logistics, airfreight.DispatchWaybill(waybill_id=waybill.id))
    invoice = do(kernel, staff.logistics, invoicing.RaiseInvoice(waybill_id=waybill.id))

    # The lines, and the order's carriage and insurance on its first invoice.
    assert (invoice.net, invoice.freight, invoice.insurance) == (
        Money.of("133052.50", "USD"),
        Money.of("4850.00", "USD"),
        Money.of("420.00", "USD"),
    )
    assert invoice.total == Money.of("138322.50", "USD")
    assert invoice.deposit_applied == Money.of("41496.75", "USD")
    assert invoice.amount_due == Money.of("96825.75", "USD")
    assert invoice.due == invoice.issued + timedelta(days=30)
    assert invoice.exchange_rate == USD
    assert invoice.total_base == invoice.total.convert("GBP", rate=USD)
    assert (invoice.awb_number, invoice.programme) == ("917-48213373", "RAHP-24-117")
    assert invoice.customer_po == "NVDL-PO-2026-0388"
    order = latest(kernel, played.order)
    assert order.state == "closed"
    for line in every(kernel, sales.SalesOrderLine, sales_order_id=order.id):
        assert (line.state, line.shipped, line.invoiced, line.outstanding) == (
            "closed",
            line.quantity,
            line.quantity,
            0,
        )
    with pytest.raises(Rejected, match=r"order SO0001 is closed; it can no longer change"):
        amend(kernel, staff.tenders, sales.AmendSalesOrder, order, notes="Too late")

    # From the invoice back to the quotation, a line at a time.
    for line in every(kernel, invoicing.InvoiceLine, invoice_id=invoice.id):
        packed = every(kernel, airfreight.PackingLine, id=line.packing_line_id)[0]
        sold = every(kernel, sales.SalesOrderLine, id=packed.sales_order_line_id)[0]
        quoted = every(kernel, sales.QuotationLine, id=sold.quotation_line_id)[0]
        assert quoted.quotation_id == played.tender.id
        assert line.amount == quoted.amount == packed.value
        assert line.batch_id == packed.batch_id

    # The customer pays when the dollar has risen to 80p, which is a gain on exchange.
    paid_on = TODAY + timedelta(days=28)
    do(kernel, staff.md, fx.RecordRate(currency="USD", effective=paid_on, rate=Decimal("0.80")))
    with pytest.raises(Unauthorised):
        do(kernel, staff.logistics, invoicing.RecordPayment(invoice_id=invoice.id, paid=paid_on))
    paid = do(
        kernel,
        staff.md,
        invoicing.RecordPayment(invoice_id=invoice.id, paid=paid_on, reference="TT-0417-2281"),
    )
    assert (paid.state, paid.paid, paid.payment_reference) == ("paid", paid_on, "TT-0417-2281")
    received = invoice.amount_due.convert("GBP", rate=Decimal("0.80"))
    owed = invoice.total.convert("GBP", rate=USD) - invoice.deposit_applied.convert("GBP", rate=USD)
    assert _balance(kernel, "7800") == -(received - owed).amount
    assert _balance(kernel, "1100") == 0
    assert _balance(kernel, "2300") == 0

    # Everything bought has gone, and the books balance with nothing left in stock but
    # the cool boxes held from before.
    assert _balance(kernel, "1300") == Decimal("1080.00") + Decimal("1368.00")
    assert sum(_balance(kernel, code) for code, *_ in month.ACCOUNTS) == 0


def test_an_order_is_amended_until_it_is_invoiced(kernel: Kernel, staff: Staff) -> None:
    played = month.play(kernel, staff, TODAY)
    order = amend(
        kernel,
        staff.tenders,
        sales.AmendSalesOrder,
        played.order,
        freight=Money.of("5100.00", "USD"),
        customer_po="NVDL-PO-2026-0388/A",
    )
    assert order.total == played.order.total + Money.of("250.00", "USD")

    line = _line(kernel, order, "EQP-BSC2")
    later = TODAY + timedelta(days=70)
    promised = amend(
        kernel, staff.tenders, sales.AmendSalesOrderLine, line, promised_delivery=later
    )
    assert (promised.promised_delivery, promised.quantity) == (later, line.quantity)


def _quoted(kernel: Kernel, quotation: sales.Quotation, item: str) -> sales.QuotationLine:
    (found,) = [
        line
        for line in every(kernel, sales.QuotationLine, quotation_id=quotation.id, state="current")
        if _item_code(kernel, line.item_id) == item
    ]
    return found


def _line(kernel: Kernel, order: sales.SalesOrder, item: str) -> sales.SalesOrderLine:
    (found,) = [
        line
        for line in every(kernel, sales.SalesOrderLine, sales_order_id=order.id)
        if _item_code(kernel, line.item_id) == item
    ]
    return found


def _batch(kernel: Kernel, item: str, batch: str) -> stock.Batch:
    (held,) = [
        b for b in every(kernel, stock.Batch, batch=batch) if _item_code(kernel, b.item_id) == item
    ]
    return held


def _item(kernel: Kernel, code: str) -> items.Item:
    return every(kernel, items.Item, code=code)[0]


def _item_code(kernel: Kernel, item_id: object) -> str:
    return every(kernel, items.Item, id=item_id)[0].code


def _party(kernel: Kernel, code: str) -> parties.Party:
    return every(kernel, parties.Party, code=code)[0]


def _balance(kernel: Kernel, code: str) -> Decimal:
    account = every(kernel, ledger.Account, code=code)[0]
    return kernel.ask(ledger.AccountBalance(account_id=account.id), actor=ROOT).base.amount
