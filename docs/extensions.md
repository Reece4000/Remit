# Extensions

Status: built.

## Why

The standard items module records what an item is: its code, kind, name and unit. It
knows nothing of who an item is bought from, how it must be stored, or what a
regulator calls it, because those belong to purchasing, to a warehouse, or to one
business alone. Yet the people working with items want one table of them, with all of
those columns, filtered and sorted by any of them.

An extension is a module's own fields on a document another module owns, as a table
extension is in Business Central. Purchasing adds a default supplier and a lead time to
the item; a business's own module adds a storage temperature. The items module stays
lean and knows nothing of either, and a screen shows one wide item.

## Declaring

```python
from remit import items, parties

purchasing = Module("purchasing", requires=(items.module, parties.module))


@purchasing.extension
class ItemPurchasing(Extension[items.Item]):
    """What purchasing keeps about an item."""

    searchable = ("supplier_code",)

    supplier_id: Annotated[UUID | None, References("parties.party")] = None
    """Who it is bought from unless an order says otherwise."""
    lead_time_days: Annotated[int, Field(ge=0)] = 0
    supplier_code: SingleLine | None = None
    """What the supplier calls it."""
```

It is registered as `purchasing.item_purchasing`. The registry refuses an extension:

- of a document of a module its own module does not require, or of its own module's
  documents, whose fields belong on the document itself;
- with a field the document has already, that another extension adds to it, or that
  every document has (`id`, `state` and `version`);
- with a field that has no default, or whose default is made afresh each time;
- with a searchable field that does not hold text, or a reference to a document of a
  module it does not require, by the same rules as a document.

An extension adds fields and nothing else. States and transitions stay the owning
module's, so nothing a module adds can take a document somewhere its owner does not
expect.

## Reading and writing

A handler reads an extension by the document's id, or from the document itself:

```python
held = context.extension(ItemPurchasing, command.item_id, version=command.version)
changed = context.update(held, lead_time_days=command.lead_time_days)
```

What it reads is the extension's fields, with the document's `id` and `version`. A
document written before the extension was installed reads as holding its defaults.
`version=` refuses an out-of-date copy, as `context.get` does.

`context.update` and `context.revise` write the extension's fields into the document,
and move the document's version on. Only the module that declares the extension can,
in the same words as writing another module's document; and not once the document is
in a final state. A command that edits an extension in place declares
`amends = "item_id"` and returns the extension, and its other fields are the
extension's own, so the registry describes it as amending the item, in those fields.

## One document

The fields are kept in the document's own stored body, beside the owner's. A document
with extensions is still one document: one version, one history, one audit entry per
change, whichever module made it. An amendment from a copy read before purchasing
changed the lead time is refused, even though it changes only the name.

Each module writes only its own fields. When the items module writes an item, the
kernel keeps every extension's fields as they are stored; when purchasing writes its
extension, it keeps the item's. The owner never needs to know what it is keeping.

## Listing, describing and serving

`registry.extended(items.Item)` is the item with every installed extension's fields
beside its own. It is what is listed, described and served:

- **Listings** filter, order and search an extension's fields as they do the item's,
  through the same `kernel.page(items.Item, where={"lead_time_days": {"gte": 10}})`. A
  document that holds nothing for a field, because it was written before the extension
  was installed, is listed as holding the field's default, on every store.
- **The registry** describes the item with every extension's fields in its schema and
  its listing, each marked `"x-module": "purchasing"` with the module that added it.
  Each module's own entry lists its extensions, what they extend and the fields they
  add.
- **The API** serves the item's routes with that type, named `ExtendedItem` in the
  OpenAPI document, so a generated client is typed with every field.
- **Reading one** through `kernel.get(registry.extended(items.Item), id)` gives every
  field; through `kernel.get(items.Item, id)` only the item's own.

## Installing

Installing an extension on a database that already holds documents migrates nothing:
the documents read and list as holding the extension's defaults until it writes to
them, which is why every field needs one. What listings search for the extended kind
is rebuilt, as for any module whose definition has changed.

## Personal data

An extension can hold personal data only on a document that is already a data subject,
since nothing else is ever erased. Its personal fields must default to holding nothing
(null, empty or `"[erased]"`). When the owning module erases the document, the kernel
returns the extension's personal fields to their defaults in the same unit of work, so
the owner erases fields it has never heard of. See [personal data](personal-data.md).

## Extensions, lines and separate documents

Three ways for one module to record something about another's document:

- **An extension**, when the fields describe the document itself, one value each, and
  belong in the same row on screen: an item's lead time, a customer's credit limit, a
  product's storage temperature. They change the document's version, so the owner and
  the extension see each other's changes as conflicts, which suits fields edited on the
  same screen.
- **A line** (`part_of`), when there are many of something within the document, each
  with its own lifecycle: the lines of a quotation, the batches of a delivery. Lines
  belong to the module that owns them, which may be a different module from the one
  that owns the document.
- **A separate document**, referring to the other by id, when what is recorded has a
  life of its own: a price list valid for a period, a supplier's quotation, a stock
  count. It has its own lifecycle, history and permissions, and changing it does not
  touch the document it refers to.

An extension is the narrowest of the three: it adds columns and nothing else. When
the fields need their own states, their own permissions, or more than one value per
document, they belong in a document of their own.

## A worked example

[Kestrel Animal Health](../examples/kestrel-animal-health/README.md#master-data) keeps
all its master data this way. Six extensions take the item from 5 fields to 48, and
three take the party from 4 to 26, each declared by the Kestrel module whose work reads
it and amended by a command of its own that refuses details that contradict one another.
Stock refuses to allocate a batch with too little life left, purchasing rounds an order
to the manufacturer's packs, airfreight works out a waybill's dangerous goods and
documents from the items packed, and sales refuses to quote an item blocked for sale.

## What this does not cover

- **A kernel without the extending module.** Every kernel on a database should run the
  same modules, as `remit serve` and `remit worker` do from one application. A kernel
  that leaves the extending module out does not know its fields, and writing the
  document from it drops them.
- **Unique fields.** An extension cannot declare fields unique, alone or with the
  document's.
- **Command results.** A command that returns the document returns the owner's fields
  only. Read `registry.extended(kind)` for the rest.
- **Removing an extension.** Uninstalling one leaves its values in the stored bodies,
  and its text in what listings search, until each document is next written.
- **Indexes for defaults.** A filter for a field's default also matches documents that
  hold nothing for the field, which PostgreSQL's index on stored bodies cannot find, so
  that filter is answered without it.
