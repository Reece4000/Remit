# Vendored: pgledger v0.9.0

The double-entry ledger this module posts to. Copied unmodified from
https://github.com/pgr0ss/pgledger at tag `v0.9.0`:

| File | Source in that repository | Licence |
|---|---|---|
| `pgledger.sql` | `pgledger.sql` | MIT, see `LICENSE` |
| `ulid-to-uuid.sql`, `uuid-to-ulid.sql` | `vendor/scoville-pgsql-ulid/` | BSD 3-Clause, see `LICENSE-pgsql-ulid` |

Do not edit these files. To upgrade, add the new version's changes as a further
migration in `remit/ledger/model.py`; pgledger does not ship upgrade scripts, so the
difference between versions has to be written out by hand.
