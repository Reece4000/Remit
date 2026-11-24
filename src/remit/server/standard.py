"""The standard modules, as one application: parties, items and the ledger.

    REMIT_APPLICATION=remit.server.standard:application
    REMIT_BASE_CURRENCY=GBP

A business with modules of its own writes an application like this one that names them.
"""

import os

from remit import items, ledger, parties

from .settings import Application


def application() -> Application:
    currency = os.environ.get("REMIT_BASE_CURRENCY")
    if not currency:
        raise ValueError("set REMIT_BASE_CURRENCY, such as GBP: the ledger keeps its books in it")
    return Application(
        modules=(parties.module, items.module, ledger.module),
        settings=(ledger.LedgerSettings(base_currency=currency),),
    )
