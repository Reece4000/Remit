# Fernhill Analytics

Sells product analytics software to other businesses, by subscription.

## The business

Twelve staff, about 400 customers, roughly £2m in annual recurring revenue. Customers
are in the UK, the EU and the US, and are billed in GBP, EUR or USD.

There are three plans. Starter and Growth are billed monthly by card. Enterprise is
billed annually by invoice, payable in 30 days. Every plan includes a number of tracked
events per month; usage above that is billed in arrears at a rate per thousand.

## How money moves

- An annual Enterprise invoice of £24,000 is not £24,000 of revenue. It is £2,000 of
  revenue in each of the next twelve months; until then, the rest is a liability
  (deferred revenue).
- Overage is measured during the month and invoiced at the start of the next.
- Card payments arrive through a payment provider, net of its fees, two days later.
- EU business customers are invoiced without UK VAT, under the reverse charge. UK
  customers pay VAT at 20%. US customers pay none.
- USD and EUR receivables are revalued at month end, and the difference is an exchange
  gain or loss.

## Who does the work

| Role | Held by | Within limits |
|---|---|---|
| Finance lead | a person | approves credit notes, writes off debts, closes the month |
| Account managers | people | change plans, agree Enterprise terms |
| Collections | an agent | retries failed cards, sends reminders, pauses an account after 21 days unpaid. Cannot write off or cancel; escalates to the finance lead |
| Revenue | an agent | runs billing and revenue recognition, investigates usage that looks wrong, and prepares the month-end pack for the finance lead to close |

## A month

1. On the 1st, monthly plans renew and are charged; last month's overage is invoiced.
2. Fourteen card payments fail. The collections agent retries them; nine succeed.
3. A new Enterprise customer in Germany signs for £30,000 a year, invoiced without VAT.
4. A Growth customer upgrades mid-month and is charged pro rata.
5. Usage arrives from the product continuously; one customer's usage jumps tenfold and
   the revenue agent flags it before it is billed.
6. A US customer pays a USD invoice; the rate has moved since it was raised.
7. A customer unpaid for 21 days is paused by the collections agent, which then asks the
   finance lead whether to write off the debt. It cannot do so itself.
8. At month end, the revenue agent recognises one month of every annual contract,
   revalues foreign balances, and presents recurring revenue, churn and deferred revenue
   for the finance lead to close.

## Built from

- **Templates:** `uk`, `subscription-software`
- **Standard modules:** ledger, parties, items, pricing, tax, banking, sales
- **Custom modules:**
  - `subscriptions`: plans, subscriptions, renewals, pro-rata changes, revenue schedules
  - `metering`: usage received from the product, summed per customer per month

## What it tests

Revenue that is earned on a schedule rather than when invoiced. Work started by the
calendar rather than by a person. A steady stream of data from outside. An agent that
handles money every day under limits it cannot exceed.
