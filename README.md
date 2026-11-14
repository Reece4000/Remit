# Remit

A framework for building business systems that people and AI agents operate through the
same commands.

> **Status: early.** The kernel and the ledger work and are heavily tested. There is no
> HTTP API yet, and no business modules beyond the ledger. See [Roadmap](#roadmap).

## Why

Off-the-shelf ERPs make a business fit the software. Building your own fits the
business, but every team then rebuilds the same hard parts: a ledger that cannot go
wrong, an audit trail, permissions, a way for one part of the system to react to
another.

Remit does those parts once, so that what you write is only what is particular to your
business. It is designed for a world where some of the staff are agents: an agent holds
a role exactly as a person does, with the same permissions, the same checks and the same
entry in the audit trail.

## How it works

- **Commands are the only way anything changes.** A command is typed, issued by an
  actor, and run inside one database transaction together with the events it causes and
  its audit entry. If it is refused, nothing is left behind.
- **Documents have declared lifecycles.** Each transition names the fields it needs.
  A document in a final state can never change again.
- **People and agents are the same kind of actor.** Both hold permissions and both are
  refused in the same words, which an agent can read and act on.
- **Anything can be tried first.** A dry run executes a command for real and rolls it
  back, reporting what would have happened.
- **Retries are safe.** A command sent twice with the same idempotency key runs once.
- **Modules react to each other through events.** A subscription's effects commit with
  its place in the event log, so each event takes effect exactly once however many
  workers run.
- **Everything is described by its own declarations.** Documents, lifecycles, commands
  and events are available as JSON Schema from the registry. A declaration without a
  docstring does not register.
- **The books are double-entry and multi-currency**, kept by
  [pgledger](https://github.com/pgr0ss/pgledger) in the same transaction as everything
  else.

## A taste

A module declares its documents, events and commands:

```python
from uuid import UUID

from remit.kernel import Command, Context, Document, Event, Lifecycle, Module, Transition

shipping = Module("shipping")


@shipping.document
class Consignment(Document):
    """Goods on their way to a customer."""

    lifecycle = Lifecycle(
        initial="packed",
        transitions=(
            Transition("book", "packed", "booked", requires=("carrier", "waybill")),
            Transition("deliver", "booked", "delivered"),
        ),
    )

    destination: str
    carrier: str | None = None
    waybill: str | None = None


@shipping.event
class ConsignmentBooked(Event):
    """A consignment was booked with a carrier."""

    consignment_id: UUID


class PackConsignment(Command[Consignment]):
    destination: str


class BookConsignment(Command[Consignment]):
    consignment_id: UUID
    carrier: str | None = None
    waybill: str | None = None


@shipping.command
def pack_consignment(command: PackConsignment, context: Context) -> Consignment:
    """Record goods as packed and ready to book."""
    return context.create(Consignment(destination=command.destination))


@shipping.command
def book_consignment(command: BookConsignment, context: Context) -> Consignment:
    """Book a packed consignment with a carrier."""
    consignment = context.get(Consignment, command.consignment_id)
    booked = context.transition(
        consignment, "book", carrier=command.carrier, waybill=command.waybill
    )
    context.emit(ConsignmentBooked(consignment_id=booked.id))
    return booked
```

An actor, here an agent, works through the kernel:

```python
from remit import ledger
from remit.kernel import Actor, ActorKind, Kernel, PostgresStore, Rejected

store = PostgresStore("postgresql:///remit")
store.install([ledger.module, shipping])
kernel = Kernel(
    [ledger.module, shipping],
    settings=[ledger.LedgerSettings(base_currency="GBP")],
    store=store,
)

agent = Actor(id="agent:dispatch", kind=ActorKind.AGENT, permissions=frozenset({"shipping.*"}))

packed = kernel.execute(PackConsignment(destination="Nairobi"), actor=agent).result

try:
    kernel.execute(BookConsignment(consignment_id=packed.id), actor=agent)
except Rejected as refusal:
    print(refusal)  # cannot book shipping.consignment: fill in carrier, waybill first

book = BookConsignment(consignment_id=packed.id, carrier="KQ", waybill="706-12345675")

preview = kernel.execute(book, actor=agent, dry_run=True)
print(preview.result.state)  # booked, and nothing was saved

kernel.execute(book, actor=agent, idempotency_key="book-706-12345675")
kernel.execute(book, actor=agent, idempotency_key="book-706-12345675")  # runs once
```

The ledger is a module like any other. Each posting carries its amount in its own
currency and its value in the base currency:

```python
from datetime import date

from remit.kernel import Money
from remit.ledger import AccountType, credit, debit

bookkeeper = Actor(id="amy", kind=ActorKind.HUMAN, permissions=frozenset({"ledger.*"}))


def open_account(code: str, name: str, type: AccountType, currency: str | None = None):
    command = ledger.OpenAccount(code=code, name=name, type=type, currency=currency)
    return kernel.execute(command, actor=bookkeeper).result


receivables = open_account("1100", "Receivables (USD)", AccountType.ASSET, "USD")
sales = open_account("4000", "Sales", AccountType.INCOME)

kernel.execute(
    ledger.PostJournalEntry(
        posting_date=date(2026, 1, 15),
        description="Invoice 1001",
        postings=(
            debit(receivables, Money.of("1000", "USD"), base=Money.of("800", "GBP")),
            credit(sales, Money.of("1000", "USD"), base=Money.of("800", "GBP")),
        ),
    ),
    actor=bookkeeper,
)

held = ledger.balance(kernel, receivables.id)
print(held.base, held.foreign["USD"])  # GBP 800.00 USD 1000.00
```

Another module posts to the ledger by executing `PostJournalEntry` from its own handler,
which puts the posting in the same transaction as the document that caused it.

## What is guaranteed, and how it is checked

| Guarantee | Enforced by | Checked by |
|---|---|---|
| A command's documents, events, audit entry and postings commit together or not at all | One database transaction per command | Tests that refuse commands midway and look for traces |
| Rules a handler checks still hold when it commits, whatever else is running | Serialisable transactions, retried on conflict | Threads racing for the same account code, reversal, period close and event |
| The ledger always balances | The ledger's commands, a database trigger, and pgledger's two-sided transfers | Property tests over random command sequences, recomputing each balance independently |
| Posted entries, the event log and the audit trail cannot be altered | Database triggers, as well as the kernel | Tests that write to the tables directly and expect refusal |
| Nothing is posted into a closed period | The ledger's commands and a database trigger | Both of the above |
| Each event takes effect once per subscription | The subscription's cursor commits with its effects | Several workers dispatching the same events at once |

`./check --deep` goes one step further: it breaks the framework in nine ways, one at a
time, and confirms the tests fail for each.

## Getting started

Requires Python 3.14, [uv](https://docs.astral.sh/uv/) and PostgreSQL. It is developed
and tested on PostgreSQL 18.

```sh
createdb remit_test        # once; or point REMIT_TEST_DSN at another database
./check                    # creates .venv from uv.lock on first run
```

## Testing

`./check` is the gate. It installs exactly the versions in `uv.lock`, then runs format,
lint (including security rules), strict type checking, the full test suite on
PostgreSQL, and branch coverage against a floor that only ever rises. It takes about
twenty seconds. There is no hosted CI.

`./check --deep` adds an audit of dependencies for known vulnerabilities, the mutation
checks described above, and twenty repeats of the concurrency tests. It takes about a
minute.

To run the gate before every push:

```sh
git config core.hooksPath .githooks
```

Kernel tests run in memory and on PostgreSQL; ledger tests need PostgreSQL. Running
`pytest` directly skips those when no database answers, and says so.

## Layout

| Path | Contents |
|---|---|
| `src/remit/kernel` | Business-agnostic mechanics: money, actors, documents, commands, events, the registry, the unit of work, and the memory and PostgreSQL stores. |
| `src/remit/ledger` | General ledger: accounts, journal entries, reversals, period closes. Built only on the kernel's public API. `books.py` is the bridge to pgledger, `rules.sql` holds what the database enforces, `vendor/pgledger` is pgledger v0.9.0 itself, unmodified. |
| `tests` | Kernel behaviour against a toy module; ledger examples and property tests; direct attacks on the database and races between commands. |
| `checks` | The mutation checks behind `./check --deep`. |

Two layering rules are enforced by a test: the kernel imports no module, and modules
import only `remit.kernel`.

## Roadmap

1. **The outside surface.** A REST API and an MCP server, both generated from the
   registry; a worker process for event dispatch; authentication; a container image.
2. **Controls.** Roles, limits per role, approval steps and segregation of duties.
3. **More kernel primitives.** Links between the lines of related documents, rules held
   as data, documents rendered through versioned templates, and a query layer for
   reporting.
4. **Business content.** Standard modules (parties, items, pricing, sales, purchasing,
   inventory), tax and financial statements, and templates that preconfigure them for
   common kinds of business.
5. **Agents and workflows.** An agent runtime that fills a role within its limits and
   escalates when refused, durable workflows triggered by events, and authoring both
   from plain-language descriptions.

## Licence

Copyright (C) 2026 Reece Mitchell.

Remit is free software, licensed under the
[GNU Affero General Public License](LICENSE), version 3 or (at your option) any later
version. You may use, change and share it, including commercially. If you distribute
it, or software built on it, or let people use either over a network, you must make the
complete source available to them under the same licence. It comes with no warranty.

To use Remit in software you do not want to release under the AGPL, ask about a
commercial licence.

The vendored copy of pgledger and its ULID helpers keep their own licences (MIT and
BSD 3-Clause), which are included alongside them in `src/remit/ledger/vendor/pgledger`.
