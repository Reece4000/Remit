"""Exchange rates: what one unit of each currency is worth in the base currency, by date.

Kestrel quotes and invoices in USD or EUR and buys in GBP, EUR, USD or DKK, so margin is
known only once both sides are converted, and every ledger posting needs its value in
the base currency. A rate is recorded for a date and never changed: a correction is a
rate for a later date.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import Field

from remit import ledger
from remit.kernel import (
    Command,
    Context,
    Currency,
    Document,
    Event,
    Lifecycle,
    Module,
    Money,
    Reader,
    References,
    Rejected,
)

module = Module("fx", requires=(ledger.module,))

RateId = Annotated[UUID, References("fx.rate")]
Factor = Annotated[Decimal, Field(gt=0, max_digits=20, decimal_places=10)]


@module.document
class Rate(Document):
    """What one unit of a currency is worth in the base currency, from a date until the
    next rate for it."""

    lifecycle = Lifecycle(initial="recorded")
    unique = (("currency", "effective"),)
    searchable = ("currency",)

    currency: Currency
    """The currency whose value is recorded."""
    effective: date
    """The first day the rate applies, until a later one is recorded."""
    rate: Factor
    """Units of the base currency that one unit of this currency buys: 0.79 for USD when
    the books are kept in GBP."""


@module.event
class RateRecorded(Event):
    """A rate was recorded."""

    rate_id: RateId
    currency: str
    effective: date


class RecordRate(Command[Rate]):
    currency: Currency
    effective: date
    rate: Factor


@module.command
def record_rate(command: RecordRate, context: Context) -> Rate:
    """Record what a currency is worth in the base currency from a date. One per day."""
    base = context.settings(ledger.LedgerSettings).base_currency
    if command.currency == base:
        raise Rejected(f"{base} is the base currency; its rate is always 1")
    rate = context.create(Rate.model_validate(command.model_dump()))
    context.emit(RateRecorded(rate_id=rate.id, currency=rate.currency, effective=rate.effective))
    return rate


def in_base(reader: Reader, amount: Money, on: date) -> Money:
    """An amount's value in the base currency, at the latest rate on or before a date."""
    base = reader.settings(ledger.LedgerSettings).base_currency
    if amount.currency == base:
        return amount
    return amount.convert(base, rate=rate_on(reader, amount.currency, on))


def rate_on(reader: Reader, currency: str, on: date) -> Decimal:
    """What one unit of a currency is worth in the base currency, at the latest rate on or
    before a date: 1 for the base currency itself."""
    if currency == reader.settings(ledger.LedgerSettings).base_currency:
        return Decimal(1)
    found = reader.page(
        Rate, where={"currency": currency, "effective": {"lte": on}}, order="-effective", limit=1
    ).items
    if not found:
        raise Rejected(f"there is no {currency} rate on or before {on}; record one in fx")
    return found[0].rate
