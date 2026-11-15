"""What only a real database can show: its own refusals, and behaviour under concurrency."""

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import psycopg
import pytest
from conftest import DSN, ROOT, every, kernel_on, rows
from test_kernel import AGENT, MoveTicket, Notice, OpenTicket, TicketResolved, desk

from remit import items, ledger, parties
from remit.kernel import (
    Actor,
    ActorKind,
    Command,
    Context,
    Document,
    Kernel,
    Lifecycle,
    Module,
    Money,
    PostgresStore,
    Query,
    Reader,
    Rejected,
    RemitError,
)
from remit.ledger import AccountType, credit, debit

BOOKKEEPER = Actor(id="bookkeeper", kind=ActorKind.HUMAN, permissions=frozenset({"ledger.*"}))
CLERK = Actor(id="clerk", kind=ActorKind.HUMAN, permissions=frozenset({"parties.*", "items.*"}))
JAN_31 = date(2026, 1, 31)


def gbp(amount: str) -> Money:
    return Money.of(amount, "GBP")


@pytest.fixture
def kernel(postgres: PostgresStore) -> Kernel:
    return kernel_on(
        postgres,
        [ledger.module, desk, parties.module, items.module],
        settings=[ledger.LedgerSettings(base_currency="GBP")],
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
        "TRUNCATE audit",
        "TRUNCATE events",
        "TRUNCATE documents",
        "TRUNCATE data_keys CASCADE",
        "TRUNCATE cursors, idempotency",
        "TRUNCATE pgledger_entries CASCADE",
        "TRUNCATE ledger_books",
    ]
    for statement in tampering:
        with pytest.raises(psycopg.errors.IntegrityError), psycopg.connect(DSN) as connection:
            connection.execute(statement.encode())


def test_the_database_itself_refuses_a_duplicate_code(kernel: Kernel) -> None:
    command = parties.RegisterParty(code="C0001", kind=parties.PartyKind.PERSON, name="Amy")
    kernel.execute(command, actor=CLERK)
    copy = {"code": "C0001", "kind": "person", "name": "Impostor", "state": "active"}
    with pytest.raises(psycopg.errors.UniqueViolation), psycopg.connect(DSN) as connection:
        connection.execute(
            "INSERT INTO documents (kind, id, version, final, body) "
            "VALUES ('parties.party', %s, 1, false, %s)",
            (uuid4(), json.dumps({**copy, "id": str(uuid4())})),
        )


def test_personal_data_is_stored_only_sealed(kernel: Kernel) -> None:
    amy = kernel.execute(
        parties.RegisterParty(code="P0001", kind=parties.PartyKind.PERSON, name="Amy Shaw"),
        actor=CLERK,
        idempotency_key="register-amy",
    ).result
    kernel.execute(
        parties.AddContact(party_id=amy.id, name="Amy Shaw", email="amy@example.com"),
        actor=CLERK,
    )

    with psycopg.connect(DSN) as connection:
        (kept,) = connection.execute(
            "SELECT (SELECT string_agg(input::text, ' ') FROM audit) || "
            "(SELECT string_agg(record::text, ' ') FROM idempotency)"
        ).fetchone() or ("",)
    assert "Amy Shaw" not in kept
    assert "amy@example.com" not in kept
    assert "P0001" in kept, "codes are not personal and stay readable"

    kernel.execute(parties.EraseParty(party_id=amy.id), actor=CLERK)
    with psycopg.connect(DSN) as connection:
        (left,) = connection.execute(
            "SELECT count(*) FROM data_key_subjects WHERE subject = %s", (amy.id,)
        ).fetchone() or (None,)
    assert left == 0
    searched = " ".join(text for (text,) in rows("SELECT text FROM document_search"))
    assert "p0001" in searched
    assert "amy" not in searched, "what search reads is erased with the document"


def test_pgledger_itself_refuses_a_transfer_into_a_closed_period(kernel: Kernel, accounts) -> None:
    post(kernel, accounts, date(2026, 3, 1))
    kernel.execute(ledger.ClosePeriod(through=date(2026, 3, 31)), actor=BOOKKEEPER)
    [(bank,), (sales,)] = rows("SELECT id FROM pgledger_accounts_view ORDER BY name")

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
    assert kernel.ask(ledger.Reconcile(), actor=ROOT) == []

    with psycopg.connect(DSN) as connection:
        connection.execute("UPDATE pgledger_accounts SET balance = 1000 WHERE name = '1200:GBP'")

    assert kernel.ask(ledger.Reconcile(), actor=ROOT) == [
        "1200:GBP holds 1000 but its entries sum to 10.00",
        "GBP accounts sum to 990.00, not zero",
    ]


