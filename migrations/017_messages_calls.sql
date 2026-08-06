-- Texts and calls, read-only from the Mac's own databases.
--
-- Both sources are TCC-protected and reached through helpers/tccread, a
-- compiled binary that holds Full Disk Access. Nothing is ever written back:
-- this is an importer, in one direction, like Calendar and Gmail.

CREATE TABLE messages (
  id           INTEGER PRIMARY KEY,
  -- chat.db's message ROWID, stringified. Unique so a re-import is a no-op
  -- rather than a duplicate — the same posture as idx_events_ext.
  external_id  TEXT NOT NULL UNIQUE,
  handle       TEXT NOT NULL,          -- phone number or Apple ID as stored
  direction    TEXT NOT NULL,          -- in|out
  body         TEXT NOT NULL,
  service      TEXT,                   -- iMessage|SMS
  sent_at      TEXT NOT NULL,          -- ISO 8601 with offset
  -- Nullable: most handles will not match anyone in `people`, and a text from
  -- a number you have never named is still a text worth having.
  person_id    INTEGER REFERENCES people(id) ON DELETE SET NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_messages_sent ON messages(sent_at DESC);
CREATE INDEX idx_messages_handle ON messages(handle, sent_at DESC);

-- External-content FTS5 with the three sync triggers, matching notes_fts.
--
-- Unlike notes, messages are HARD-deleted when they age out, so the delete
-- trigger fires and the index stays honest on its own. Search needs no join
-- back to `messages` to filter tombstones — the subtlety that applies to
-- notes deliberately does not apply here.
CREATE VIRTUAL TABLE messages_fts USING fts5(body, content='messages', content_rowid='id');

CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
  INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TABLE calls (
  id           INTEGER PRIMARY KEY,
  external_id  TEXT NOT NULL UNIQUE,   -- ZCALLRECORD.Z_PK, stringified
  handle       TEXT NOT NULL,
  direction    TEXT NOT NULL,          -- in|out
  -- Whether it was picked up. A missed call is the whole point of this table;
  -- an answered one is context for it.
  answered     INTEGER NOT NULL DEFAULT 0,
  duration_s   INTEGER NOT NULL DEFAULT 0,
  occurred_at  TEXT NOT NULL,          -- ISO 8601, converted from Core Data epoch
  person_id    INTEGER REFERENCES people(id) ON DELETE SET NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_calls_occurred ON calls(occurred_at DESC);
