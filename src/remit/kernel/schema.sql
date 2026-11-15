-- The kernel's tables. Idempotent: safe to apply on every start.
--
-- The kernel already refuses to change a final document or rewrite history. The
-- triggers below make the database refuse too, so those rules hold even against a
-- kernel bug or someone connecting directly.

CREATE TABLE IF NOT EXISTS migrations (
    module     text        NOT NULL,
    name       text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (module, name)
);

CREATE TABLE IF NOT EXISTS installed_modules (
    module       text        PRIMARY KEY,
    fingerprint  text        NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
    kind    text    NOT NULL,
    id      uuid    NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    final   boolean NOT NULL,
    body    jsonb   NOT NULL,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS documents_body ON documents USING gin (body jsonb_path_ops);

CREATE TABLE IF NOT EXISTS events (
    position bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name     text        NOT NULL,
    body     jsonb       NOT NULL,
    at       timestamptz NOT NULL,
    actor    text        NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at        timestamptz NOT NULL,
    actor     jsonb       NOT NULL,
    action    text        NOT NULL,
    input     jsonb       NOT NULL,
    documents jsonb       NOT NULL,
    events    jsonb       NOT NULL,
    cause     bigint
);

ALTER TABLE audit ADD COLUMN IF NOT EXISTS key uuid;

-- Data keys seal personal values in the audit trail and idempotency records. Deleting a
-- key erases what it sealed; these are the only tables rows are deleted from.
CREATE TABLE IF NOT EXISTS data_keys (
    id     uuid  PRIMARY KEY,
    secret bytea NOT NULL
);

CREATE TABLE IF NOT EXISTS data_key_subjects (
    key_id  uuid NOT NULL REFERENCES data_keys (id) ON DELETE CASCADE,
    subject uuid NOT NULL,
    PRIMARY KEY (key_id, subject)
);
CREATE INDEX IF NOT EXISTS data_key_subjects_subject ON data_key_subjects (subject);

CREATE TABLE IF NOT EXISTS cursors (
    subscription text   PRIMARY KEY,
    position     bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key    text  PRIMARY KEY,
    record jsonb NOT NULL
);

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
