-- What a listing's search looks in: the folded text of each searchable document, indexed
-- by trigram so that finding a word inside it does not read every row.
--
-- This table is derived from the documents and holds nothing they do not. The kernel
-- writes a document's row whenever it writes the document, and installing rebuilds the
-- rows of any module whose declarations have changed, so it may be emptied and rebuilt
-- at any time. It is not a table of record and nothing protects its rows.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE document_search (
    kind text NOT NULL,
    id   uuid NOT NULL,
    text text NOT NULL,
    PRIMARY KEY (kind, id)
);

-- Without fastupdate, a search inside a serialisable unit locks only the parts of the
-- index it read, so it conflicts with writes of matching documents and not with every
-- write.
CREATE INDEX document_search_text ON document_search
    USING gin (text gin_trgm_ops) WITH (fastupdate = off);
