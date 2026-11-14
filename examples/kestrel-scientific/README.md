# Kestrel Scientific

A UK supplier of laboratory equipment and reagents to universities, hospitals and
research programmes, mostly in East and West Africa.

## The business

Eight staff, turnover around £3m. Most orders are bespoke: a customer sends a list of
requirements, Kestrel finds the products from its suppliers, quotes, and buys them in
once the customer orders. It keeps a small stock of products it sells often, and
whatever is left over from past orders.

Few customers order twice. Many orders are funded by grants, and the funder's
programme number has to appear on every document.

## How money moves

- Customers are quoted and invoiced in USD, EUR or GBP. Suppliers invoice in GBP, EUR,
  USD or CHF. Margin is only known in GBP once both sides are converted.
- Exports are zero-rated for VAT, provided there is proof the goods left the UK. The air
  waybill is that proof.
- Large orders take a deposit before anything is bought.
- Stock is valued at what it cost. Reagents have batch numbers and expiry dates, and
  expired stock is written off.

## The flow of documents

    quotation → sales order → purchase orders → goods received
                                              → packing list → air waybill → invoice

Every line is linked to the line it came from. A packing line knows which order line it
fulfils, and which purchase order line and supplier batch it was bought on. Quantities
can't be packed twice, or shipped without being received.

The **air waybill** is the custom module that matters most. Its fields are filled in as
each stage completes: weights and dimensions at packing, hazard classes from the items,
the airline's reference at booking. Each destination country adds its own requirements,
such as an import permit number or a certificate of origin. The waybill's lifecycle
decides when the shipment may be dispatched, and the invoice may not be raised until it
is.

## Who does the work

| Role | Held by | Within limits |
|---|---|---|
| Director | a person | approves quotes below 15% margin, purchase orders over £10,000 |
| Sales and sourcing | people | prepare quotations, confirm orders |
| Logistics | a person | books freight, signs off waybills |
| Pricing | an agent | loads supplier price files, finds and fills gaps, asks suppliers for prices it does not have. Cannot change a quote that has been sent |
| Export documents | an agent | fills in each waybill from the order, the items and the destination's rules; lists what is missing and whom to ask |

## A month

1. Three suppliers send updated price files as spreadsheets. The pricing agent loads
   them and reports 40 changed prices and 6 discontinued products.
2. A university in Kenya sends a list of 25 items. Sales quotes in USD; two items need
   prices the agent obtains by email.
3. The quote is accepted and becomes a sales order. A 30% deposit is received.
4. Purchase orders go to four suppliers, raised from the order lines, one of them in
   CHF. One exceeds £10,000 and waits for the director's approval.
5. Goods arrive over two weeks; reagent batches and expiry dates are recorded.
6. Everything is packed. The export documents agent fills in the waybill and reports
   that Kenya requires an import permit number the order does not have yet.
7. The permit arrives, the waybill completes, and the shipment is dispatched. The
   invoice is raised, zero-rated, and the deposit is set against it.
8. At month end, stock past its expiry date is written off, foreign balances are
   revalued, and margin per order is reported in GBP.

## Built from

- **Templates:** `uk`, `wholesale-export`
- **Standard modules:** ledger, parties, items, pricing, tax, banking, sales, purchasing,
  inventory
- **Custom modules:**
  - `airfreight`: air waybills, their lifecycle, and each destination country's
    requirements, held as data
  - `pricefiles`: loading supplier price files and keeping a history of each price

## What it tests

A custom module that adds fields to standard documents and refuses standard transitions
(no dispatch before the waybill is complete) without editing the standard modules.
Lineage from quote to waybill. Rules that vary by country, held as data rather than
code. Four currencies on one order.
