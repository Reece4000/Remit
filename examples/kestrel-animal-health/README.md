# Kestrel Animal Health

A UK exporter of veterinary diagnostic kits, vaccines and laboratory equipment to
government veterinary services and animal health programmes in South and South-East
Asia.

## The business

Eleven staff, turnover around £4m. Most orders are tenders: a customer publishes a list
of requirements, Kestrel sources the products from manufacturers, quotes, and buys them
in once it wins the order. It holds stock only of a few fast-moving consumables.

Many orders are paid for by development programmes, and the programme's reference has
to appear on every document.

## How money moves

- Customers are quoted and invoiced in USD or EUR. Manufacturers invoice in GBP, EUR,
  USD or DKK. Margin is only known in GBP once both sides are converted.
- Exports are zero-rated for VAT, provided there is proof the goods left the UK. The air
  waybill is that proof.
- Tenders usually require a performance bond, and large orders take a deposit before
  anything is bought.
- Stock is valued at what it cost. Vaccines and test kits have batch numbers and expiry
  dates, and some must travel chilled. Expired stock is written off.

## The flow of documents

    quotation → sales order → purchase orders → goods received
                                              → packing list → air waybill → invoice

Every line is linked to the line it came from. A packing line knows which order line it
fulfils, and which purchase order line and manufacturer batch it was bought on.
Quantities can't be packed twice, or shipped without being received.

The **air waybill** is the custom module that matters most. Its fields are filled in as
each stage completes:

- weights, dimensions and temperature range at packing
- dangerous goods, worked out from the items packed and the dry ice
- the airline's reference at booking

Each destination country adds its own requirements, such as a veterinary import permit,
and the goods add theirs wherever they go: a health certificate for vaccines, an export
licence for kits for controlled pathogens. The waybill's lifecycle decides when the
shipment may be dispatched, and the invoice may not be raised until it is.

## Who does the work

| Role | Held by | Within limits |
|---|---|---|
| Managing director | a person | approves quotes below 12% margin, purchase orders over £15,000 |
| Tenders | people | prepare quotations, confirm orders |
| Logistics | a person | books freight, signs off waybills |
| Sourcing | an agent | loads manufacturers' price lists, finds and fills gaps, asks manufacturers for prices it does not have. Cannot change a quote that has been sent |
| Export documents | an agent | fills in each waybill from the order, the items and the destination's rules; lists what is missing and whom to ask |

## A month

1. Two manufacturers send updated price lists. The sourcing agent loads them and
   reports 25 changed prices and 4 discontinued products.
2. A national veterinary laboratory publishes a tender for 18 items. Tenders quotes in
   USD; one item needs a price the agent obtains by email.
3. The tender is won and becomes a sales order. A 30% deposit is received.
4. Purchase orders go to three manufacturers, raised from the order lines, one of them in
   DKK. One exceeds £15,000 and waits for the managing director's approval.
5. Goods arrive over two weeks; vaccine batches, expiry dates and storage temperatures
   are recorded.
6. Everything is packed. The export documents agent fills in the waybill and reports
   that the destination requires a veterinary import permit the order does not have yet.
7. The permit arrives, the waybill completes, and the shipment is dispatched. The
   invoice is raised, zero-rated, and the deposit is set against it.
8. At month end, stock past its expiry date is written off, foreign balances are
   revalued, and margin per order is reported in GBP.

## Built from

The aim:

- **Templates:** `uk`, `wholesale-export`
- **Standard modules:** ledger, parties, items, pricing, tax, banking, sales, purchasing,
  inventory
- **Custom modules:**
  - `airfreight`: air waybills, their lifecycle, and each destination country's
    requirements, held as data
  - `pricelists`: loading manufacturers' price lists and keeping a history of each price

Today, Remit's standard modules are ledger, parties, items and access, and there are no
templates. Kestrel writes the rest in [`kestrel/`](kestrel), as any business would, on
the kernel's public API and the standard modules' public APIs. They become standard
modules once a second, unrelated example needs them.

| Module | What it holds |
|---|---|
| `fx` | exchange rates into GBP, by day |
| `pricelists` | each manufacturer's price and lead time for each item, superseded rather than changed; who an item is bought from by default |
| `stock` | batches with their paperwork, expiry, storage and bin, valued at cost; allocation, issue, write-off; how each item is kept |
| `sales` | quotations and their lines, approval below the margin floor or over the credit limit, orders, deposits; how each item is sold; customers' terms |
| `purchasing` | purchase orders raised from order lines, approval over the limit, confirmation, goods received; how each item is bought; manufacturers' terms and approval |
| `airfreight` | packing, the waybill, the documents each destination and each kind of goods requires, booking, dispatch; what customs and the airline are told of each item |
| `invoicing` | zero-rated invoices once dispatched, the deposit set against them, payment; the account each item's sales are recorded in |

