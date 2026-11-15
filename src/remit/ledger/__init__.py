"""A double-entry general ledger, with its books kept by pgledger.

Install it with `Kernel([ledger.module], settings=[ledger.LedgerSettings(...)])` on a
PostgreSQL store. Other modules post to it by executing `PostJournalEntry` from their
own handlers, which puts the posting in the same unit of work as the document that
caused it, and read it by asking its queries: `AccountBalance`, `TrialBalance`,
`ClosedThrough` and `Reconcile`.
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
    AccountId,
    AccountOpened,
    AccountType,
    JournalEntry,
    JournalEntryId,
    JournalEntryPosted,
    LedgerSettings,
    PeriodClose,
    PeriodClosed,
    Posting,
    credit,
    debit,
    module,
)
from .queries import AccountBalance, Balance, ClosedThrough, Reconcile, TrialBalance

__all__ = [
    "Account",
    "AccountArchived",
    "AccountBalance",
    "AccountId",
    "AccountOpened",
    "AccountType",
    "ArchiveAccount",
    "Balance",
    "ClosePeriod",
    "ClosedThrough",
    "JournalEntry",
    "JournalEntryId",
    "JournalEntryPosted",
    "LedgerSettings",
    "OpenAccount",
    "PeriodClose",
    "PeriodClosed",
    "PostJournalEntry",
    "Posting",
    "Reconcile",
    "ReverseJournalEntry",
    "TrialBalance",
    "credit",
    "debit",
    "module",
]
