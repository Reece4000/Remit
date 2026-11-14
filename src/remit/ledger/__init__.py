"""A double-entry general ledger, with its books kept by pgledger.

Install it with `Kernel([ledger.module], settings=[ledger.LedgerSettings(...)])` on a
PostgreSQL store. Other modules post to it by executing `PostJournalEntry` from their
own handlers, which puts the posting in the same unit of work as the document that
caused it.
"""

from .commands import (
    ArchiveAccount,
    ClosePeriod,
    OpenAccount,
    PostJournalEntry,
    ReverseJournalEntry,
)
from .model import (
    Account,
    AccountArchived,
    AccountOpened,
    AccountType,
    JournalEntry,
    JournalEntryPosted,
    LedgerSettings,
    PeriodClose,
    PeriodClosed,
    Posting,
    credit,
    debit,
    module,
)
from .queries import Balance, balance, closed_through, reconcile, trial_balance

__all__ = [
    "Account",
    "AccountArchived",
    "AccountOpened",
    "AccountType",
    "ArchiveAccount",
    "Balance",
    "ClosePeriod",
    "JournalEntry",
    "JournalEntryPosted",
    "LedgerSettings",
    "OpenAccount",
    "PeriodClose",
    "PeriodClosed",
    "PostJournalEntry",
    "Posting",
    "ReverseJournalEntry",
    "balance",
    "closed_through",
    "credit",
    "debit",
    "module",
    "reconcile",
    "trial_balance",
]
