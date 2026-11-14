# Riverbank Food Trust

A charity that collects surplus food from supermarkets and distributes it through
community kitchens and food banks.

## The business

Four staff and around 120 volunteers. Annual income of about £600,000:

- individual donations, many of them monthly
- grants from trusts and the local council
- Gift Aid reclaimed from HMRC on donations from UK taxpayers
- a contract with the council to supply school holiday meals

It also receives donated food, which it gives away and does not sell.

## How money moves

- **Funds.** Money given for a purpose may only be spent on that purpose, and the trust
  must be able to show it was. A grant for the holiday meals programme is a restricted
  fund; general donations are unrestricted. Every transaction belongs to a fund, and
  each fund has its own balance.
- **Grant income.** It is recognised when the trust is entitled to it. A three-year grant
  paid in yearly instalments, with conditions, is not all income in year one.
- **Gift Aid.** For each eligible donation, the trust reclaims 25p per £1 from HMRC. That
  needs a valid declaration from the donor, and a claim submitted to HMRC in its format.
- **Donated food.** It is recorded at a fair value when received and when distributed, so
  the accounts show the scale of the work, though no money changes hands. It has use-by
  dates.
- **Accounts.** They follow the charity format (the Charities SORP), showing income and
  spending by fund rather than profit.

## Who does the work

| Role | Held by | Within limits |
|---|---|---|
| Treasurer | a trustee | approves spending over £1,000, approves Gift Aid claims, closes the month |
| Operations manager | a person | runs collections and distribution, orders supplies |
| Fundraiser | a person | manages donors and grant applications |
| Gift Aid | an agent | checks declarations, prepares the claim, chases missing declarations. Cannot submit to HMRC; the treasurer does |
| Grant reporting | an agent | drafts each funder's report from the fund's transactions and the distribution records |

## A month

1. 340 monthly donors give through direct debit; 85 one-off gifts arrive online.
2. The Gift Aid agent matches donations to declarations, finds 12 donors without one,
   and drafts a request to each.
3. The council's second grant instalment of £40,000 arrives, restricted to the holiday
   meals programme.
4. 18 tonnes of food are collected from supermarkets, recorded by type, weight, value
   and use-by date, and distributed to 22 partner kitchens.
5. The operations manager buys a refrigerated van for £28,000, split between the
   restricted capital grant that paid for it and unrestricted funds. The treasurer
   approves.
6. A purchase charged to the holiday meals fund turns out not to be for that programme;
   it is refused at posting.
7. At month end, the Gift Aid agent prepares the claim for the treasurer to approve and
   submit, the grant reporting agent drafts the council's quarterly report, and the
   treasurer sees income and spending by fund.

## Built from

- **Templates:** `uk`, `charity`
- **Standard modules:** ledger, parties, items, tax, banking, purchasing, inventory
- **Custom modules:**
  - `giving`: donors, regular gifts, Gift Aid declarations and claims
  - `grants`: grant agreements, instalments and conditions, restricted funds and what
    each may pay for, funder reports

## What it tests

A ledger dimension that matters more than the account (the fund). A rule that refuses a
posting because of what it is for, not what it is. Stock that is given away, not sold.
Reporting in a regulated format that is not profit and loss. A business with no sales
module at all.
