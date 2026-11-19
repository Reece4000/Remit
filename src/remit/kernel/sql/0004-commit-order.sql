-- Audit entries are read by position, by anything that follows the trail (a feed of
-- changes, a document's history). From this migration on, every unit takes the commit
-- order lock before writing its entry, so entries become visible in the order of their
-- ids, as events already did. This index finds the entries that wrote a document.
CREATE INDEX audit_documents ON audit USING gin (documents jsonb_path_ops);
