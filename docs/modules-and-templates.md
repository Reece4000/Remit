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
outside the kernel, so can a customer's air waybill module.

## Modules

A module declares documents, events, commands and subscriptions (as now), plus:

- **Dependencies.** `Module("sales", requires=(parties.module, items.module, ledger.module))`.
  Dependencies are module objects, not names, so a module cannot be declared before
  what it requires, and a cycle cannot be written. Installing a module installs what it
  requires first. A module may react only to its own events and those of modules it
  requires directly, which the registry checks at startup. It imports `remit.kernel`
  and the top-level package of each dependency, and nothing deeper, which
  `tests/test_architecture.py` checks.
- **Extensions to other modules' documents.** A module can add fields to a document it
  does not own. For example, an export module adds a hazard class to an item. The
  owning module doesn't know about the extension, and the extension is namespaced so two
  modules cannot collide.
- **Guards on other modules' transitions.** A module can refuse a transition it does
  not own, giving the reason in the same words as any other refusal. For example, a
  shipment cannot be dispatched until its air waybill is complete. This is how a custom
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
- document layouts (quotes, invoices, waybills)
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
