"""What only a real database can show: its own refusals, and behaviour under concurrency."""

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import psycopg
import pytest
from conftest import DSN
from test_kernel import AGENT, MoveTicket, Notice, OpenTicket, desk

from remit import ledger
from remit.kernel import Actor, ActorKind, Kernel, Money, PostgresStore, Rejected
from remit.ledger import AccountType, credit, debit

BOOKKEEPER = Actor(id="bookkeeper", kind=ActorKind.HUMAN, permissions=frozenset({"ledger.*"}))
JAN_31 = date(2026, 1, 31)


def gbp(amount: str) -> Money:
    return Money.of(amount, "GBP")


@pytest.fixture
def kernel(postgres: PostgresStore) -> Kernel:
    return Kernel(
        [ledger.module, desk],
        settings=[ledger.LedgerSettings(base_currency="GBP")],
        store=postgres,
    )


@pytest.fixture
def accounts(kernel: Kernel) -> tuple[ledger.Account, ledger.Account]:
    def open(code: str, type: AccountType) -> ledger.Account:
        command = ledger.OpenAccount(code=code, name=code, type=type)
        return kernel.execute(command, actor=BOOKKEEPER).result

    return open("1200", AccountType.ASSET), open("4000", AccountType.INCOME)


def post(kernel: Kernel, accounts: tuple[ledger.Account, ledger.Account], on: date):
    bank, sales = accounts
    postings = (debit(bank, gbp("10")), credit(sales, gbp("10")))
    command = ledger.PostJournalEntry(posting_date=on, description="Sale", postings=postings)
    return kernel.execute(command, actor=BOOKKEEPER).result


def insert_journal_entry(amounts: tuple[str, ...], on: date = date(2026, 3, 1)) -> None:
    """Write a journal entry straight into the table, as a kernel bug or an intruder might."""
    id = uuid4()
    body = {
        "id": str(id),
        "state": "posted",
        "version": 1,
        "posting_date": on.isoformat(),
        "description": "Behind the kernel's back",
        "postings": [
            {
                "account": str(uuid4()),
                "amount": {"amount": amount, "currency": "GBP"},
                "base": {"amount": amount, "currency": "GBP"},
            }
            for amount in amounts
        ],
        "reverses": None,
    }
    with psycopg.connect(DSN) as connection:
        connection.execute(
            "INSERT INTO documents (kind, id, version, final, body) "
            "VALUES ('ledger.journal_entry', %s, 1, true, %s)",
            (id, json.dumps(body)),
        )


def race[T](contenders: int, attempt: Callable[[int], T]) -> list[T | Rejected]:
    """Run `attempt` on several threads released at the same instant."""
    start = threading.Barrier(contenders)

    def run(number: int) -> T | Rejected:
        start.wait()
        try:
            return attempt(number)
        except Rejected as refusal:
            return refusal

    with ThreadPoolExecutor(contenders) as threads:
        return list(threads.map(run, range(contenders)))


def test_the_database_itself_refuses_an_unbalanced_entry(postgres: PostgresStore) -> None:
    insert_journal_entry(("10.00", "-10.00"))
    with pytest.raises(psycopg.errors.CheckViolation, match="does not balance"):
        insert_journal_entry(("10.00", "-9.99"))
    with pytest.raises(psycopg.errors.CheckViolation, match="fewer than two postings"):
        insert_journal_entry(())


def test_the_database_itself_refuses_an_entry_in_a_closed_period(kernel: Kernel) -> None:
    kernel.execute(ledger.ClosePeriod(through=JAN_31), actor=BOOKKEEPER)
    with pytest.raises(psycopg.errors.CheckViolation, match="closed through 2026-01-31"):
        insert_journal_entry(("10.00", "-10.00"), on=JAN_31)


def test_the_database_itself_refuses_to_alter_history(kernel: Kernel, accounts) -> None:
    post(kernel, accounts, date(2026, 3, 1))
    tampering = [
        'UPDATE documents SET body = body || \'{"description": "Edited"}\' '
        "WHERE kind = 'ledger.journal_entry'",
        "DELETE FROM documents WHERE kind = 'ledger.journal_entry'",
        "DELETE FROM documents WHERE kind = 'ledger.account'",
        "UPDATE documents SET version = 7 WHERE kind = 'ledger.account'",
        "UPDATE pgledger_entries SET amount = 0",
        "DELETE FROM pgledger_entries",
        "UPDATE pgledger_transfers SET amount = 1",
        "DELETE FROM pgledger_transfers",
        "UPDATE events SET actor = 'someone else'",
        "DELETE FROM events",
        "UPDATE audit SET action = 'nothing happened'",
        "DELETE FROM audit",
    ]
    for statement in tampering:
        with pytest.raises(psycopg.errors.IntegrityError), psycopg.connect(DSN) as connection:
            connection.execute(statement.encode())


