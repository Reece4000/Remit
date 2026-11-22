"""Kestrel's made-up history, as its seed makes it: the same every time, and agreeing
with itself.

`kestrel.history` drives a year of trading through commands, on a clock it moves, from
one seeded random generator. These tests play a few months of it at a smaller volume:
twice, to see that the same volume, dates and seed make exactly the same records, and
then against the checks an auditor would make. Headers total their lines, what was
shipped and invoiced matches the waybills and invoices, stock is what the ledger says
it is, and customers owe what their open invoices say. Every item and party is described
as its kind makes plausible, and each waybill travels as what is packed on it is kept.
"""

import json
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from typing import Any

import kestrel
from conftest import ROOT, empty_database, every, rows
from kestrel import airfreight, history, invoicing, month, purchasing, sales, stock
from kestrel.catalogue import PRODUCTS, Family
from test_kestrel import ISSUER, PEOPLE

from remit import items, ledger, parties
from remit.kernel import Document, Kernel, Money, PostgresStore

START = date(2025, 3, 3)
END = date(2025, 9, 1)
SMALL = history.Volume(quotations=45, customers=20, products=120)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SEALED = re.compile(r'\{"\$sealed": "[^"]*"\}')
"""Personal data as the audit trail keeps it, encrypted afresh each time."""


def play(postgres: PostgresStore) -> Kernel:
    """Set Kestrel up on the first day and make its history up to the last."""
    application = kestrel.application()
    postgres.install(application.installed)
    clock = history.Clock(datetime.combine(START, time(7), tzinfo=UTC))
    kernel = Kernel(
        application.installed, settings=application.settings, store=postgres, clock=clock
    )
    staff = month.enrol(kernel, ISSUER, PEOPLE)
    month.set_up(kernel, staff, START)
    history.play(kernel, staff, clock, START, END, SMALL)
    return kernel


def fingerprint() -> list[str]:
    """Everything recorded, a line each: every command as audited, every event, and every
    document as it stands. Ids are drawn at random, so each is numbered in the order it
    first appears. Personal data is sealed in the audit trail under a random nonce, so it
    is compared where documents hold it in the clear, and an actor's permissions are a
    set, so they are compared in order."""
    numbers: dict[str, str] = {}

    def numbered(record: Iterable[object]) -> str:
        line = SEALED.sub('"sealed"', json.dumps(list(record), default=str, sort_keys=True))
        return UUID.sub(lambda found: numbers.setdefault(found[0], f"#{len(numbers)}"), line)

    return [
        numbered(record)
        for query in (
            "SELECT at, actor->>'id', actor->>'kind', ARRAY(SELECT jsonb_array_elements_text("
            "actor->'permissions') ORDER BY 1), action, input, documents, events "
            "FROM audit ORDER BY id",
            "SELECT name, body, at, actor FROM events ORDER BY position",
            "SELECT kind, version, final, body FROM documents ORDER BY kind, id",
        )
        for record in rows(query)
    ]


def test_the_same_seed_makes_the_same_history(postgres: PostgresStore) -> None:
    play(postgres)
    first = fingerprint()
    empty_database()
    play(postgres)

    assert fingerprint() == first


