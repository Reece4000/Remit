-- Idempotency keys belong to the actor who sent them. They were stored joined to the
-- actor's id by a colon, so actor "ops" with key "bot:1" and actor "ops:bot" with key "1"
-- shared one record. A joined key cannot be split reliably (actor ids contain colons
-- themselves), so records kept before this migration are dropped: a command retried
-- across the upgrade runs again rather than replaying.
DROP TABLE idempotency;

CREATE TABLE idempotency (
    actor  text  NOT NULL,
    key    text  NOT NULL,
    record jsonb NOT NULL,
    PRIMARY KEY (actor, key)
);
