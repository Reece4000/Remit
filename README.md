# Remit

A framework for building business systems that people and AI agents operate through the
same commands.

> **Status: early.** The kernel, the ledger and the first standard modules (parties and
> items) work and are heavily tested. There is no HTTP API yet. See [Roadmap](#roadmap).

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
- **Retries are safe.** A command an actor sends twice with the same idempotency key runs
  once. Keys belong to the actor, so two actors' keys never collide.
- **Modules react to each other through events.** A subscription's effects commit with
  its place in the event log, so each event takes effect exactly once however many
  workers run. A subscription that fails keeps its place and is retried, without
  holding up any other.
- **Everything is described by its own declarations.** Documents, lifecycles, commands
  and events are available as JSON Schema from the registry. A declaration without a
  docstring does not register.
- **Personal data can be erased without rewriting history.** Fields marked personal are
  stored encrypted in the audit trail under keys that belong to the person they are
  about; erasing the person deletes the keys. See
  [personal data](docs/personal-data.md).
- **Modules build on modules.** A module declares the modules it requires, may use
  only their public API and react only to their events, and brings them with it when
  installed. It writes only its own documents: to change another module's, it executes
  that module's commands. Remit's own standard modules follow the same rules as anyone
  else's.
- **Money is exact.** Currencies and their minor units come from the ISO 4217 table as
  published, an amount never mixes currencies, and arithmetic never rounds except where
  a rounding mode is named (`times`, `convert`, `allocate`).
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

packed = kernel.execute(PackConsignment(destination="Rotterdam"), actor=agent).result

try:
    kernel.execute(BookConsignment(consignment_id=packed.id), actor=agent)
except Rejected as refusal:
    print(refusal)  # cannot book shipping.consignment: fill in carrier, waybill first

book = BookConsignment(consignment_id=packed.id, carrier="DHL", waybill="1234567890")

preview = kernel.execute(book, actor=agent, dry_run=True)
print(preview.result.state)  # booked, and nothing was saved

kernel.execute(book, actor=agent, idempotency_key="book-1234567890")
kernel.execute(book, actor=agent, idempotency_key="book-1234567890")  # runs once
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
| Posted entries, the event log and the audit trail cannot be altered or emptied | Database triggers on rows and on TRUNCATE, as well as the kernel | Tests that write to the tables directly and expect refusal |
| Nothing changes except through a command | Writes go only through the kernel's commit; reads run in read-only transactions | Updates and inserts sent through `Kernel.sql` |
| Nothing is posted into a closed period | The ledger's commands and a database trigger | Both of the above |
| Each event takes effect once per subscription | The subscription's cursor commits with its effects | Several workers dispatching the same events at once |
| A change made from an out-of-date copy never overwrites a newer one | Amendments name the version they were read at | Threads amending the same party from the same copy |
| Codes and other unique values are never shared | A document's `unique` fields, checked by the kernel and indexed by the database | Threads racing for the same code, and direct inserts of a duplicate |
| A module imports, and reacts to events from, only the modules it requires | An architecture test over every import; the registry, for subscriptions | Modules that try to react to undeclared events |
| A module writes only its own documents and emits only its own events | The unit of work, which knows whose code is running | Commands and subscriptions that write another module's documents or emit its events |
| One subscription's failure never stops another's delivery | Each delivery is its own unit of work, and any error in it is recorded, not raised | Subscriptions that raise refusals and ordinary Python errors |
| Personal data in the audit trail is unreadable once its subject is erased, and never stored in the clear | Encryption under per-subject keys, and registration refuses personal data in events | Raw tables searched for personal values; erasure through nested data and replays |
| A kernel runs only modules its database was installed with, as currently defined | Each install records a fingerprint of the kernel's and each module's migrations, rules and unique fields | Kernels started against missing or changed installs |
| A migration, once applied, never changes | Each is recorded with a digest, and install refuses one that differs | Migrations edited after they were applied |
| Every store refuses the same things | Shared field types refuse unprintable text, and every commit is checked for text PostgreSQL cannot store | The kernel's tests, run against both stores |

`./check --deep` goes one step further: it breaks the framework in twenty-two ways, one
at a time, and confirms the tests fail for each.

Some things are trusted rather than checked, because modules are code you choose to
install:

- `context.sql` runs SQL with the kernel's own database rights, so a module that uses it
  could read or write other modules' tables, or the documents table itself, outside
  the ownership rule and without an audit entry. The database still refuses changes to
  final documents and to history, whoever sends them. A database role for each module,
  limited to its own tables, is the planned fix.
- `context.execute` runs another module's command on the authority of the command
  already running, without checking that permission again. That is deliberate: a clerk
  who may issue an invoice should not also need permission to post journal entries for
  the invoice to reach the ledger. Permissions are checked once, where an actor issues
  a command.

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
| `src/remit/parties`, `src/remit/items` | Standard modules: the people and organisations a business deals with, and the goods and services it handles. |
| `src/remit/ledger` | General ledger: accounts, journal entries, reversals, period closes. Built only on the kernel's public API. `books.py` is the bridge to pgledger, `rules.sql` holds what the database enforces, `vendor/pgledger` is pgledger v0.9.0 itself, unmodified. |
| `tests` | Kernel behaviour against a toy module; ledger examples and property tests; direct attacks on the database and races between commands. |
| `checks` | The mutation checks behind `./check --deep`. |
| `docs` | Design notes: [modules and templates](docs/modules-and-templates.md), [personal data](docs/personal-data.md). |
| `examples` | Five fictional companies built on Remit, used as learning material and as the test of its generality. |

Two layering rules are enforced by a test: the kernel imports no module, and a module
imports only `remit.kernel` and the top-level packages of the modules it requires.

## Roadmap

1. **The outside surface.** A REST API and an MCP server, both generated from the
   registry; a worker process for event dispatch; authentication; a container image.
2. **Controls.** Roles, limits per role, approval steps and segregation of duties.
3. **More kernel primitives.** Modules that add fields to, and refuse transitions of,
   documents they do not own; links between the lines of related
   documents; rules held as data; documents rendered through versioned templates; and a
   query layer for reporting.
4. **Business content.** Standard modules (parties, items, pricing, tax, banking, sales,
   purchasing, inventory), built exactly as any third-party module would be, and
   templates that give a new business a working starting point. See
   [modules and templates](docs/modules-and-templates.md), and the five
   [example companies](examples/) they are tested against.
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
The unit of measure codes in `src/remit/kernel/data/units.tsv` are UN/ECE
Recommendations 20 and 21, as published by OpenPeppol.
