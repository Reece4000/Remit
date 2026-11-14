"""The bridge to pgledger, which keeps the books.

pgledger moves an amount of one currency from one account to another, so it cannot hold
an unbalanced ledger. A journal entry is richer: several postings, each with an amount
in its own currency and a value in the base currency. This module translates.

Every ledger account has a base book: a pgledger account in the base currency holding
the base value of everything posted to it. These are the statutory books, and pgledger
keeps them balanced. An account posted to in a foreign currency also has a book in that
currency, holding the foreign amount. A foreign amount need not have a counterpart in
the same entry (a dollar receivable can be settled by a sterling payment), so each
currency has a clearing book that takes the other side.
"""

import json
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from remit.kernel import Context, RemitError

from .model import Account, JournalEntry, LedgerSettings, Posting

CLEARING = UUID(int=0)
"""Stands in for the account that owns each currency's clearing book."""

type Book = tuple[UUID, str]
"""A ledger account and a currency: one pgledger account."""


@dataclass(frozen=True)
class Movement:
    """An amount moving from the book it is credited to, to the book it is debited to."""

    source: Book
    target: Book
    amount: Decimal


def movements(postings: Sequence[Posting], base: str) -> list[Movement]:
    """The two-sided movements that have exactly the effect of a set of postings."""
    net: defaultdict[Book, Decimal] = defaultdict(Decimal)
    for posting in postings:
        currency = posting.amount.currency
        net[posting.account, base] += posting.base.amount
        if currency != base:
            net[posting.account, currency] += posting.amount.amount
            net[CLEARING, currency] -= posting.amount.amount

    result: list[Movement] = []
    for currency in dict.fromkeys(currency for _, currency in net):
        books = [(book, amount) for book, amount in net.items() if book[1] == currency]
        debits = deque((book, amount) for book, amount in books if amount > 0)
        credits = deque((book, -amount) for book, amount in books if amount < 0)
        while debits and credits:
            (debited, owed), (credited, due) = debits.popleft(), credits.popleft()
            moved = min(owed, due)
            result.append(Movement(source=credited, target=debited, amount=moved))
            if owed > moved:
                debits.appendleft((debited, owed - moved))
            if due > moved:
                credits.appendleft((credited, due - moved))
        if debits or credits:
            raise RemitError(f"the {currency} postings do not balance")
    return result


def record(context: Context, entry: JournalEntry) -> None:
    """Write a journal entry's movements to pgledger, in this unit of work."""
    moves = movements(entry.postings, context.settings(LedgerSettings).base_currency)
    if not moves:
        return
    books = dict.fromkeys(book for move in moves for book in (move.source, move.target))
    accounts = {book: _pgledger_account(context, book) for book in books}
    requests = ", ".join(["(%s, %s, %s)::transfer_request"] * len(moves))
    context.sql(
        f"SELECT id FROM pgledger_create_transfers(ARRAY[{requests}], "
        "event_at => %s, metadata => %s::jsonb)",
        [
            *(value for m in moves for value in (accounts[m.source], accounts[m.target], m.amount)),
            start_of(entry.posting_date),
            json.dumps({"journal_entry": str(entry.id)}),
        ],
    )


def start_of(day: date) -> datetime:
    """The instant a posting date begins. Entries are timestamped with it in pgledger."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _pgledger_account(context: Context, book: Book) -> str:
    account, currency = book
    found = context.sql(
        "SELECT pgledger_account FROM ledger_books WHERE account = %s AND currency = %s",
        (account, currency),
    )
    if found:
        return found[0][0]
    owner = "clearing" if account == CLEARING else context.get(Account, account).code
    [(created,)] = context.sql(
        "SELECT id FROM pgledger_create_account(%s, %s, metadata => %s::jsonb)",
        (f"{owner}:{currency}", currency, json.dumps({"account": str(account)})),
    )
    context.sql(
        "INSERT INTO ledger_books (account, currency, pgledger_account) VALUES (%s, %s, %s)",
        (account, currency, created),
    )
    return created
