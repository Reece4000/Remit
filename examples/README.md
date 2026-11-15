# Example companies

Five fictional businesses, chosen to be as unlike each other as possible, each built on
Remit the way a real customer would build. They are learning material, and they are
also the test of whether Remit is genuinely general.

Status: profiles only. The code arrives as the modules it needs are built (see
[docs/modules-and-templates.md](../docs/modules-and-templates.md)).

The companies are invented. Some are modelled on kinds of business the authors have
worked in, but none describes a real one, and every name, figure and detail is made up.

| Company | Business model | Why it is here |
|---|---|---|
| [Fernhill Analytics](fernhill-analytics/) | B2B software sold by subscription | Revenue earned over time, not when invoiced; billing that runs itself |
| [Kestrel Animal Health](kestrel-animal-health/) | Exports veterinary diagnostics, buying each order in | Documents that flow line by line from quote to air waybill; four currencies |
| [Orrin Cycles](orrin-cycles/) | Builds bicycles to order | Turning parts into products; what each bike really cost |
| [Stallholder](stallholder/) | Online marketplace for independent makers | Most of the money it handles belongs to someone else |
| [Riverbank Food Trust](riverbank-food-trust/) | Charity | Money that may only be spent on what it was given for; no profit |

## How each one is built

Each company starts from templates, installs standard modules, and writes custom modules
only for what is particular to it. Its custom code lives in its own folder, outside the
`remit` package, as a customer's would.

| | Fernhill | Kestrel | Orrin | Stallholder | Riverbank |
|---|---|---|---|---|---|
| Templates | `uk`, `subscription-software` | `uk`, `wholesale-export` | `uk`, `make-to-order` | `uk`, `marketplace` | `uk`, `charity` |
| ledger | ● | ● | ● | ● | ● |
| parties | ● | ● | ● | ● | ● |
| items | ● | ● | ● | ● | ● |
| pricing | ● | ● | ● | | |
| tax | ● | ● | ● | ● | ● |
| banking | ● | ● | ● | ● | ● |
| sales | ● | ● | ● | ● | |
| purchasing | | ● | ● | | ● |
| inventory | | ● | ● | | ● |
| Custom modules | `subscriptions`, `metering` | `airfreight`, `pricelists` | `workshop` | `marketplace` | `giving`, `grants` |

Custom modules that a second company turns out to need are promoted to standard ones.
`subscriptions` is the first candidate: Fernhill bills plans monthly, and Riverbank takes
monthly gifts from regular donors.

## What the framework must provide

Each ● is something that company cannot be built without. This is the order of work.

| Capability | Fernhill | Kestrel | Orrin | Stallholder | Riverbank |
|---|---|---|---|---|---|
| Modules that depend on other modules | ● | ● | ● | ● | ● |
| Adding fields to another module's documents | | ● | ● | ● | ● |
| Refusing another module's transitions | | ● | ● | ● | ● |
| Links between the lines of related documents | | ● | ● | ● | |
| Things that happen on a schedule | ● | | | ● | ● |
| Ledger dimensions (fund, product line) | ● | | | | ● |
| Revenue recognised over time | ● | | | | ● |
| Stock valuation | | ● | ● | | ● |
| Lots and expiry dates | | ● | | | ● |
| Foreign-currency revaluation | ● | ● | | | |
| Money held for others | | | | ● | |
| Data arriving from outside (API, files, webhooks) | ● | ● | | ● | ● |
| Documents rendered from templates | ● | ● | ● | ● | ● |
| Approvals and limits | ● | ● | ● | ● | ● |
| Agents holding roles | ● | ● | ● | ● | ● |
| Reports beyond the ledger's own | ● | ● | ● | ● | ● |

## Running an example

Not yet possible. Each example will have a `scenario.py` that applies its templates to an
empty database, runs one month of the business described in its profile, and prints what
an owner would want to see at month end. The scenarios run as part of `./check`, so an
example that stops working fails the gate.
