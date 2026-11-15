-- The kernel's tables, as they stood before the kernel's schema was versioned. Written
-- with IF NOT EXISTS so that it adopts a database installed by an earlier version.
-- Applied once; never edit it. Change the schema with a new migration.

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
