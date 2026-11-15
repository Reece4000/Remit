from datetime import date
from decimal import Decimal

import pytest
from conftest import ROOT, every, rows

from remit import ledger
from remit.kernel import (
    Actor,
    ActorKind,
    Kernel,
    MemoryStore,
    Money,
    PostgresStore,
    RegistrationError,
    Rejected,
    Store,
)
from remit.ledger import AccountType, credit, debit

BOOKKEEPER = Actor(id="bookkeeper", kind=ActorKind.HUMAN, permissions=frozenset({"ledger.*"}))
JAN, FEB = date(2026, 1, 15), date(2026, 2, 15)


def gbp(amount: str) -> Money:
    return Money.of(amount, "GBP")


def usd(amount: str) -> Money:
    return Money.of(amount, "USD")


class Books:
    """A ledger with a small chart of accounts, and shorthand for driving it."""

    def __init__(self, store: Store) -> None:
        self.kernel = Kernel(
            [ledger.module], settings=[ledger.LedgerSettings(base_currency="GBP")], store=store
        )
        self.bank = self.open("1200", "Bank", AccountType.ASSET, "GBP")
        self.receivables = self.open("1100", "Receivables (USD)", AccountType.ASSET, "USD")
        self.sales = self.open("4000", "Sales", AccountType.INCOME)
        self.fx = self.open("7900", "Exchange differences", AccountType.EXPENSE)

    def open(
        self, code: str, name: str, type: AccountType, currency: str | None = None
    ) -> ledger.Account:
        command = ledger.OpenAccount(code=code, name=name, type=type, currency=currency)
        return self.kernel.execute(command, actor=BOOKKEEPER).result

    def post(self, on: date, *postings: ledger.Posting) -> ledger.JournalEntry:
        command = ledger.PostJournalEntry(posting_date=on, description="Test", postings=postings)
        return self.kernel.execute(command, actor=BOOKKEEPER).result

    def balance(self, account: ledger.Account) -> ledger.Balance:
        return self.kernel.ask(ledger.AccountBalance(account_id=account.id), actor=ROOT)


@pytest.fixture
def books(postgres: PostgresStore) -> Books:
    return Books(postgres)


def test_a_foreign_invoice_settles_with_an_exchange_difference(books: Books) -> None:
    # Invoice USD 1,000 when a dollar is worth 80p; it is paid when a dollar is worth 78p.
    books.post(
        JAN,
        debit(books.receivables, usd("1000"), base=gbp("800")),
        credit(books.sales, usd("1000"), base=gbp("800")),
    )
    books.post(
        FEB,
        debit(books.bank, gbp("780")),
        debit(books.fx, gbp("20")),
        credit(books.receivables, usd("1000"), base=gbp("800")),
    )

    assert books.balance(books.receivables).is_zero
    assert books.balance(books.bank).base == gbp("780")
    assert books.balance(books.sales).base == gbp("-800")
    assert books.balance(books.fx).base == gbp("20")
    assert Money.total(books.kernel.ask(ledger.TrialBalance(), actor=ROOT).values(), "GBP").is_zero
    assert books.kernel.settings(ledger.LedgerSettings).base_currency == "GBP"


def test_a_foreign_balance_can_be_revalued_without_moving_the_foreign_amount(
    books: Books,
) -> None:
    books.post(
        JAN,
        debit(books.receivables, usd("1000"), base=gbp("800")),
        credit(books.sales, usd("1000"), base=gbp("800")),
    )
    books.post(FEB, debit(books.receivables, usd("0"), base=gbp("15")), credit(books.fx, gbp("15")))

    held = books.balance(books.receivables)
    assert (held.base, held.foreign["USD"]) == (gbp("815"), usd("1000"))


def test_an_entry_with_several_legs_lands_on_every_account(books: Books) -> None:
    vat = books.open("2200", "VAT", AccountType.LIABILITY)
    books.post(
        JAN,
        debit(books.bank, gbp("120")),
        credit(books.sales, gbp("100")),
        credit(vat, gbp("20")),
    )

    assert books.balance(books.bank).base == gbp("120")
    assert books.balance(books.sales).base == gbp("-100")
    assert books.balance(vat).base == gbp("-20")
    assert books.kernel.ask(ledger.Reconcile(), actor=ROOT) == []


def test_balances_can_be_read_as_of_a_date(books: Books) -> None:
    books.post(JAN, debit(books.bank, gbp("100")), credit(books.sales, gbp("100")))
    books.post(FEB, debit(books.bank, gbp("50")), credit(books.sales, gbp("50")))

    assert books.kernel.ask(
        ledger.AccountBalance(account_id=books.bank.id, as_of=JAN), actor=ROOT
    ).base == gbp("100")
    assert books.balance(books.bank).base == gbp("150")


