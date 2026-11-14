-- The ledger's own tables and the rules the database enforces. Idempotent.
--
-- pgledger (vendored, applied as a migration) keeps the books. It guarantees that every
-- movement has two equal sides. What follows adds what it leaves to its caller: which
-- pgledger account belongs to which ledger account, that history cannot be rewritten,
-- and that nothing lands in a closed period.

CREATE TABLE IF NOT EXISTS ledger_books (
    account          uuid NOT NULL,
    currency         text NOT NULL,
    pgledger_account text NOT NULL UNIQUE REFERENCES pgledger_accounts (id),
    PRIMARY KEY (account, currency)
);

-- Account codes are unique through `Account.unique`, which the kernel indexes.
DROP INDEX IF EXISTS ledger_account_code;

-- pgledger only ever inserts into these two tables, so nothing legitimate is blocked.
CREATE OR REPLACE TRIGGER pgledger_transfers_append_only
    BEFORE UPDATE OR DELETE ON pgledger_transfers
    FOR EACH ROW EXECUTE FUNCTION remit_append_only();

CREATE OR REPLACE TRIGGER pgledger_entries_append_only
    BEFORE UPDATE OR DELETE ON pgledger_entries
    FOR EACH ROW EXECUTE FUNCTION remit_append_only();

CREATE OR REPLACE FUNCTION ledger_closed_through() RETURNS date LANGUAGE sql STABLE AS $$
    SELECT max((body ->> 'through')::date) FROM documents WHERE kind = 'ledger.period_close'
$$;

CREATE OR REPLACE FUNCTION ledger_check_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.event_at AT TIME ZONE 'UTC')::date <= ledger_closed_through() THEN
        RAISE EXCEPTION 'transfer % is dated in a period closed through %',
            NEW.id, ledger_closed_through()
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER ledger_transfer
    BEFORE INSERT ON pgledger_transfers
    FOR EACH ROW EXECUTE FUNCTION ledger_check_transfer();

-- The journal entry document is the record of what was asked for. It must agree with
-- the rules pgledger and the trigger above apply to the movements it caused.
CREATE OR REPLACE FUNCTION ledger_check_journal_entry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    postings  integer;
    imbalance numeric;
BEGIN
    SELECT count(*), coalesce(sum((posting -> 'base' ->> 'amount')::numeric), 0)
    INTO postings, imbalance
    FROM jsonb_array_elements(NEW.body -> 'postings') AS posting;

    IF postings < 2 THEN
        RAISE EXCEPTION 'journal entry % has fewer than two postings', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF imbalance <> 0 THEN
        RAISE EXCEPTION 'journal entry % does not balance (out by %)', NEW.id, imbalance
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.body ->> 'posting_date')::date <= ledger_closed_through() THEN
        RAISE EXCEPTION 'journal entry % is dated in a period closed through %',
            NEW.id, ledger_closed_through()
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER ledger_journal_entry
    BEFORE INSERT ON documents
    FOR EACH ROW WHEN (NEW.kind = 'ledger.journal_entry')
    EXECUTE FUNCTION ledger_check_journal_entry();
