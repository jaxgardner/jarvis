-- Phase 6 fix: accepting a proposal must stay undoable.
--
-- 005 declared `proposals.event_id INTEGER REFERENCES events(id)` with no
-- delete behaviour, which defaults to RESTRICT. Accepting a proposal writes
-- that reference — and accepting goes through the mutations helper precisely
-- so it can be reversed — but /undo reverses an insert with a HARD delete of
-- the events row. The foreign key then refuses it:
--
--     sqlite3.IntegrityError: FOREIGN KEY constraint failed
--
-- So the one ingestion write deliberately made undoable was the one write that
-- could not be undone. Caught by tests/test_proposals.py rather than by a
-- phone at a lock screen, which is the only reason it is a two-line note
-- instead of an incident.
--
-- ON DELETE SET NULL, and the proposal stays marked 'accepted' with a NULL
-- event_id. It does NOT return to the review queue: you already made a
-- decision about this message, and re-offering something you just removed is
-- a loop, not a feature.
--
-- SQLite cannot ALTER a foreign key, so this is the standard rebuild. Nothing
-- references `proposals`, so no pragma juggling is needed — the drop and
-- rename are safe with foreign_keys ON.

CREATE TABLE proposals_new (
  id           INTEGER PRIMARY KEY,
  source       TEXT NOT NULL,
  external_id  TEXT NOT NULL,
  kind         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  summary      TEXT,
  confidence   REAL,
  status       TEXT NOT NULL DEFAULT 'pending',
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  decided_at   TEXT,
  event_id     INTEGER REFERENCES events(id) ON DELETE SET NULL
);

INSERT INTO proposals_new
  (id, source, external_id, kind, payload_json, summary, confidence,
   status, created_at, decided_at, event_id)
SELECT
   id, source, external_id, kind, payload_json, summary, confidence,
   status, created_at, decided_at, event_id
FROM proposals;

DROP TABLE proposals;

ALTER TABLE proposals_new RENAME TO proposals;

-- Recreated verbatim from 005: dropping the table took them with it.
--
-- Note what this index does NOT do, because the comment in 005 overstates it:
-- it keeps one live proposal per source message while letting rejected ones
-- accumulate, but because rejected rows are excluded from the index it would
-- happily allow a message to be re-proposed after rejection. The rule that
-- re-reading an inbox must not re-propose what you already said no to is
-- enforced by ingest.gmail.candidates(), not here.
CREATE UNIQUE INDEX idx_proposals_ext ON proposals(source, external_id)
  WHERE status != 'rejected';

CREATE INDEX idx_proposals_pending ON proposals(created_at) WHERE status = 'pending';
