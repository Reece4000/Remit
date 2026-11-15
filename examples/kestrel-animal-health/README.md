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
- dangerous goods classes from the items
- the airline's reference at booking

Each destination country adds its own requirements, such as a veterinary import permit
or a health certificate. The waybill's lifecycle decides when the shipment may be
dispatched, and the invoice may not be raised until it is.

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

- **Templates:** `uk`, `wholesale-export`
- **Standard modules:** ledger, parties, items, pricing, tax, banking, sales, purchasing,
  inventory
- **Custom modules:**
  - `airfreight`: air waybills, their lifecycle, and each destination country's
    requirements, held as data
  - `pricelists`: loading manufacturers' price lists and keeping a history of each price

## What it tests

A custom module that adds fields to standard documents and refuses standard transitions
(no dispatch before the waybill is complete) without editing the standard modules.
Lineage from quote to waybill. Rules that vary by country, held as data rather than
code. Four currencies on one order.
