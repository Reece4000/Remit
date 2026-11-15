-- What installing needs before it can install anything, the kernel's own tables included.
-- Idempotent: applied on every install.

CREATE TABLE IF NOT EXISTS migrations (
    module     text        NOT NULL,
    name       text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (module, name)
);

-- A digest of each migration as it was applied, so that one edited afterwards is
-- refused rather than silently skipped. Null for migrations applied before digests were
-- kept; the next install records the digest of the migration as it then stands.
ALTER TABLE migrations ADD COLUMN IF NOT EXISTS digest text;

CREATE TABLE IF NOT EXISTS installed_modules (
    module       text        PRIMARY KEY,
    fingerprint  text        NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT now()
);
