# Modules and templates

Status: dependencies between modules are built, as are the first two standard modules
(parties and items). Extensions, guards, links between lines and templates are designed
here but not yet built.

## The rule

Remit's own modules (sales, purchasing, inventory and the rest) are written exactly as
anyone else's would be. They use only what a third party can use: the kernel's public
API, and the public API of the modules they declare as dependencies. No module gets a
hook, table or import that is not available to every other module.

If a standard module needs something the kernel cannot do, the kernel gains a general
primitive that any module could use, and the standard module uses that. The kernel never
learns what a sales order is.

This makes the standard modules the proof of extensibility. If sales can be built from
outside the kernel, so can any module a customer writes for themselves.

## Modules

A module declares documents, events, commands, queries and subscriptions (as now), plus:

- **Dependencies.** `Module("sales", requires=(parties.module, items.module, ledger.module))`.
  Dependencies are module objects, not names, so a module cannot be declared before
  what it requires, and a cycle cannot be written. Installing a module installs what it
  requires first. A module may react only to its own events and those of modules it
  requires directly, which the registry checks at startup. It imports `remit.kernel`
  and the top-level package of each dependency, and nothing deeper, which
  `tests/test_architecture.py` checks.
- **Ownership.** A module writes only the documents it declares and emits only its own
  events; the kernel refuses anything else. To change another module's documents it
  executes one of that module's commands, which it may do only for its own commands and
  those of modules it requires. So a custom module cannot get round a standard module's
  rules (the ledger's balance, a code that never changes) by writing its documents
  directly. `context.sql` is not yet held to this; see the README. It reads another
  module's documents freely, and asks the queries of the modules it requires.
- **Stable names.** Documents, events, commands and queries are named after their
  classes, and subscriptions after their handlers, prefixed with the module's name.
  Stored documents, permissions and each subscription's place in the event log are kept
  under those names, so renaming one is a migration, not a refactor. The permission to
  read a document or ask a query is `<module>.read.<name>`. A subscription can be given a
  fixed name, `@module.subscribe(Event, name="notify")`, so that its handler can be
  renamed freely.
- **Extensions to other modules' documents.** A module can add fields to a document it
  does not own. For example, a warranty module adds a warranty period to an item. The
  owning module doesn't know about the extension, and the extension is namespaced so two
  modules cannot collide.
- **Guards on other modules' transitions.** A module can refuse a transition it does
  not own, giving the reason in the same words as any other refusal. For example, an
  order cannot be released until a credit check has passed. This is how a custom
  module changes a standard process without editing it.
- **Links between lines.** A purchase order line records which sales order line it was
  raised for, and a packing line records which order line it fulfils. Quantities
  carried forward are checked, and lineage can be queried in both directions.

Standard modules live in this repository under `src/remit/`, beside the ledger. Example
companies' custom modules live under `examples/`, outside the package, the way a
customer's own code would.

## Templates

A template is a starting point for a new business, not a running dependency. It
contains:

- the modules to install, with their settings
- a chart of accounts, tax codes and number series
- roles for people and agents, with permissions and limits
- approval rules and workflows
- document layouts (quotes, invoices, delivery notes)
- optionally, sample data to practise on

A template is data, and applying one means executing commands as a setup actor. So it is
validated, audited, idempotent and can be dry-run like everything else. It creates no
state that an administrator could not have created by hand.

Templates layer. A business might start from `uk` (VAT, a UK chart of accounts and
fiscal year) together with `wholesale-export`. Layers that disagree are refused at apply
time rather than resolved silently.

Once applied, the configuration belongs to the business. Later versions of a template
are offered as a dry-run diff of commands to accept or decline, never applied
automatically.

## Promotion

The examples are where standard modules come from. A capability is written as a custom
module inside one example first. When a second, unrelated example needs it, it is
promoted to a standard module, and both examples switch to it. This keeps standard
modules shaped by more than one business.