def test_the_history_reaches_every_state_and_agrees_with_itself(postgres: PostgresStore) -> None:
    kernel = play(postgres)

    def states(kind: type[Document]) -> set[str]:
        return {document.state for document in every(kernel, kind)}

    assert {"won", "lost", "sent", "withdrawn"} <= states(sales.Quotation)
    assert {"open", "closed"} <= states(sales.SalesOrder)
    assert {"confirmed", "received"} <= states(purchasing.PurchaseOrder)
    assert "dispatched" in states(airfreight.Waybill)
    assert {"issued", "paid"} <= states(invoicing.Invoice)
    assert {"in_stock", "used_up", "written_off"} <= states(stock.Batch)

    for quotation in every(kernel, sales.Quotation):
        lines = every(kernel, sales.QuotationLine, quotation_id=quotation.id, state="current")
        assert quotation.net == Money.total((line.amount for line in lines), quotation.currency)
        assert quotation.total == quotation.net + quotation.freight + quotation.insurance
        assert quotation.cost == Money.total((line.cost for line in lines), "GBP")

    packed = Counter[object]()
    for waybill in every(kernel, airfreight.Waybill, state="dispatched"):
        for line in every(kernel, airfreight.PackingLine, waybill_id=waybill.id):
            packed[line.sales_order_line_id] += line.quantity
    invoiced = Counter[object]()
    for line in every(kernel, invoicing.InvoiceLine):
        invoiced[line.sales_order_line_id] += line.quantity
    for order in every(kernel, sales.SalesOrder):
        lines = every(kernel, sales.SalesOrderLine, sales_order_id=order.id)
        assert order.net == Money.total((line.amount for line in lines), order.currency)
        assert order.total == order.net + order.freight + order.insurance
        for line in lines:
            assert line.outstanding == line.quantity - line.shipped
            assert (line.shipped, line.invoiced) == (packed[line.id], invoiced[line.id])
        if order.state == "closed":
            assert all(line.invoiced == line.quantity for line in lines)

    for order in every(kernel, purchasing.PurchaseOrder):
        lines = every(kernel, purchasing.PurchaseOrderLine, purchase_order_id=order.id)
        bought = [line for line in lines if line.state != "removed"]
        assert order.net == Money.total((line.amount for line in bought), order.currency)
        assert order.total == order.net + order.freight
        assert all(line.outstanding == line.quantity - line.received for line in lines)

    for invoice in every(kernel, invoicing.Invoice):
        lines = every(kernel, invoicing.InvoiceLine, invoice_id=invoice.id)
        assert invoice.net == Money.total((line.amount for line in lines), invoice.currency)
        assert invoice.total == invoice.net + invoice.freight + invoice.insurance
        assert invoice.amount_due == invoice.total - invoice.deposit_applied
        assert invoice.total_base == invoice.total.convert("GBP", rate=invoice.exchange_rate)

    batches = every(kernel, stock.Batch)
    for batch in batches:
        assert batch.available == batch.on_hand - batch.allocated
        assert batch.value == batch.unit_cost.times(batch.on_hand)
    assert balance(kernel, "1300") == Money.total((batch.value for batch in batches), "GBP")
    owed = [
        invoice.total_base - invoice.deposit_applied.convert("GBP", rate=invoice.exchange_rate)
        for invoice in every(kernel, invoicing.Invoice, state="issued")
    ]
    assert balance(kernel, "1100") == Money.total(owed, "GBP")


def test_the_history_describes_every_item_and_party_as_its_kind_makes_plausible(
    postgres: PostgresStore,
) -> None:
    kernel = play(postgres)
    family = {product.code: product.family for product in PRODUCTS}
    described = every(kernel, kernel.registry.extended(items.Item))
    seeded = [item for item in described if item.code in family]
    assert len(seeded) == SMALL.products
    for item in seeded:
        kind, kept = family[item.code], _field(item, "storage")
        low, high = _field(item, "temperature_min"), _field(item, "temperature_max")
        life, least = _field(item, "shelf_life_days"), _field(item, "minimum_shelf_life_days")
        assert _field(item, "supplier_id") is not None
        assert _field(item, "list_price").currency == "GBP"
        assert _field(item, "revenue_account") in {"4010", "4020", "4030", "4040"}
        assert least is None or least <= life
        assert _field(item, "expiry_tracked") == (life is not None)
        if kind is Family.VACCINE:
            assert (kept, low, high) == (stock.Storage.CHILLED, 2, 8)
            flags = ("do_not_freeze", "health_certificate", "veterinary_only", "light_sensitive")
            assert all(_field(item, flag) for flag in flags)
            assert _field(item, "species") != ()
        if kept is stock.Storage.FROZEN:
            assert high <= -20
            assert (_field(item, "dry_ice"), _field(item, "do_not_freeze")) == (True, False)
        if kind in (Family.EQUIPMENT, Family.COLD_CHAIN):
            assert kept is stock.Storage.AMBIENT
            assert _field(item, "length_cm") is not None
        assert (_field(item, "un_number") is None) == (_field(item, "hazard_class") is None)

    for party in every(kernel, kernel.registry.extended(parties.Party)):
        limit = _field(party, "credit_limit")
        if party.code.startswith("C-"):
            assert _field(party, "customer_type") is not None
            assert _field(party, "destination_airport") is not None
        if limit is not None:
            assert limit.currency == _field(party, "customer_currency")
            assert _field(party, "customer_payment_terms").days > 0
        if party.code.startswith("S-"):
            assert _field(party, "approved_until") >= END

    # Frozen goods and goods that must not freeze never share a waybill.
    storage = {item.id: _field(item, "storage") for item in described}
    for waybill in every(kernel, airfreight.Waybill, state="dispatched"):
        packed = every(kernel, airfreight.PackingLine, waybill_id=waybill.id, state="packed")
        assert len({storage[line.item_id] for line in packed}) == 1


def _field(document: Document, name: str) -> Any:
    """A field an extension adds to a document, read as a client reads it."""
    return getattr(document, name)


def balance(kernel: Kernel, code: str) -> Money:
    account = every(kernel, ledger.Account, code=code)[0]
    return kernel.ask(ledger.AccountBalance(account_id=account.id), actor=ROOT).base
