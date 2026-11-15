-- The rules the database enforces on the kernel's tables. Idempotent: applied on every
-- install.
--
-- The kernel already refuses to change a final document or rewrite history. These
-- triggers make the database refuse too, so those rules hold even against a kernel bug
-- or someone connecting directly.

CREATE OR REPLACE FUNCTION remit_protect_final() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.final THEN
        RAISE EXCEPTION '% % is final and cannot change', OLD.kind, OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'documents are never deleted (% %)', OLD.kind, OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION '% % must move from version % to %', OLD.kind, OLD.id,
            OLD.version, OLD.version + 1
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER documents_protect_final
    BEFORE UPDATE OR DELETE ON documents
    FOR EACH ROW EXECUTE FUNCTION remit_protect_final();

CREATE OR REPLACE FUNCTION remit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END $$;

CREATE OR REPLACE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION remit_append_only();

CREATE OR REPLACE TRIGGER audit_append_only
    BEFORE UPDATE OR DELETE ON audit
    FOR EACH ROW EXECUTE FUNCTION remit_append_only();

CREATE OR REPLACE FUNCTION remit_never_truncated() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is never truncated', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END $$;

-- Row-level triggers do not fire on TRUNCATE, which empties a table in one statement.
-- Modules call this on their own tables of record too.
CREATE OR REPLACE FUNCTION remit_forbid_truncate(target regclass) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format(
        'CREATE OR REPLACE TRIGGER never_truncated BEFORE TRUNCATE ON %s '
        'FOR EACH STATEMENT EXECUTE FUNCTION remit_never_truncated()',
        target
    );
END $$;

SELECT remit_forbid_truncate(target) FROM unnest(ARRAY[
    'migrations', 'installed_modules', 'documents', 'events', 'audit',
    'data_keys', 'data_key_subjects', 'cursors', 'idempotency'
]::regclass[]) AS target;
