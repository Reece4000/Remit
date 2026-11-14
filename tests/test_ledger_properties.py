"""The ledger's invariants, checked against arbitrary sequences of commands.

Hypothesis drives the ledger with valid and invalid commands in any order. After every
step, whatever was accepted or refused, the invariants at the bottom must hold. One of
them recomputes every balance from the journal entries the commands returned and
compares it with what pgledger holds, so the two are checked against each other.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from conftest import empty_database
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from remit import ledger
from remit.kernel import (
    Actor,
    ActorKind,
    Command,
    Kernel,
    Money,
    PostgresStore,
    Rejected,
    minor_units,
)
from remit.ledger import AccountType, Posting
from remit.ledger.books import CLEARING, movements

BASE = "GBP"
CURRENCIES = (BASE, "USD", "JPY")
BOOKKEEPER = Actor(id="bookkeeper", kind=ActorKind.HUMAN, permissions=frozenset({"ledger.*"}))

dates = st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 12, 31))
closing_dates = st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 6, 30))


def amounts(currency: str, *, min_value: int = -1_000_000) -> st.SearchStrategy[Money]:
    decimals = st.decimals(
        min_value=Decimal(min_value), max_value=Decimal(1_000_000), places=minor_units(currency)
    )
    return decimals.map(lambda amount: Money.of(amount, currency))


class LedgerMachine(RuleBasedStateMachine):
    store: PostgresStore

    def __init__(self) -> None:
        super().__init__()
        empty_database()
        self.kernel = Kernel(
            [ledger.module], settings=[ledger.LedgerSettings(base_currency=BASE)], store=self.store
        )
        self.accounts: list[ledger.Account] = []
        self.archived: set[UUID] = set()
        self.entries: list[ledger.JournalEntry] = []
        self.closed: tuple[date, dict[UUID, Money]] | None = None

    def footprint(self) -> tuple[object, ...]:
        kernel = self.kernel
        return (
            len(kernel.audit()),
            len(kernel.events()),
            kernel.sql("SELECT count(*) FROM pgledger_transfers"),
            ledger.trial_balance(kernel),
        )

    def attempt[R](self, command: Command[R]) -> R | None:
        """Run a command; if it is refused, check that it left no trace."""
        before = self.footprint()
        try:
            return self.kernel.execute(command, actor=BOOKKEEPER).result
        except Rejected:
            assert self.footprint() == before
            return None

    @property
    def active_accounts(self) -> list[ledger.Account]:
        return [account for account in self.accounts if account.id not in self.archived]

    def draw_valid_posting(self, data: st.DataObject) -> Posting:
        account = data.draw(st.sampled_from(self.active_accounts))
        currency = account.currency or data.draw(st.sampled_from(CURRENCIES))
        amount = data.draw(amounts(currency).filter(lambda m: not m.is_zero))
        if currency == BASE:
            return Posting(account=account.id, amount=amount, base=amount)
        magnitude = data.draw(amounts(BASE, min_value=0).filter(lambda m: not m.is_zero))
        return Posting(
            account=account.id, amount=amount, base=magnitude if amount.sign > 0 else -magnitude
        )

    @initialize()
    def open_unrestricted_accounts(self) -> None:
        self.open_account(AccountType.ASSET, None)
        self.open_account(AccountType.EQUITY, None)

    @rule(type=st.sampled_from(AccountType), currency=st.sampled_from((None, *CURRENCIES)))
    def open_account(self, type: AccountType, currency: str | None) -> None:
        code = f"{len(self.accounts) + 1:04d}"
        account = self.attempt(
            ledger.OpenAccount(code=code, name=code, type=type, currency=currency)
        )
        assert account is not None
        self.accounts.append(account)

    @rule(data=st.data(), on=dates)
    def post_balanced_entry(self, data: st.DataObject, on: date) -> None:
        count = data.draw(st.integers(min_value=1, max_value=4))
        postings = [self.draw_valid_posting(data) for _ in range(count)]
        residual = -Money.total((posting.base for posting in postings), BASE)
        assume(not residual.is_zero)
        balancing = Posting(account=self.accounts[0].id, amount=residual, base=residual)
        entry = self.attempt(
            ledger.PostJournalEntry(
                posting_date=on, description="Balanced", postings=(*postings, balancing)
            )
        )

        period_open = self.closed is None or on > self.closed[0]
        assert (entry is not None) == period_open
        if entry:
            self.entries.append(entry)

    @precondition(lambda self: self.archived)
    @rule(data=st.data(), on=dates)
    def post_to_archived_account(self, data: st.DataObject, on: date) -> None:
        archived = data.draw(st.sampled_from(sorted(self.archived)))
        one = Money.of(1, BASE)
        postings = (ledger.debit(archived, one), ledger.credit(self.accounts[0], one))
        entry = ledger.PostJournalEntry(posting_date=on, description="Late", postings=postings)
        assert self.attempt(entry) is None

    @rule(data=st.data(), on=dates)
    def post_arbitrary_entry(self, data: st.DataObject, on: date) -> None:
        known = st.sampled_from([account.id for account in self.accounts])
        posting = st.builds(
            Posting,
            account=known | st.builds(uuid4),
            amount=st.sampled_from(CURRENCIES).flatmap(amounts),
            base=st.sampled_from((BASE, BASE, "USD")).flatmap(amounts),
        )
        postings = data.draw(st.lists(posting, max_size=4))
        entry = self.attempt(
            ledger.PostJournalEntry(
                posting_date=on, description="Arbitrary", postings=tuple(postings)
            )
        )
        if entry:
            self.entries.append(entry)

    @precondition(lambda self: self.entries)
    @rule(data=st.data(), on=st.none() | dates)
    def reverse_entry(self, data: st.DataObject, on: date | None) -> None:
        original = data.draw(st.sampled_from(self.entries))
        reversal = self.attempt(ledger.ReverseJournalEntry(entry_id=original.id, posting_date=on))
        if reversal:
            self.entries.append(reversal)

    @rule(through=closing_dates)
    def close_period(self, through: date) -> None:
        if self.attempt(ledger.ClosePeriod(through=through)):
            self.closed = (through, ledger.trial_balance(self.kernel, as_of=through))

    @rule(data=st.data())
    def archive_account(self, data: st.DataObject) -> None:
        # The first account is never archived, so a balancing posting always has a home.
        account = data.draw(st.sampled_from(self.accounts[1:]))
        if self.attempt(ledger.ArchiveAccount(account_id=account.id)):
            self.archived.add(account.id)

    @invariant()
    def the_ledger_balances(self) -> None:
        assert Money.total(ledger.trial_balance(self.kernel).values(), BASE).is_zero

    @invariant()
    def posted_entries_never_change(self) -> None:
        stored = self.kernel.find(ledger.JournalEntry)
        assert sorted(stored, key=lambda e: e.id) == sorted(self.entries, key=lambda e: e.id)

    @invariant()
    def closed_periods_never_change(self) -> None:
        if self.closed:
            through, balances = self.closed
            now = ledger.trial_balance(self.kernel, as_of=through)
            assert {account: now[account] for account in balances} == balances

    @invariant()
    def entries_are_reversed_at_most_once(self) -> None:
        reversed_ids = [entry.reverses for entry in self.entries if entry.reverses]
        assert len(reversed_ids) == len(set(reversed_ids))

    @invariant()
    def archived_accounts_stay_empty(self) -> None:
        assert all(ledger.balance(self.kernel, account).is_zero for account in self.archived)

    @invariant()
    def pgledger_agrees_with_the_journal(self) -> None:
        base: defaultdict[UUID, Money] = defaultdict(lambda: Money.zero(BASE))
        foreign: defaultdict[UUID, dict[str, Money]] = defaultdict(dict)
        for posting in (posting for entry in self.entries for posting in entry.postings):
            currency = posting.amount.currency
            base[posting.account] += posting.base
            if currency != BASE:
                held = foreign[posting.account].get(currency, Money.zero(currency))
                foreign[posting.account][currency] = held + posting.amount

        for account in self.accounts:
            held = ledger.balance(self.kernel, account.id)
            assert held.base == base[account.id]
            assert held.foreign == foreign[account.id]

    @invariant()
    def the_books_reconcile(self) -> None:
        assert ledger.reconcile(self.kernel) == []


def test_the_invariants_hold(postgres: PostgresStore) -> None:
    LedgerMachine.store = postgres
    run_state_machine_as_test(
        LedgerMachine, settings=settings(max_examples=100, stateful_step_count=30, deadline=None)
    )


@st.composite
def balanced_postings(draw: st.DrawFn) -> list[Posting]:
    accounts = draw(st.lists(st.uuids(), min_size=2, max_size=5, unique=True))
    postings: list[Posting] = []
    for _ in range(draw(st.integers(min_value=1, max_value=6))):
        currency = draw(st.sampled_from(CURRENCIES))
        amount = draw(amounts(currency).filter(lambda m: not m.is_zero))
        value = amount if currency == BASE else draw(amounts(BASE, min_value=1))
        base = value if amount.sign > 0 else -abs(value)
        postings.append(Posting(account=draw(st.sampled_from(accounts)), amount=amount, base=base))
    residual = -Money.total((posting.base for posting in postings), BASE)
    if not residual.is_zero:
        account = draw(st.sampled_from(accounts))
        postings.append(Posting(account=account, amount=residual, base=residual))
    return postings


@given(balanced_postings())
def test_movements_have_exactly_the_effect_of_their_postings(postings: list[Posting]) -> None:
    expected: defaultdict[tuple[UUID, str], Decimal] = defaultdict(Decimal)
    for posting in postings:
        currency = posting.amount.currency
        expected[posting.account, BASE] += posting.base.amount
        if currency != BASE:
            expected[posting.account, currency] += posting.amount.amount
            expected[CLEARING, currency] -= posting.amount.amount

    moved: defaultdict[tuple[UUID, str], Decimal] = defaultdict(Decimal)
    for movement in movements(postings, BASE):
        assert movement.amount > 0
        assert movement.source != movement.target
        assert movement.source[1] == movement.target[1]
        moved[movement.target] += movement.amount
        moved[movement.source] -= movement.amount

    assert {book: amount for book, amount in moved.items() if amount} == {
        book: amount for book, amount in expected.items() if amount
    }
