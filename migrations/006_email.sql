-- Email as context.
--
-- This table is an addition to the original ingestion design, which routed
-- Gmail exclusively into `proposals` — a review queue for appointments
-- extracted from mail. That was right as far as it went, because nothing
-- extracted by a model should reach `events` unattended.
--
-- But proposals only answer "should this become a calendar entry?". They do
-- not answer "did the landlord ever email me back?", which is the question
-- that actually makes an assistant feel like it knows your life. So mail lands
-- here too, and `handlers.query` reads it.
--
-- The safety property is kept by what is stored, not by a review step:
--
--   * Metadata and Google's own `snippet` only. No message bodies. A snippet
--     is ~200 characters Google already computed, so there is no extraction
--     step, no model call, and nothing invented — the assistant can quote what
--     arrived but cannot hallucinate an appointment out of it.
--   * Nothing here is a domain row. It is never surfaced as an event or a
--     reminder, so a marketing email cannot pollute the agenda. That path
--     still runs through `proposals` and a human.
--
-- Writes bypass the mutations helper, for the same reason calendar writes do:
-- ingestion is not a user action, there is nothing to regret, and routing a
-- few hundred rows per sync through the log would bury the user's last real
-- action and make /undo useless for the thing it was built for.

CREATE TABLE email_messages (
  id           INTEGER PRIMARY KEY,
  external_id  TEXT NOT NULL UNIQUE,      -- Gmail message id
  thread_id    TEXT,
  sender       TEXT,                      -- the From header, verbatim
  recipient    TEXT,
  subject      TEXT,
  snippet      TEXT,                      -- Google's snippet. NOT the body.
  received_at  TEXT NOT NULL,             -- ISO 8601 UTC, from internalDate
  labels       TEXT,                      -- JSON array of Gmail label ids
  is_unread    INTEGER NOT NULL DEFAULT 0,

  -- When the proposal extractor last looked at this message. Its purpose is
  -- cost: without it, every run re-pays a Haiku call for the same marketing
  -- email that matched the query and yielded nothing. A per-run ceiling caps
  -- the damage; this is what stops the damage recurring.
  examined_at  TEXT,

  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_email_received ON email_messages(received_at DESC);

-- Partial index over exactly the rows the extractor scans for.
CREATE INDEX idx_email_unexamined ON email_messages(received_at)
  WHERE examined_at IS NULL;

-- ── search ────────────────────────────────────────────────
-- External-content FTS5, same shape as notes_fts in 001. Unlike notes, email
-- rows are HARD-deleted when they age out of the retention window, so the
-- delete trigger actually fires here and the index needs no join to stay
-- honest. Searching still goes through email_messages for the other columns.

CREATE VIRTUAL TABLE email_fts USING fts5(
  subject, snippet, sender,
  content='email_messages', content_rowid='id'
);

CREATE TRIGGER email_ai AFTER INSERT ON email_messages BEGIN
  INSERT INTO email_fts(rowid, subject, snippet, sender)
    VALUES (new.id, new.subject, new.snippet, new.sender);
END;

CREATE TRIGGER email_ad AFTER DELETE ON email_messages BEGIN
  INSERT INTO email_fts(email_fts, rowid, subject, snippet, sender)
    VALUES ('delete', old.id, old.subject, old.snippet, old.sender);
END;

CREATE TRIGGER email_au AFTER UPDATE ON email_messages BEGIN
  INSERT INTO email_fts(email_fts, rowid, subject, snippet, sender)
    VALUES ('delete', old.id, old.subject, old.snippet, old.sender);
  INSERT INTO email_fts(rowid, subject, snippet, sender)
    VALUES (new.id, new.subject, new.snippet, new.sender);
END;