@pytest.mark.parametrize(
    ("postings", "reason"),
    [
        (lambda b: (debit(b.bank, gbp("100")),), "at least two postings"),
        (
            lambda b: (debit(b.bank, gbp("100")), credit(b.sales, gbp("90"))),
            "debits exceed credits by GBP 10.00",
        ),
        (
            lambda b: (debit(b.bank, usd("100"), base=gbp("80")), credit(b.sales, gbp("80"))),
            r"posting 1 \(account 1200\): the account only accepts GBP",
        ),
        (
            lambda b: (debit(b.sales, usd("100"), base=usd("100")), credit(b.bank, gbp("100"))),
            "the base amount must be in GBP",
        ),
        (
            lambda b: (debit(b.sales, gbp("100"), base=gbp("90")), credit(b.bank, gbp("90"))),
            "must equal its base amount",
        ),
        (
            lambda b: (debit(b.sales, usd("100"), base=gbp("-80")), debit(b.bank, gbp("80"))),
            "opposite signs",
        ),
        (
            lambda b: (debit(b.sales, usd("100"), base=gbp("0")), credit(b.bank, gbp("0"))),
            "the base amount is zero",
        ),
    ],
)
def test_invalid_entries_are_refused_with_a_reason(books: Books, postings, reason: str) -> None:
    with pytest.raises(Rejected, match=reason):
        books.post(JAN, *postings(books))
    assert every(books.kernel, ledger.JournalEntry) == []


def test_a_reversal_cancels_an_entry_exactly_once(books: Books) -> None:
    entry = books.post(JAN, debit(books.bank, gbp("100")), credit(books.sales, gbp("100")))
    reverse = ledger.ReverseJournalEntry(entry_id=entry.id, posting_date=FEB)

    reversal = books.kernel.execute(reverse, actor=BOOKKEEPER).result

    assert reversal.reverses == entry.id
    assert books.balance(books.bank).is_zero
    assert books.kernel.get(ledger.JournalEntry, entry.id, actor=ROOT) == entry
    with pytest.raises(Rejected, match="already been reversed"):
        books.kernel.execute(reverse, actor=BOOKKEEPER)
    with pytest.raises(Rejected, match="itself a reversal"):
        books.kernel.execute(ledger.ReverseJournalEntry(entry_id=reversal.id), actor=BOOKKEEPER)


def test_a_closed_period_accepts_no_postings(books: Books) -> None:
    books.kernel.execute(ledger.ClosePeriod(through=date(2026, 1, 31)), actor=BOOKKEEPER)

    with pytest.raises(Rejected, match="post on or after 2026-02-01"):
        books.post(JAN, debit(books.bank, gbp("100")), credit(books.sales, gbp("100")))
    with pytest.raises(Rejected, match="already closed through 2026-01-31"):
        books.kernel.execute(ledger.ClosePeriod(through=JAN), actor=BOOKKEEPER)
    books.post(FEB, debit(books.bank, gbp("100")), credit(books.sales, gbp("100")))


def test_an_account_is_archived_only_when_empty_and_then_accepts_nothing(books: Books) -> None:
    entry = books.post(JAN, debit(books.bank, gbp("100")), credit(books.sales, gbp("100")))
    archive = ledger.ArchiveAccount(account_id=books.bank.id)

    with pytest.raises(Rejected, match=r"still holds a balance of GBP 100\.00"):
        books.kernel.execute(archive, actor=BOOKKEEPER)

    books.kernel.execute(ledger.ReverseJournalEntry(entry_id=entry.id), actor=BOOKKEEPER)
    assert books.kernel.execute(archive, actor=BOOKKEEPER).result.state == "archived"
    with pytest.raises(Rejected, match="the account is archived"):
        books.post(FEB, debit(books.bank, gbp("1")), credit(books.sales, gbp("1")))


def test_a_dry_run_shows_the_entry_and_posts_nothing(books: Books) -> None:
    postings = (debit(books.bank, gbp("100")), credit(books.sales, gbp("100")))
    command = ledger.PostJournalEntry(posting_date=JAN, description="Test", postings=postings)

    outcome = books.kernel.execute(command, actor=BOOKKEEPER, dry_run=True)

    assert outcome.result.postings == postings
    assert every(books.kernel, ledger.JournalEntry) == []
    assert books.balance(books.bank).is_zero
    assert rows("SELECT count(*) FROM pgledger_transfers") == [(0,)]


def test_the_books_hold_a_clearing_balance_for_each_foreign_currency(books: Books) -> None:
    books.post(
        JAN,
        debit(books.receivables, usd("1000"), base=gbp("800")),
        credit(books.sales, gbp("800")),
    )

    held = dict(rows("SELECT name, balance FROM pgledger_accounts_view"))
    assert held == {
        "1100:GBP": Decimal("800.00"),
        "4000:GBP": Decimal("-800.00"),
        "1100:USD": Decimal("1000.00"),
        "clearing:USD": Decimal("-1000.00"),
    }
    assert books.kernel.ask(ledger.Reconcile(), actor=ROOT) == []


def test_the_ledger_will_not_start_on_a_store_without_sql() -> None:
    with pytest.raises(RegistrationError, match="needs a PostgreSQL store"):
        Books(MemoryStore())


def test_account_codes_are_unique(books: Books) -> None:
    with pytest.raises(Rejected, match="'1200' is already in use"):
        books.open("1200", "Another bank", AccountType.ASSET)


def test_the_ledger_will_not_start_without_a_base_currency() -> None:
    with pytest.raises(RegistrationError, match="needs LedgerSettings"):
        Kernel([ledger.module])