def test_one_account_code_is_won_by_exactly_one_of_many_claimants(kernel: Kernel) -> None:
    claim = ledger.OpenAccount(code="1200", name="Bank", type=AccountType.ASSET)

    results = race(8, lambda _: kernel.execute(claim, actor=BOOKKEEPER).result)

    assert sum(isinstance(result, ledger.Account) for result in results) == 1
    assert sum(isinstance(result, Rejected) for result in results) == 7
    assert len(every(kernel, ledger.Account)) == 1


def test_one_party_code_is_won_by_exactly_one_of_many_claimants(kernel: Kernel) -> None:
    claim = parties.RegisterParty(code="C0001", kind=parties.PartyKind.PERSON, name="Amy")

    results = race(8, lambda _: kernel.execute(claim, actor=CLERK).result)

    assert sum(isinstance(result, parties.Party) for result in results) == 1
    assert sum(isinstance(result, Rejected) for result in results) == 7
    assert len(every(kernel, parties.Party)) == 1


def test_one_item_code_is_won_by_exactly_one_of_many_claimants(kernel: Kernel) -> None:
    claim = items.AddItem(code="BOLT-M8", kind=items.ItemKind.GOODS, name="Hex bolt")

    results = race(8, lambda _: kernel.execute(claim, actor=CLERK).result)

    assert sum(isinstance(result, items.Item) for result in results) == 1
    assert sum(isinstance(result, Rejected) for result in results) == 7
    assert len(every(kernel, items.Item)) == 1


def test_of_many_amendments_from_the_same_copy_exactly_one_lands(kernel: Kernel) -> None:
    command = parties.RegisterParty(code="C0001", kind=parties.PartyKind.PERSON, name="Amy")
    amy = kernel.execute(command, actor=CLERK).result

    def amend(number: int) -> parties.Party:
        renamed = parties.AmendParty(
            party_id=amy.id, version=amy.version, name=f"Amy {number}", tax_number=None
        )
        return kernel.execute(renamed, actor=CLERK).result

    results = race(8, amend)

    [landed] = [result for result in results if isinstance(result, parties.Party)]
    assert all("has changed since version 1" in str(r) for r in results if r is not landed)
    assert kernel.get(parties.Party, amy.id, actor=ROOT) == landed


def test_an_entry_is_reversed_by_exactly_one_of_many_attempts(kernel: Kernel, accounts) -> None:
    entry = post(kernel, accounts, date(2026, 3, 1))
    reverse = ledger.ReverseJournalEntry(entry_id=entry.id)

    results = race(8, lambda _: kernel.execute(reverse, actor=BOOKKEEPER).result)

    assert sum(isinstance(result, ledger.JournalEntry) for result in results) == 1
    assert len(every(kernel, ledger.JournalEntry)) == 2
    assert kernel.ask(ledger.AccountBalance(account_id=accounts[0].id), actor=ROOT).is_zero


def test_a_retried_command_runs_once_however_many_copies_arrive(kernel: Kernel) -> None:
    command = ledger.OpenAccount(code="1200", name="Bank", type=AccountType.ASSET)

    results = race(
        8, lambda _: kernel.execute(command, actor=BOOKKEEPER, idempotency_key="open-bank")
    )

    assert len({o.result.id for o in results if not isinstance(o, Rejected)}) == 1
    assert not any(isinstance(outcome, Rejected) for outcome in results)
    assert len(kernel.audit(actor=ROOT)) == 1


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

    log = kernel.events(actor=ROOT)
    [closed_at] = [e.position for e in log if isinstance(e.event, ledger.PeriodClosed)]
    posted = [e.position for e in log if isinstance(e.event, ledger.JournalEntryPosted)]
    assert all(position < closed_at for position in posted)
    assert len(posted) == accepted_in_total == len(every(kernel, ledger.JournalEntry))
    assert rows("SELECT count(*) FROM pgledger_transfers") == [(accepted_in_total,)]
    assert Money.total(kernel.ask(ledger.TrialBalance(), actor=ROOT).values(), "GBP").is_zero
    assert kernel.ask(ledger.Reconcile(), actor=ROOT) == []


