"""The ledger's commands. These are the only ways the books change.

Each command checks the accounting rules, records a document of what was done, and has
pgledger move the money, all in one unit of work.
"""

from datetime import date, timedelta
from uuid import UUID

from remit.kernel import Code, Command, Context, Currency, Money, Name, Rejected

from .books import record
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
    module,
)
from .queries import balance, closed_through


class OpenAccount(Command[Account]):
    code: Code
    name: Name
    type: AccountType
    currency: Currency | None = None


class ArchiveAccount(Command[Account]):
    account_id: AccountId


class PostJournalEntry(Command[JournalEntry]):
    posting_date: date
    description: str
    postings: tuple[Posting, ...]


class ReverseJournalEntry(Command[JournalEntry]):
    entry_id: JournalEntryId
    posting_date: date | None = None
    """Defaults to the original entry's date."""


class ClosePeriod(Command[PeriodClose]):
    through: date


@module.command
def open_account(command: OpenAccount, context: Context) -> Account:
    """Add an account to the chart of accounts. Codes are unique and never reused."""
    account = context.create(
        Account(code=command.code, name=command.name, type=command.type, currency=command.currency)
    )
    context.emit(AccountOpened(account_id=account.id, code=account.code))
    return account


@module.command
def archive_account(command: ArchiveAccount, context: Context) -> Account:
    """Stop an account accepting postings. Its balance must be zero."""
    account = context.get(Account, command.account_id)
    held = balance(context, account.id)
    if not held.is_zero:
        raise Rejected(f"account {account.code} still holds a balance of {held.base}")
    archived = context.transition(account, "archive")
    context.emit(AccountArchived(account_id=account.id))
    return archived


@module.command
def post_journal_entry(command: PostJournalEntry, context: Context) -> JournalEntry:
    """Post a balanced journal entry. It can never be edited, only reversed."""
    return _post(context, command.posting_date, command.description, command.postings)


@module.command
def reverse_journal_entry(command: ReverseJournalEntry, context: Context) -> JournalEntry:
    """Cancel a journal entry by posting its mirror image."""
    original = context.get(JournalEntry, command.entry_id)
    if original.reverses:
        raise Rejected("this entry is itself a reversal; post a new entry instead")
    if context.find(JournalEntry, reverses=original.id):
        raise Rejected("this entry has already been reversed")
    posting_date = command.posting_date or original.posting_date
    if posting_date < original.posting_date:
        raise Rejected(f"a reversal cannot be dated before its entry ({original.posting_date})")
    mirrored = tuple(
        Posting(account=posting.account, amount=-posting.amount, base=-posting.base)
        for posting in original.postings
    )
    return _post(
        context, posting_date, f"Reversal: {original.description}", mirrored, reverses=original.id
    )


@module.command
def close_period(command: ClosePeriod, context: Context) -> PeriodClose:
    """Close the books through a date. Nothing can then be posted on or before it."""
    closed = closed_through(context)
    if closed and command.through <= closed:
        raise Rejected(f"the books are already closed through {closed}")
    close = context.create(PeriodClose(through=command.through))
    context.emit(PeriodClosed(through=command.through))
    return close


def _post(
    context: Context,
    posting_date: date,
    description: str,
    postings: tuple[Posting, ...],
    *,
    reverses: UUID | None = None,
) -> JournalEntry:
    closed = closed_through(context)
    if closed and posting_date <= closed:
        raise Rejected(
            f"the books are closed through {closed}; post on or after {closed + timedelta(days=1)}"
        )
    _require_valid(context, postings)
    entry = context.create(
        JournalEntry(
            posting_date=posting_date, description=description, postings=postings, reverses=reverses
        )
    )
    record(context, entry)
    context.emit(
        JournalEntryPosted(entry_id=entry.id, posting_date=posting_date, reverses=reverses)
    )
    return entry


def _require_valid(context: Context, postings: tuple[Posting, ...]) -> None:
    base = context.settings(LedgerSettings).base_currency
    if len(postings) < 2:
        raise Rejected("a journal entry needs at least two postings")

    for number, posting in enumerate(postings, start=1):
        account = context.get(Account, posting.account)
        where = f"posting {number} (account {account.code})"
        if account.is_final:
            raise Rejected(f"{where}: the account is archived")
        if account.currency and posting.amount.currency != account.currency:
            raise Rejected(f"{where}: the account only accepts {account.currency}")
        if posting.base.currency != base:
            raise Rejected(f"{where}: the base amount must be in {base}")
        if posting.base.is_zero:
            raise Rejected(f"{where}: the base amount is zero")
        if posting.amount.currency == base and posting.amount != posting.base:
            raise Rejected(f"{where}: a {base} amount must equal its base amount")
        if posting.amount.sign * posting.base.sign < 0:
            raise Rejected(f"{where}: the amount and base amount have opposite signs")

    difference = Money.total((posting.base for posting in postings), base)
    if not difference.is_zero:
        heavier, lighter = ("debits", "credits") if difference.sign > 0 else ("credits", "debits")
        raise Rejected(
            f"the entry does not balance: {heavier} exceed {lighter} by {abs(difference)}"
        )
