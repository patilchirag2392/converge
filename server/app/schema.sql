-- Converge — Stage 2 schema.
--
-- We persist the CRDT's *operation log*: one row per insert/delete. For an RGA
-- that is also the materialized state, because nothing is ever discarded (an
-- insert becomes a tombstone at worst, a delete is permanent). See NOTES.md.
--
-- Applied at startup with IF NOT EXISTS rather than through a migration tool.
-- That is fine while the schema only ever grows; the first time a column needs
-- to change, this wants to become real migrations.

CREATE TABLE IF NOT EXISTS documents (
    doc_id     text        PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS operations (
    doc_id           text        NOT NULL REFERENCES documents (doc_id) ON DELETE CASCADE,

    -- The operation's own identity. `client_id`/`counter` name the *character*
    -- the operation is about -- for an insert that's the new character, for a
    -- delete it's the character being tombstoned.
    kind             text        NOT NULL,
    client_id        text        NOT NULL,
    counter          bigint      NOT NULL,

    -- Insert-only: the character this one was inserted after. NULL for deletes.
    parent_client_id text,
    parent_counter   bigint,
    value            text,

    -- Server arrival order. Not used for correctness -- the CRDT is
    -- order-independent -- but it makes psql output readable and gives a free
    -- audit trail.
    seq              bigserial   NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),

    -- Idempotency, in one line: re-persisting an operation we already have is a
    -- no-op, so `ON CONFLICT DO NOTHING` on insert is the entire dedup story.
    -- Two clients deleting the same character generate the identical row, so the
    -- second one costs nothing.
    PRIMARY KEY (doc_id, kind, client_id, counter),

    CONSTRAINT operations_kind_valid
        CHECK (kind IN ('insert', 'delete')),

    -- The shape invariant lives in the database, not only in Python: an insert
    -- must carry a parent and exactly one character; a delete must carry neither.
    CONSTRAINT operations_shape_matches_kind CHECK (
        (kind = 'insert'
            AND parent_client_id IS NOT NULL
            AND parent_counter   IS NOT NULL
            AND value            IS NOT NULL
            AND length(value) = 1)
        OR
        (kind = 'delete'
            AND parent_client_id IS NULL
            AND parent_counter   IS NULL
            AND value            IS NULL)
    )
);

-- The primary key index already covers `WHERE doc_id = $1`, but not the
-- `ORDER BY seq` the load query uses, which would otherwise need a sort.
CREATE INDEX IF NOT EXISTS operations_doc_seq_idx ON operations (doc_id, seq);
