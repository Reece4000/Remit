# Orrin Cycles

Builds steel touring bicycles to order in a workshop, sold direct and through a few
dealers.

## The business

Fifteen staff, about 40 bicycles a month. A customer chooses a frame size, a groupset,
wheels and finish; each combination is a different set of parts. Frames are made in the
workshop from tubing; everything else is bought in. Dealers buy at a discount on 60-day
terms. Direct customers pay a deposit on order and the balance before collection.

## How money moves

- Parts in stock are an asset. When a bike is built, the cost of its parts and the
  labour to build it move into the bike. When it is sold, that cost becomes cost of
  goods sold.
- Parts are valued at average cost, so a price rise from a supplier changes what the
  next bike costs, not the last one.
- Labour is recorded per build, so the real cost of each bike can be compared with its
  price.
- Bikes carry a five-year frame warranty. Repairs under warranty are a cost, not a
  sale.

## Who does the work

| Role | Held by | Within limits |
|---|---|---|
| Owner | a person | sets prices, approves purchases over £5,000, writes off scrap |
| Workshop lead | a person | schedules builds, records labour and scrap |
| Sales | a person | takes orders, manages dealers |
| Purchasing | an agent | reorders parts from the build schedule and stock levels, within a monthly budget. Asks the owner before changing supplier |

## A month

1. Thirty-one direct orders and two dealer orders, for 45 bikes, arrive. Each order line
   is a bill of materials worked out from the options chosen.
2. The purchasing agent compares the parts needed with stock and orders the shortfall,
   all within budget except one large tubing order that waits for the owner.
3. A hub supplier raises prices by 8%. The next builds cost more; bikes already built do
   not change.
4. Forty-two bikes are built. Each build consumes parts, records hours, and produces one
   finished bike, linked to its order line.
5. One frame fails inspection and is scrapped; its tubing and hours are written off.
6. Bikes are collected and invoiced. Cost of goods sold for each is its own cost, not an
   average across bikes.
7. A warranty claim comes in from a bike sold two years ago: a replacement frame is
   built, and its cost goes to warranty expense.
8. At month end, the owner sees margin per bike, labour per build against the
   standard, and the value of parts and work in progress.

## Built from

- **Templates:** `uk`, `make-to-order`
- **Standard modules:** ledger, parties, items, pricing, tax, banking, sales,
  purchasing, inventory
- **Custom modules:**
  - `workshop`: options and the bills of materials they produce, builds, labour,
    inspection and scrap

## What it tests

Inventory that changes form: parts consumed, a product produced, cost carried across.
Valuation that is exact per bike. A custom module that adds configuration to sales order
lines and holds a line back until its parts are in stock. A purchasing agent whose
limit is a budget rather than a single amount.