## The documents

Each document carries what an ERP's would. Totals, margins, outstanding quantities and
values are worked out by the module and stored, never typed in. Every field is
described, and the descriptions reach clients through the registry.

| Document | Fields | Amended by | Refused once |
|---|---|---|---|
| Quotation | 30 | `sales.amend_quotation` | approved (reopen it, and have it approved again) or sent; valid until before its date |
| Quotation line | 17 | `sales.amend_quotation_line` | the same. A change of manufacturer re-costs the line |
| Sales order | 33 | `sales.amend_sales_order` | closed or cancelled; freight and insurance once anything is invoiced |
| Sales order line | 19 | `sales.amend_sales_order_line` | closed or cancelled |
| Purchase order | 20 | `purchasing.amend_purchase_order` | sent: what was agreed (Incoterm, terms, freight) is fixed; received: everything |
| Purchase order line | 15 | `purchasing.amend_purchase_order_line` | sent: what it orders is fixed; never more than was sold |
| Goods receipt | 15 | (a record; never changes) | |
| Batch | 15 | `stock.amend_batch` | used up or written off; an expiry before manufacture |
| Waybill | 26 | `airfreight.fill_waybill` | booked. Booking refuses an AWB number with the wrong check digit |
| Packing line | 12 | `airfreight.amend_packing_line` | packing closed |
| Invoice | 27 | (issued once; then paid) | |
| Invoice line | 14 | (issued with the invoice) | |

Counts leave out the `id`, `version` and `state` every document has. Amendments use the
kernel's `amends`: a client sends every field as it holds it, at the version it read,
and the module refuses anything the business would.

