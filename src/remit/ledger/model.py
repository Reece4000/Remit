"""The ledger's documents, events and settings.

Amounts are signed: a debit is positive, a credit negative. Every posting carries its
amount in the transaction currency and in the ledger's base currency, and an entry is
balanced when its base amounts sum to zero.

A journal entry document records what was asked for. The books themselves are kept by
pgledger, which the module installs as a migration; see `books.py`.
"""

from datetime import date
from enum import StrEnum
from importlib.resources import files
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from remit.kernel import (
    Code,
    Currency,
    Document,
    Event,
    Lifecycle,
    Migration,
    Module,
    Money,
    Name,
    Transition,
)


class LedgerSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_currency: Currency
    """The currency the books are kept in."""


_PGLEDGER = files(__package__).joinpath("vendor", "pgledger")

module = Module(
    "ledger",
    settings=LedgerSettings,
    migrations=(
        Migration(
            "pgledger-0.9.0-ulid-to-uuid", _PGLEDGER.joinpath("ulid-to-uuid.sql").read_text()
        ),
        Migration(
            "pgledger-0.9.0-uuid-to-ulid", _PGLEDGER.joinpath("uuid-to-ulid.sql").read_text()
        ),
        Migration("pgledger-0.9.0", _PGLEDGER.joinpath("pgledger.sql").read_text()),
    ),
    sql=files(__package__).joinpath("rules.sql").read_text(),
)


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"


@module.document
class Account(Document):
    """A general ledger account. Archived accounts keep their history and their code."""

    lifecycle = Lifecycle(
        initial="active",
        transitions=(Transition("archive", source="active", target="archived"),),
    )
    unique = ("code",)

    code: Code
    name: Name
    type: AccountType
    currency: Currency | None = None
    """When set, the account only accepts postings in this currency."""


class Posting(BaseModel):
    model_config = ConfigDict(frozen=True)

    account: UUID
    amount: Money
    base: Money


@module.document
class JournalEntry(Document):
    """A balanced set of postings. Final from the moment it exists; corrected by reversal."""

    lifecycle = Lifecycle(initial="posted")

    posting_date: date
    description: str
    postings: tuple[Posting, ...]
    reverses: UUID | None = None


@module.document
class PeriodClose(Document):
    """Marks the books as closed up to and including a date."""

    lifecycle = Lifecycle(initial="closed")

    through: date


@module.event
class AccountOpened(Event):
    """An account was added to the chart of accounts."""

    account_id: UUID
    code: str


@module.event
class AccountArchived(Event):
    """An account was archived and accepts no further postings."""

    account_id: UUID


@module.event
class JournalEntryPosted(Event):
    """A journal entry was posted. `reverses` is set when it cancels an earlier entry."""

    entry_id: UUID
    posting_date: date
    reverses: UUID | None = None


@module.event
class PeriodClosed(Event):
    """The books were closed through a date; nothing can be posted on or before it."""

    through: date


def debit(account: Account | UUID, amount: Money, *, base: Money | None = None) -> Posting:
    """A debit posting. `base` may be omitted when `amount` is in the base currency."""
    account_id = account.id if isinstance(account, Account) else account
    return Posting(account=account_id, amount=amount, base=base or amount)


def credit(account: Account | UUID, amount: Money, *, base: Money | None = None) -> Posting:
    """A credit posting. Pass positive amounts; they are stored negated."""
    return debit(account, -amount, base=-(base or amount))
