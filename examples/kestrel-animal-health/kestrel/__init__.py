"""Kestrel Animal Health's own modules, and the application that runs them.

    REMIT_APPLICATION=kestrel:application

Remit has no standard sales, purchasing, stock or invoicing modules yet. Following the
promotion rule (docs/modules-and-templates.md), Kestrel writes its own here, as any
business would, and they become standard modules once a second, unrelated example needs
them. They build only on the kernel's public API and the standard modules' public APIs.
"""

from remit import items, ledger, parties
from remit.server import Application

from . import airfreight, fx, invoicing, pricelists, purchasing, sales, stock

MODULES = (
    parties.module,
    items.module,
    ledger.module,
    fx.module,
    pricelists.module,
    stock.module,
    sales.module,
    purchasing.module,
    airfreight.module,
    invoicing.module,
)


def application() -> Application:
    """Kestrel's modules with Remit's standard ones, keeping the books in GBP."""
    return Application(
        modules=MODULES,
        settings=(
            ledger.LedgerSettings(base_currency="GBP"),
            stock.StockSettings(),
            sales.SalesSettings(),
            purchasing.PurchasingSettings(),
            invoicing.InvoicingSettings(),
        ),
    )


__all__ = [
    "MODULES",
    "airfreight",
    "application",
    "fx",
    "invoicing",
    "pricelists",
    "purchasing",
    "sales",
    "stock",
]