def test_pgledger_itself_refuses_a_transfer_into_a_closed_period(kernel: Kernel, accounts) -> None:
    post(kernel, accounts, date(2026, 3, 1))
    kernel.execute(ledger.ClosePeriod(through=date(2026, 3, 31)), actor=BOOKKEEPER)
    [(bank,), (sales,)] = kernel.sql("SELECT id FROM pgledger_accounts_view ORDER BY name")

    with (
        pytest.raises(psycopg.errors.CheckViolation, match="closed through 2026-03-31"),
        psycopg.connect(DSN) as connection,
    ):
        connection.execute(
            "SELECT * FROM pgledger_create_transfer(%s, %s, 5, event_at => '2026-03-15')",
            (bank, sales),
        )


def test_a_balance_altered_behind_pgledgers_back_is_reported(kernel: Kernel, accounts) -> None:
    post(kernel, accounts, date(2026, 3, 1))
    assert ledger.reconcile(kernel) == []

    with psycopg.connect(DSN) as connection:
        connection.execute("UPDATE pgledger_accounts SET balance = 1000 WHERE name = '1200:GBP'")

    assert ledger.reconcile(kernel) == [
        "1200:GBP holds 1000 but its entries sum to 10.00",
        "GBP accounts sum to 990.00, not zero",
    ]


def test_one_account_code_is_won_by_exactly_one_of_many_claimants(kernel: Kernel) -> None:
    claim = ledger.OpenAccount(code="1200", name="Bank", type=AccountType.ASSET)

    results = race(8, lambda _: kernel.execute(claim, actor=BOOKKEEPER).result)

    assert sum(isinstance(result, ledger.Account) for result in results) == 1
    assert sum(isinstance(result, Rejected) for result in results) == 7
    assert len(kernel.find(ledger.Account)) == 1


def test_an_entry_is_reversed_by_exactly_one_of_many_attempts(kernel: Kernel, accounts) -> None:
    entry = post(kernel, accounts, date(2026, 3, 1))
    reverse = ledger.ReverseJournalEntry(entry_id=entry.id)

    results = race(8, lambda _: kernel.execute(reverse, actor=BOOKKEEPER).result)

    assert sum(isinstance(result, ledger.JournalEntry) for result in results) == 1
    assert len(kernel.find(ledger.JournalEntry)) == 2
    assert ledger.balance(kernel, accounts[0].id).is_zero


def test_a_retried_command_runs_once_however_many_copies_arrive(kernel: Kernel) -> None:
    command = ledger.OpenAccount(code="1200", name="Bank", type=AccountType.ASSET)

    results = race(
        8, lambda _: kernel.execute(command, actor=BOOKKEEPER, idempotency_key="open-bank")
    )

    assert len({o.result.id for o in results if not isinstance(o, Rejected)}) == 1
    assert not any(isinstance(outcome, Rejected) for outcome in results)
    assert len(kernel.audit()) == 1


def test_nothing_is_posted_into_a_period_once_it_is_closed(kernel: Kernel, accounts) -> None:
    def accepted() -> bool:
        try:
            post(kernel, accounts, JAN_31)
        except Rejected:
            return False
        return True

    def attempt(number: int) -> int:
        if number == 0:
            kernel.execute(ledger.ClosePeriod(through=JAN_31), actor=BOOKKEEPER)
            return 0
        return sum(accepted() for _ in range(15))

    accepted_in_total = sum(count for count in race(8, attempt) if isinstance(count, int))

    log = kernel.events()
    [closed_at] = [e.position for e in log if isinstance(e.event, ledger.PeriodClosed)]
    posted = [e.position for e in log if isinstance(e.event, ledger.JournalEntryPosted)]
    assert all(position < closed_at for position in posted)
    assert len(posted) == accepted_in_total == len(kernel.find(ledger.JournalEntry))
    assert kernel.sql("SELECT count(*) FROM pgledger_transfers") == [(accepted_in_total,)]
    assert Money.total(ledger.trial_balance(kernel).values(), "GBP").is_zero
    assert ledger.reconcile(kernel) == []


def test_many_workers_deliver_each_event_exactly_once(kernel: Kernel) -> None:
    for number in range(20):
        ticket = kernel.execute(OpenTicket(subject=f"Ticket {number}"), actor=AGENT).result
        kernel.execute(
            MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Done"), actor=AGENT
        )

    results = race(6, lambda _: kernel.dispatch().delivered)

    assert sum(result for result in results if isinstance(result, int)) == 20
    assert len(kernel.find(Notice)) == 20
    assert len({notice.ticket_id for notice in kernel.find(Notice)}) == 20
