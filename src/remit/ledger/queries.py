"""Reading the ledger. Balances come from pgledger, which keeps them."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from remit.kernel import Money, Reader

from .books import CLEARING, Book, start_of
from .model import Account, LedgerSettings, PeriodClose

# Both take the account to read, twice, or null for every account. Reading only the books asked
# for matters beyond speed: a serialisable unit conflicts with whatever writes what it
# read, so a balance check on one account must not read every other account's books.

_CURRENT = """
    SELECT book.account, book.currency, held.balance
    FROM ledger_books AS book
    JOIN pgledger_accounts_view AS held ON held.id = book.pgledger_account
    WHERE %s::uuid IS NULL OR book.account = %s
"""

_AS_OF = """
    SELECT book.account, book.currency, coalesce(sum(entry.amount), 0)
    FROM ledger_books AS book
    LEFT JOIN pgledger_entries_view AS entry
        ON entry.account_id = book.pgledger_account AND entry.event_at < %s
    WHERE %s::uuid IS NULL OR book.account = %s
    GROUP BY book.account, book.currency
"""

_STORED_BALANCES_THAT_DISAGREE_WITH_THEIR_ENTRIES = """
    SELECT held.name, held.balance, coalesce(sum(entry.amount), 0)
    FROM pgledger_accounts_view AS held
    LEFT JOIN pgledger_entries_view AS entry ON entry.account_id = held.id
    GROUP BY held.id, held.name, held.balance
    HAVING held.balance <> coalesce(sum(entry.amount), 0)
    ORDER BY held.name
"""

_CURRENCIES_THAT_DO_NOT_SUM_TO_ZERO = """
    SELECT currency, sum(balance) FROM pgledger_accounts_view
    GROUP BY currency HAVING sum(balance) <> 0 ORDER BY currency
"""


@dataclass(frozen=True)
class Balance:
    base: Money
    """The balance in the ledger's base currency."""
    foreign: Mapping[str, Money]
    """The amount held in each foreign currency posted to the account."""

    @property
    def is_zero(self) -> bool:
        return self.base.is_zero and all(amount.is_zero for amount in self.foreign.values())


def balance(reader: Reader, account_id: UUID, *, as_of: date | None = None) -> Balance:
    """An account's balance, optionally as of the end of a date."""
    base = reader.settings(LedgerSettings).base_currency
    held = {
        currency: Money.of(amount, currency)
        for (_, currency), amount in _books(reader, as_of, account_id).items()
    }
    return Balance(held.pop(base, Money.zero(base)), held)


def trial_balance(reader: Reader, *, as_of: date | None = None) -> dict[UUID, Money]:
    """Every account's base-currency balance. The values always sum to zero."""
    base = reader.settings(LedgerSettings).base_currency
    totals = {account.id: Money.zero(base) for account in reader.find(Account)}
    for (account, currency), amount in _books(reader, as_of, None).items():
        if currency == base and account != CLEARING:
            totals[account] = Money.of(amount, base)
    return totals


def closed_through(reader: Reader) -> date | None:
    """The latest date the books are closed through, if any."""
    return max((close.through for close in reader.find(PeriodClose)), default=None)


def reconcile(reader: Reader) -> list[str]:
    """Check pgledger's books against themselves. Empty when they are sound.

    Reports any account whose stored balance is not the sum of its entries, and any
    currency whose accounts do not sum to zero.
    """
    problems = [
        f"{name} holds {stored} but its entries sum to {summed}"
        for name, stored, summed in reader.sql(_STORED_BALANCES_THAT_DISAGREE_WITH_THEIR_ENTRIES)
    ]
    problems += [
        f"{currency} accounts sum to {total}, not zero"
        for currency, total in reader.sql(_CURRENCIES_THAT_DO_NOT_SUM_TO_ZERO)
    ]
    return problems


def _books(reader: Reader, as_of: date | None, account: UUID | None) -> dict[Book, Decimal]:
    if as_of is None:
        rows = reader.sql(_CURRENT, (account, account))
    else:
        rows = reader.sql(_AS_OF, (start_of(as_of + timedelta(days=1)), account, account))
    return {(account, currency): amount for account, currency, amount in rows}
