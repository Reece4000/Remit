# Stallholder

An online marketplace where independent makers sell ceramics, prints and textiles.

## The business

Six staff and about 1,200 sellers. Buyers pay Stallholder; Stallholder keeps a 12%
commission and pays the rest to the seller once the order has been delivered and the
14-day return window has passed. About £5m a year passes through it, of which roughly
£600,000 is its own revenue.

## How money moves

- When a buyer pays £100, Stallholder has received £100, but only £12 is revenue. The
  other £88 belongs to the seller and is a liability until it is paid out.
- Payments arrive through a payment provider, which reports each charge, refund and
  fee, and pays out to Stallholder's bank in batches.
- Sellers are the ones selling, so VAT applies to Stallholder's commission, not to the
  goods. VAT-registered sellers account for their own VAT.
- A refund after a payout cannot be taken back from money already paid out, so it is
  recovered from the seller's next payout. If there is not enough, the seller owes
  Stallholder.
- Payouts happen weekly. Every seller gets a statement showing their sales, commission,
  refunds and what was paid.

## Who does the work

| Role | Held by | Within limits |
|---|---|---|
| Founder | a person | approves the weekly payout run, writes off seller debts |
| Seller support | people | onboard sellers, decide disputes |
| Buyer support | an agent | answers order questions, issues refunds up to £50 where the policy clearly applies; anything else goes to seller support |
| Payouts | an agent | prepares the weekly payout run, checks it against what the payment provider holds, holds back sellers with open disputes |

## A month

1. About 4,000 orders, each with lines from one or more sellers. Each order is split:
   commission to Stallholder, the rest held for each seller.
2. The payment provider reports charges, fees and its own payouts to the bank, through a
   webhook and a daily file. Every one is matched.
3. The buyer support agent issues 60 refunds under £50 and passes 9 larger or unclear
   cases to seller support.
4. Each week the payouts agent proposes a run that pays only for orders past their return
   window, and holds back two sellers with disputes. The founder approves it.
5. A refund arrives after a seller has been paid; it is recovered from the next payout.
6. A seller closes their shop owing £35; the founder writes it off.
7. At month end, the money held for sellers matches the provider balance and the bank,
   and each seller has a statement.

## Built from

- **Templates:** `uk`, `marketplace`
- **Standard modules:** ledger, parties, items, tax, banking, sales
- **Custom modules:**
  - `marketplace`: sellers, commission rates, splitting orders, money held for each
    seller, the return window, payout runs and seller statements

## What it tests

Money held on behalf of others, kept separate from revenue to the penny. A sale that is
mostly someone else's. High volume arriving from a payment provider, matched
automatically. A custom module that refuses a standard transition: no payout for an
order still inside its return window. Two agents with different limits.
