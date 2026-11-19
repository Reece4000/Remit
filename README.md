# Remit

A framework for building business systems that people and AI agents operate through the
same commands.

> **Status: early.** The kernel, the ledger, the first standard modules (parties, items
> and access) and the REST API work and are heavily tested. There is no MCP server yet.
> See [Roadmap](#roadmap).

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
- **Reading is permitted the way changing is.** Every read names the actor who is
  reading. `ledger.read.*` lets a role read the ledger and change nothing; `ledger.*`
  grants both; `*.read.*` suits an auditor.
- **Every kind of document can be listed, with nothing written for it.** One call
  filters, searches, orders and pages any of them, and means the same on every store.
  Reads that take code, such as a trial balance, are declared as queries, with a
  docstring, a typed input and a typed result, exactly as commands are.
- **Everything installed is served over HTTP, with nothing written for it.** Each
  command, document and query gets its own route and its own types in the OpenAPI
  document, so a client generated from it is typed throughout. Errors are problem
  details that name each field at fault.
- **Signing in is an identity provider's job; what someone may do is Remit's.** Any
  OpenID Connect provider can vouch for who a person or agent is. Their roles are
  documents in Remit, changed by commands, so every grant and revocation is in the audit
  trail, and takes effect on the next request.
- **What changes can be followed as it commits.** A feed of changes, which each reader
  sees only as far as they may read, and a stream of it, tell a screen what to refresh.
  Every document has a history: each unit of work that wrote it, and who did.
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
  docstring does not register; a docstring under a field describes the field. A field
  that holds another document's id says which kind of document, so a screen can offer
  a choice of them and an agent knows what to look up.
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

held = kernel.ask(ledger.AccountBalance(account_id=receivables.id), actor=bookkeeper)
print(held.base, held.foreign["USD"])  # GBP 800.00 USD 1000.00
```

Another module posts to the ledger by executing `PostJournalEntry` from its own handler,
which puts the posting in the same transaction as the document that caused it.

## Reading

An actor who may read a kind of document can list it:

```python
auditor = Actor(id="agent:audit", kind=ActorKind.AGENT, permissions=frozenset({"*.read.*"}))

page = kernel.page(
    ledger.JournalEntry,
    actor=auditor,
    where={"posting_date": {"gte": "2026-01-01", "lt": "2026-02-01"}},
    order="-posting_date",
    limit=50,
)
following = kernel.page(..., after=page.next)  # the same listing, continued

kernel.page(ledger.Account, actor=auditor, search="receiv")  # finds "Receivables (USD)"
kernel.ask(ledger.TrialBalance(as_of=date(2026, 1, 31)), actor=auditor)
```

- **`where`** maps a field to the value it must equal, or to operators: `eq`, `ne`,
  `in`, `gt`, `gte`, `lt`, `lte`. Values are checked against the field's type, so dates
  compare as dates, amounts as numbers and times as instants, and a misspelt field is
  refused instead of matching nothing. Every fault in a listing is reported at once,
  each against the part at fault.
- **`search`** looks in the fields a document declares `searchable`. It finds the
  documents holding every word of the search, whatever the case, accents or width
  (`muller` finds `Müller`), and puts exact matches first, then those that start with
  the search. On PostgreSQL a trigram index (`pg_trgm`, which ships with it) makes that
  fast. The index does not decide what matches: the kernel does, so tests on the
  memory store predict production.
- **`order`** names one field, `-` first for descending. Ties are broken by id, which
  is also the order of creation.
- **`after`** takes the previous page's `next`. Pages are reached by cursor, so
  documents added between two pages are neither skipped nor repeated.

A query is declared like a command, and its handler is given a reader, which has no
means of writing:

```python
class Overdue(Query[tuple[Consignment, ...]]):
    days: int = 7
    """How long ago a consignment was packed before it counts as overdue."""


@shipping.query
def overdue(query: Overdue, reader: Reader) -> tuple[Consignment, ...]:
    """Consignments packed some days ago and still not booked."""
    ...
```

It is registered as `shipping.overdue`, asked with `kernel.ask(Overdue(), actor=...)`
by an actor holding `shipping.read.overdue`, and asked by other modules' handlers with
`context.ask(Overdue())`.

## Serving

The server runs the API, a worker that delivers events, and the setup around them, all
from one container image. To run everything on one machine, with PostgreSQL and a
Keycloak to sign in through:

```sh
docker compose up --build
docker compose run --rm api enrol "Local Owner" --subject 0b9f6a3e-2c1d-4f5e-8a7b-1c2d3e4f5a6b
```

`enrol` makes the first member, holding every permission; they enrol everyone else
through the API. The local Keycloak has a user `owner` (password `owner`) with that id,
and an agent client, `remit-agent`, which signs in with its client credentials as an
agent does. Its token goes in every request:

```sh
token=$(curl -s localhost:8080/realms/remit/protocol/openid-connect/token \
  -d grant_type=password -d client_id=remit-desktop -d username=owner -d password=owner \
  | jq -r .access_token)

curl -s localhost:8000/commands/parties.register_party \
  -H "Authorization: Bearer $token" -H "Idempotency-Key: hollander-1" \
  --json '{"code": "C0001", "kind": "organisation", "name": "Hollander Farms"}'

curl -s "localhost:8000/documents/parties.party?search=hollander" -H "Authorization: Bearer $token"
curl -sN localhost:8000/changes/stream -H "Authorization: Bearer $token"
```

| Route | What it does |
|---|---|
| `POST /commands/{name}` | Runs a command. `Idempotency-Key` makes a retry safe; `?dry_run=true` changes nothing. |
| `GET /documents/{kind}` | Lists documents: `where` (JSON), `search`, `order`, `after`, `limit`. |
| `GET /documents/{kind}/{id}` | Reads one, with its version as its `ETag`. `/history` lists what wrote it. |
| `POST /queries/{name}` | Asks a query. It changes nothing; its input is a body. |
| `GET /changes`, `GET /changes/stream` | Follows what is written, as pages or as server-sent events. |
| `GET /audit`, `GET /events` | The audit trail, of everything or one document, and the event log. |
| `GET /registry`, `GET /me` | What is installed, and who is asking and what they may do. |
| `GET /openapi.json`, `GET /docs` | The OpenAPI 3.2 document, and a page to try it from. |

In production the same image runs `remit serve`, `remit worker` and, on each release,
`remit install`, configured from the environment:

| Variable | Meaning |
|---|---|
| `REMIT_DATABASE` | The PostgreSQL connection string. |
| `REMIT_APPLICATION` | What to run, as `package.module:name`: an `Application` naming the modules and their settings. `remit.server.standard:application` runs parties, items and the ledger, in `REMIT_BASE_CURRENCY`. |
| `REMIT_PROVIDERS` | The identity providers trusted, as JSON: `[{"issuer": ..., "audience": ...}]`. The audience should name this API alone. `discovery` gives another address to fetch the provider's keys from, such as inside a private network. |
| `REMIT_CONNECTIONS` | Database connections per process; ten by default. Scale by running more processes. |

A token is accepted only from a trusted issuer, signed with an asymmetric algorithm by a
key that issuer publishes, for the audience configured, with a subject, and in date.
Keys are cached and fetched again when a provider rotates them, at most once every ten
seconds however many unknown keys are sent. Providers on plain http are refused unless
`REMIT_INSECURE_PROVIDERS` is set, which compose.yaml does for the local Keycloak.

## What is guaranteed, and how it is checked

| Guarantee | Enforced by | Checked by |
|---|---|---|
| A command's documents, events, audit entry and postings commit together or not at all | One database transaction per command | Tests that refuse commands midway and look for traces |
| Rules a handler checks still hold when it commits, whatever else is running | Serialisable transactions, retried on conflict | Threads racing for the same account code, reversal, period close and event |
| The ledger always balances | The ledger's commands, a database trigger, and pgledger's two-sided transfers | Property tests over random command sequences, recomputing each balance independently |
| Posted entries, the event log and the audit trail cannot be altered or emptied | Database triggers on rows and on TRUNCATE, as well as the kernel | Tests that write to the tables directly and expect refusal |
| Nothing changes except through a command | Writes go only through the kernel's commit; reads run in read-only transactions, and a query's handler is given no means of writing | Updates, inserts and deletes sent through a query; the reader a command's query is handed |
| Nothing is read without permission to read it | Every read through the kernel names an actor and is checked; there is no unchecked read | Actors without the permission, for documents, queries, the audit trail and ids that do not exist |
| A listing means the same on every store | One definition, in Python; PostgreSQL's SQL is held to it | Hundreds of random listings, paged through on both stores and compared |
| Paging neither skips nor repeats documents as others are added | Cursors mark a place in the order, and continue only the listing that gave them | Documents added between pages; cursors forged, or taken to another listing |
| Search reads only what the document now holds | What is searched is written with the document, and rebuilt on install when a module declares it differently | Searching for the erased; the search table read directly; reinstalls |
| Nothing is posted into a closed period | The ledger's commands and a database trigger | Both of the above |
| Each event takes effect once per subscription | The subscription's cursor commits with its effects | Several workers dispatching the same events at once |
| A change made from an out-of-date copy never overwrites a newer one | Amendments name the version they were read at | Threads amending the same party from the same copy |
| A reference always leads to a document of the kind it names | The kernel, whenever a document is written; documents are never deleted | Writes referring to missing documents, from optional fields, tuples and nested models |
| Codes and other unique values are never shared | A document's `unique` fields, alone or together, checked by the kernel and indexed by the database | Threads racing for the same code, direct inserts of a duplicate, and fields unique together that each repeat |
| Anything following the changes feed or the audit trail by position never misses a commit | Every commit takes its turn from the moment it is numbered, so commits become visible in order | A unit held mid-commit while another tries to commit; writers racing a follower |
| The changes feed shows each reader only what they may read | Each change lists only the documents of kinds the reader may read | Readers with and without those permissions, through the kernel and the API |
| Only a token a trusted provider issued for this API, in date, signs anyone in | The verifier: issuer, asymmetric signature by a published key, audience, subject, expiry | Forged keys, the public key used as a shared secret, unsigned tokens, other audiences, expired tokens, rotated keys, and a real Keycloak |
| A revoked role or a suspended member is refused from the next request | The access module signs the token's subject in on every request, and on every pass of a stream | Revocation and suspension between requests and during a stream |
| No one grants, changes or takes away permissions they do not hold | The access module's commands | Managers defining, granting, widening and revoking above themselves |
| A module imports, and reacts to events from, only the modules it requires | An architecture test over every import; the registry, for subscriptions | Modules that try to react to undeclared events |
| A module writes only its own documents and emits only its own events | The unit of work, which knows whose code is running | Commands and subscriptions that write another module's documents or emit its events |
| One subscription's failure never stops another's delivery | Each delivery is its own unit of work, and any error in it is recorded, not raised | Subscriptions that raise refusals and ordinary Python errors |
| Personal data in the audit trail is unreadable once its subject is erased, and never stored in the clear | Encryption under per-subject keys, and registration refuses personal data in events | Raw tables searched for personal values; erasure through nested data and replays |
| A kernel runs only modules its database was installed with, as currently defined | Each install records a fingerprint of the kernel's and each module's migrations, rules and unique fields | Kernels started against missing or changed installs |
| A migration, once applied, never changes | Each is recorded with a digest, and install refuses one that differs | Migrations edited after they were applied |
| Every store refuses the same things | Shared field types refuse unprintable text, and every commit is checked for text PostgreSQL cannot store | The kernel's tests, run against both stores |

`./check --deep` goes one step further: it breaks the framework in forty ways, one at a
time, and confirms the tests fail for each.

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
- A handler reads on the authority of what it is handling, in the same way: an actor
  who may ask for a trial balance need not also hold permission to read every account.
  What a command or a query gives back is therefore up to the module that wrote it.
- Whoever can run `remit enrol` can make a member with every permission. They can reach
  the database already, so this gives them nothing they lack, and it is how a business
  locked out of its own access recovers.

And some things reading does not do yet:

- Permission to read covers a whole kind of document. Limiting it to some documents
  (a salesperson's own customers) or some fields (hiding personal data from an agent)
  belongs with roles and limits, under Controls in the roadmap. Every read passes
  through one check in the kernel, which is where those will go.
- Reads are not recorded in the audit trail.
- Text is ordered by code point, which every store agrees on and no upgrade can
  change: capitals sort before lower case, and accented letters after `z`.
- A listing ordered by anything but id is sorted by the database on each request, with
  no index of its own. That suits kinds with hundreds of thousands of documents, not
  hundreds of millions.
- A listing does not count how many documents match.
- Search does not forgive misspellings.

And some things the server does not do yet:

- It speaks plain HTTP, to be run behind a proxy or ingress that terminates TLS and
  limits request rates, as a container platform provides.
- A token revoked at the identity provider is accepted until it expires, since tokens
  are checked by signature rather than by asking the provider. Suspending the member in
  Remit takes effect at once, so keep access tokens short-lived.
- When an agent acts, the audit trail names the agent, not who instructed it.
- An actor can suspend themselves or revoke their own last role. `remit enrol` recovers.
- Logs are plain lines. FastAPI can export traces, metrics and logs to OpenTelemetry
  when it is configured to, but nothing here configures it.

## Getting started

Requires Python 3.14, [uv](https://docs.astral.sh/uv/) and PostgreSQL. It is developed
and tested on PostgreSQL 18.

```sh
createdb remit_test        # once; or point REMIT_TEST_DSN at another database
./check                    # creates .venv from uv.lock on first run
```

To run the server, see [Serving](#serving); it needs only Docker.

## Testing

`./check` is the gate. It installs exactly the versions in `uv.lock`, then runs format,
lint (including security rules), strict type checking, the full test suite on
PostgreSQL, and branch coverage against a floor that only ever rises. It takes about a
minute and a half. There is no hosted CI.

`./check --deep` adds an audit of dependencies for known vulnerabilities, the mutation
checks described above, and twenty repeats of the concurrency tests. It takes a few
minutes.

The API's tests sign in through an identity provider with a real key pair, reached over
in-memory HTTP. To sign in through a real Keycloak as well:

```sh
docker compose up -d identity
REMIT_TEST_KEYCLOAK=http://localhost:8080 ./check
```

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
| `src/remit/parties`, `src/remit/items`, `src/remit/access` | Standard modules: the people and organisations a business deals with, the goods and services it handles, and the members and roles that sign in and act. |
| `src/remit/ledger` | General ledger: accounts, journal entries, reversals, period closes. Built only on the kernel's public API. `books.py` is the bridge to pgledger, `rules.sql` holds what the database enforces, `vendor/pgledger` is pgledger v0.9.0 itself, unmodified. |
| `src/remit/server` | The REST API, token verification, the worker, and the `remit` command. A client of the kernel, built on public APIs only. |
| `Dockerfile`, `compose.yaml`, `deploy` | The container image, everything running on one machine, and the local Keycloak's realm. |
| `tests` | Kernel behaviour against a toy module; ledger examples and property tests; direct attacks on the database and races between commands; the API driven with signed tokens. |
| `checks` | The mutation checks behind `./check --deep`. |
| `docs` | Design notes: [modules and templates](docs/modules-and-templates.md), [personal data](docs/personal-data.md). |
| `examples` | Five fictional companies built on Remit, used as learning material and as the test of its generality. |

Three layering rules are enforced by a test: the kernel imports no module, a module
imports only `remit.kernel` and the top-level packages of the modules it requires, and
the server imports only those public APIs.

## Roadmap

1. **The outside surface.** Built: the REST API generated from the registry, sign-in
   through OpenID Connect, audit by document, the changes feed, the worker and the
   container image. Next: an MCP server generated from the registry in the same way, and
   recording who instructed an agent.
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