Amounts are kept in the document's currency, with what they come to in GBP where the
business uses it: cost and margin on quotations and orders, a purchase order's worth
(`total_base`, which the approval limit is held to) and an invoice's value in the books
(`total_base`, at the invoice's exchange rate).

## Master data

Remit's items module knows an item's code, kind, name and unit, and its parties module a
party's code, kind, name and tax number. Everything else Kestrel keeps about them is
added by the Kestrel module whose work needs it, as an [extension](../../docs/extensions.md):
its own fields on the item or party, listed, filtered and described with the item's or
party's own. The items listing has 48 fields, and the parties listing 26.

| On | Extension | Fields | Kept by | What it says |
|---|---|---|---|---|
| item | `stock.item_stock` | 15 | sourcing agent | storage, temperature limits, do not freeze, light sensitive, shelf life and the least a batch must have left, whether batches need an expiry and a certificate of analysis, bin, weights and dimensions |
| item | `pricelists.item_sourcing` | 1 | sourcing agent | the manufacturer it is bought from by default |
| item | `purchasing.item_purchasing` | 4 | sourcing agent | the manufacturer's unit and pack, its minimum order, blocked for purchase |
| item | `airfreight.item_shipping` | 13 | export documents agent | commodity code, country of origin, customs description, UN number, class, packing group and proper shipping name, biological substance, dry ice, temperature logger, health certificate, export licence, packaging |
| item | `sales.item_sales` | 9 | sourcing agent | product group, species, disease, list price, veterinary only, sales multiple, blocked for sale, tender eligible, registration number |
| item | `invoicing.item_invoicing` | 1 | managing director | the income account its sales are recorded in |
| party | `sales.customer_terms` | 12 | Tenders | customer type, currency, payment terms, credit limit, Incoterm and place, airport, salesperson, import permit and its expiry, documents asked for, Kestrel's vendor number |
| party | `purchasing.supplier_terms` | 8 | Tenders | currency, payment terms, Incoterm and place, lead time, minimum order value, account number, ordering email |
| party | `purchasing.supplier_approval` | 2 | managing director | approved until, and the qualification it rests on |

Values are choices, flags, whole numbers, decimals, money, dates, references to parties
and lists of choices (species, documents asked for). Each extension is amended by one
command, `stock.amend_item_stock` and so on, sent with every field and the version
read, and each refuses what contradicts itself: a minimum temperature above the
maximum; a minimum shelf life longer than the shelf life, or without one; a gross weight
under the net; frozen storage for what must not freeze; some dimensions without the
rest; a class, packing group or shipping name without a UN number, or a UN number
without its class and name; UN3373 or UN1845 given as the item's UN number rather than
as biological substance or dry ice; a commodity code that is not 6 to 10 digits; a
default manufacturer with no current price; a minimum order that is not whole packs; a
list price outside GBP; a species named twice; a revenue account that is not an active
income account; a credit limit or minimum order value in another currency than the
party's; a named place without its Incoterm; a permit expiry without the permit; an
approval without its qualification.

What is kept changes the work:

- **Receiving.** A batch of an item tracked by expiry, or needing a certificate of
  analysis, is refused without it. It is stored and put away as the item is unless the
  receipt says otherwise.
- **Packing.** A batch with less life left than the item's minimum is not allocated.
  Goods that must not freeze are not packed with goods flown frozen. Packing cannot
  close on a temperature range that would not keep every item within its limits, or
  without the dry ice and temperature logger the goods need.
- **The waybill's documents.** Its dangerous goods are worked out from the items packed:
  each UN number with its class and packing group, UN3373 for a biological substance and
  UN1845 for the dry ice. Requirements can apply to every destination when the goods
  need them, so a shipment of vaccines needs a veterinary health certificate, kits for
  controlled pathogens an export licence, and dangerous goods a shipper's declaration.
- **Dispatch.** Refused once the customer's import permit has lapsed, until its renewal
  is recorded.
- **Quoting.** A quotation takes the customer's currency, payment terms, Incoterm,
  place and salesperson unless it says otherwise, and a line its item's list price at
  the day's rate and its default manufacturer. An item blocked for sale is refused; so
  is a veterinary medicine for a customer not authorised to receive one, an item that
  is not tender eligible on a quotation answering a tender, and a quantity that is not a
  whole multiple of the item's sales multiple. A quotation on credit over the
  customer's limit must be approved, as one below the margin floor must.
- **Buying.** A purchase order goes only to a manufacturer whose approval is current,
  on its currency, terms, Incoterm and place and expected by its lead time. A line buys
  what was sold, rounded up to the minimum order and whole packs; an item blocked for
  purchase is refused.
- **Invoicing.** Each line carries its item's commodity code and country of origin, and
  its sales are recorded in the item's revenue account, with carriage apart.

`airfreight.requirement` stays a document of its own rather than becoming an extension:
it describes a destination's rules, not an item or a party, and it has a lifecycle
(a requirement is retired). What items contribute is a flag on the item, and the
requirement's `condition` says which flag brings it in.

Limits, for now:

- "No invoice before dispatch" is a rule in Kestrel's own invoicing module. It does not
  yet show how a custom module refuses a standard module's transition.
- A deposit is released at the rate of the day it is invoiced. The difference stays on
  the deposits account, because foreign balances are not yet revalued.
- An invoice is paid in full or not at all: there are no part payments. Kestrel never
  pays its manufacturers either, because there are no purchase invoices yet, so what it
  owes them only grows.
- There is no tax module: invoices carry their zero-rated treatment, but nothing posts
  VAT. There are no performance bonds and no credit notes.
- Month end (revaluation, margin per order in GBP) is not built; the history only
  writes off expired stock as each month closes. Margin per quotation and order can be
  asked for: `sales.quotation_margin`, `sales.order_margin`.
- Some master data is kept for people to read and nothing else checks it: light
  sensitivity, packaging, weights and dimensions, customs description, registration
  number, the documents a customer asks for, its vendor number, a manufacturer's
  account number, ordering email and minimum order value. A purchase order under the
  minimum order value is not refused.
- A credit limit is held to the one quotation, not to what the customer already owes.
  An import permit is checked at dispatch only, not when quoting.
- A receipt's temperature is recorded, not held to the item's limits.
- Changing master data does not reach documents already made: a quotation keeps the
  terms it was opened on, a batch the bin it was put away in.
- Every company, person and airline is invented. Airline prefixes (917, 921, 934, 948)
  are made up, though the AWB check digit follows the real rule. Phone numbers are from
  Ofcom's ranges for drama.

## Running it

From the repository's root, with Docker:

    docker compose -p remit down    # it uses the same ports
    docker compose -f compose.yaml -f examples/kestrel-animal-health/compose.yaml up --build -d
    docker compose -f compose.yaml -f examples/kestrel-animal-health/compose.yaml \
        run --rm --entrypoint python api -m kestrel.seed

The seed sets Kestrel up on the first of the month a year ago, makes a year of trading
up to the end of last month (`kestrel.history`), then plays the first weeks of this one
(`kestrel.month`). It takes a minute or two and runs once; to start again, add `-v` to
`down` to remove the database. Sign in to a client against
`http://localhost:8080/realms/kestrel` as:

| User | Password | Role |
|---|---|---|
| `morgan` | `morgan` | Managing director: everything |
| `priya` | `priya` | Tenders: quotations, orders, deposits, purchase orders |
| `tom` | `tom` | Logistics: goods received, airfreight, write-offs, invoices |

The two agents sign in with client credentials: `kestrel-sourcing` and
`kestrel-export-documents`, with secrets `local-sourcing-secret` and
`local-export-documents-secret`. These are local passwords for a local realm; nothing
here is fit to deploy.

### The year

Every change in the year is a command, run as the person or agent who would make it, on
a clock the history moves forward, so documents and the audit trail carry the dates
they would have had. Choices come from one seeded random generator: the same dates make
the same year, and `tests/test_kestrel_history.py` holds it to that. Roughly:

- 402 items from 30 manufacturers, priced in GBP, EUR, USD and DKK, with three rounds of
  new price lists and a few products discontinued mid-year, then blocked for sale and
  purchase. Each is described as its family makes plausible: vaccines chilled at 2 to
  8 °C, light sensitive, sold and bought in boxes of ten, needing a health certificate;
  PCR kits and some reagents frozen and flown on dry ice; rapid tests kept at 2 to
  30 °C; solvents and acids with their UN numbers; equipment heavy, crated and measured
- 80 customers in 15 countries, each country's import requirements held as data. Each
  customer has a type, currency, terms, airport and import permit, and a credit limit
  if it trades on credit; each manufacturer its terms and an approval. Approvals are
  renewed before they lapse, and permits around when they do, some a few days late
- 220 quotations, of which about 90 are won and 75 lost, with the rest withdrawn or
  still awaiting an answer; some approved below the margin floor, reopened and approved
  again
- about 90 sales orders, a few cancelled; deposits on programme orders
- about 110 purchase orders, since each order is bought from its own manufacturers and a
  programme's order needs a purchase order of its own; a few over the limit approved,
  confirmed, delayed or cancelled
- about 400 goods receipts and batches, some delivered in two parts; expired stock
  written off at each month's end
- about 95 waybills, packed, booked, documented and dispatched, each invoiced; an order
  whose goods travel at different temperatures goes on a waybill for each. Most
  invoices paid, a few never

The last weeks are left as they would be: orders waiting for goods, shipments being
packed, invoices not yet due.

### What the seed leaves to do

The seed prints the codes of this month's documents: the tender and its order, the
purchase order over the limit, the thin quotation, and the purchase order still part
received.

1. As Morgan, approve the purchase order over £15,000, which is in DKK. Priya sends it
   (she cannot approve it).
2. As Morgan, approve the quotation below the 12% floor, so Priya can send it.
3. As Tom, receive what is still due on the two purchase orders, from each line.
   Vaccines and kits need a batch, an expiry, a certificate of analysis, and a
   temperature when chilled; they are put away in the item's bin.
4. As Tom, open a waybill for the order to Vietnam (`VN`); it lands at Hanoi (`HAN`), as
   the customer's terms say. Pack each order line from its batch, fill in the pieces,
   weight, dimensions and temperature range (`COL`), and close the packing. Closing is
   refused until the waybill names the temperature logger the vaccines and kits fly
   with. Book the freight.
5. Completing it is refused until four documents are held: Vietnam's import permit and
   certificate of origin, a veterinary health certificate for the vaccines, and an
   export licence for the African swine fever PCR kits. `airfreight.what_is_missing`
   lists them. Hold all four, complete the waybill and dispatch it.
6. Raise the invoice. It is refused before dispatch; after dispatch it is zero-rated,
   the USD 41,496.75 deposit is set against it, and the order closes.

`tests/test_kestrel.py` plays the same month to the end.

## What it tests

A custom module that adds fields to standard documents and refuses standard transitions
(no dispatch before the waybill is complete) without editing the standard modules.
Master data kept as extensions of the standard items and parties, by the modules whose
work reads it, in one wide listing. Lineage from quote to waybill. Rules that vary by
country, held as data rather than code. Four currencies on one order.