def test_many_workers_deliver_each_event_exactly_once(kernel: Kernel) -> None:
    for number in range(20):
        ticket = kernel.execute(OpenTicket(subject=f"Ticket {number}"), actor=AGENT).result
        kernel.execute(
            MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Done"), actor=AGENT
        )

    results = race(6, lambda _: kernel.dispatch().delivered)

    assert sum(result for result in results if isinstance(result, int)) == 20
    assert len(every(kernel, Notice)) == 20
    assert len({notice.ticket_id for notice in every(kernel, Notice)}) == 20


# A module whose query tries to change things, as a careless or hostile one might.
prying = Module("prying")


class Tamper(Query[None]):
    statement: str


@prying.query
def tamper(query: Tamper, reader: Reader) -> None:
    """Run a statement through a read."""
    reader.sql(query.statement)


def test_nothing_can_be_changed_through_a_read(postgres: PostgresStore) -> None:
    kernel = kernel_on(postgres, [desk, prying])
    ticket = kernel.execute(OpenTicket(subject="Printer"), actor=AGENT).result

    for statement in (
        'UPDATE documents SET body = body || \'{"subject": "Edited"}\'',
        "INSERT INTO cursors (subscription, position) VALUES ('desk.x', 99)",
        "DELETE FROM document_search",
    ):
        with pytest.raises(RemitError, match="nothing can be changed through a read"):
            kernel.ask(Tamper(statement=statement), actor=ROOT)
    assert kernel.get(type(ticket), ticket.id, actor=ROOT) == ticket
    assert len(kernel.audit(actor=ROOT)) == 1


# A module whose rule lives only in the database, as a trigger.

vault = Module(
    "vault",
    sql="""
    CREATE OR REPLACE FUNCTION vault_refuse_secrets() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.body ->> 'text' = 'secret' THEN
            RAISE EXCEPTION 'secrets are not kept here' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$;
    CREATE OR REPLACE TRIGGER vault_refuse_secrets BEFORE INSERT ON documents
        FOR EACH ROW WHEN (NEW.kind = 'vault.note') EXECUTE FUNCTION vault_refuse_secrets();
    """,
)


@vault.document
class Note(Document):
    """A note."""

    lifecycle = Lifecycle(initial="kept")

    text: str


class KeepNote(Command[Note]):
    text: str


@vault.command
def keep_note(command: KeepNote, context: Context) -> Note:
    """Keep a note."""
    return context.create(Note(text=command.text))


def test_a_dry_run_is_refused_by_rules_the_database_enforces(postgres: PostgresStore) -> None:
    kernel = kernel_on(postgres, [vault])

    with pytest.raises(Rejected, match="the database refused this: secrets are not kept here"):
        kernel.execute(KeepNote(text="secret"), actor=AGENT_EVERYWHERE, dry_run=True)
    with pytest.raises(Rejected, match="secrets are not kept here"):
        kernel.execute(KeepNote(text="secret"), actor=AGENT_EVERYWHERE)

    preview = kernel.execute(KeepNote(text="shopping"), actor=AGENT_EVERYWHERE, dry_run=True)
    assert not preview.committed
    assert every(kernel, Note) == []
    assert kernel.audit(actor=ROOT) == []


AGENT_EVERYWHERE = Actor(id="agent:all", kind=ActorKind.AGENT, permissions=frozenset({"*"}))


archivist = Module("archivist", requires=(vault, desk))


@archivist.subscribe(TicketResolved)
def file_secretly(event: TicketResolved, context: Context) -> None:
    """Keep a note the vault's database rule refuses when the unit commits."""
    context.execute(KeepNote(text="secret"))


def test_a_delivery_the_database_refuses_does_not_stop_the_others(
    postgres: PostgresStore,
) -> None:
    kernel = kernel_on(postgres, [archivist])
    ticket = kernel.execute(OpenTicket(subject="Printer"), actor=AGENT).result
    kernel.execute(
        MoveTicket(ticket_id=ticket.id, transition="resolve", resolution="Done"), actor=AGENT
    )

    dispatched = kernel.dispatch()

    [failure] = dispatched.failures
    assert failure.subscription == "archivist.file_secretly"
    assert "secrets are not kept here" in failure.error
    assert len(every(kernel, Notice)) == 1
