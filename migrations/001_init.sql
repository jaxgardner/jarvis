-- Phase 0: full schema, applied up front (cheaper than migrating later).
-- Timestamps are ISO 8601 WITH OFFSET. Never naive local time; never a bare
-- date for something that has a time.

PRAGMA journal_mode = WAL;

-- ── domain ────────────────────────────────────────────────

CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  title       TEXT NOT NULL,
  starts_at   TEXT NOT NULL,
  ends_at     TEXT,
  all_day     INTEGER NOT NULL DEFAULT 0,
  location    TEXT,
  notes       TEXT,
  source      TEXT NOT NULL DEFAULT 'voice',   -- voice|calendar|email|manual
  external_id TEXT,                            -- dedupe key for synced sources
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at  TEXT,
  deleted_at  TEXT
);

CREATE INDEX idx_events_starts ON events(starts_at) WHERE deleted_at IS NULL;

-- Partial unique index: dedupes synced events while still allowing many
-- NULL external_id rows (voice captures).
CREATE UNIQUE INDEX idx_events_ext ON events(source, external_id)
  WHERE external_id IS NOT NULL;

CREATE TABLE reminders (
  id         INTEGER PRIMARY KEY,
  body       TEXT NOT NULL,
  fire_at    TEXT NOT NULL,
  recurrence TEXT,                              -- NULL | 'daily' | 'weekly:MO,WE' | RRULE
  status     TEXT NOT NULL DEFAULT 'pending',   -- pending|firing|fired|acked|cancelled
  fired_at   TEXT,
  event_id   INTEGER REFERENCES events(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_reminders_due ON reminders(fire_at) WHERE status = 'pending';

CREATE TABLE people (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  relationship TEXT,
  birthday     TEXT,
  notes        TEXT,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE projects (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'active',    -- active|paused|done
  notes      TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE notes (
  id         INTEGER PRIMARY KEY,
  body       TEXT NOT NULL,
  tags       TEXT,                              -- JSON array
  project_id INTEGER REFERENCES projects(id),
  person_id  INTEGER REFERENCES people(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  deleted_at TEXT
);

-- ── operational ───────────────────────────────────────────

CREATE TABLE utterances (
  id            INTEGER PRIMARY KEY,
  raw_text      TEXT NOT NULL,
  route         TEXT,                            -- fast|deep
  intent        TEXT,
  response_text TEXT,
  model         TEXT,
  latency_ms    INTEGER,
  client        TEXT,                            -- shortcut|desktop|dashboard
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE mutations (
  id           INTEGER PRIMARY KEY,
  utterance_id INTEGER REFERENCES utterances(id),
  table_name   TEXT NOT NULL,
  row_id       INTEGER NOT NULL,
  op           TEXT NOT NULL,                    -- insert|update|delete
  before_json  TEXT,                             -- NULL on insert
  after_json   TEXT,                             -- NULL on delete
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  undone_at    TEXT
);

CREATE TABLE jobs (
  id           INTEGER PRIMARY KEY,
  utterance_id INTEGER REFERENCES utterances(id),
  prompt       TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed
  result       TEXT,
  error        TEXT,
  attempts     INTEGER NOT NULL DEFAULT 0,
  session_id   TEXT,                             -- Claude Code session, for resume
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  started_at   TEXT,
  finished_at  TEXT
);

CREATE INDEX idx_jobs_queued ON jobs(created_at) WHERE status = 'queued';

-- ── search ────────────────────────────────────────────────
-- External-content FTS5: the index stores no copy of the text, so it needs
-- these three triggers to stay in sync with `notes`.

CREATE VIRTUAL TABLE notes_fts USING fts5(body, content='notes', content_rowid='id');

CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;

CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.id, old.body);
  INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
END;

-- NOTE: notes are soft-deleted (deleted_at), which fires notes_au, not notes_ad —
-- so soft-deleted rows STAY in the FTS index. Search queries must join `notes`
-- and filter `deleted_at IS NULL` rather than trusting the index alone.

-- sqlite-vec (`note_vecs`) is deliberately NOT created here: the vec0 virtual
-- table requires the extension to be loaded at CREATE time, and sqlite-vec is a
-- Phase 3 dependency. It lands in 002_vectors.sql. Extension loading is
-- available on this machine's Python (verified), so this is scheduling, not a
-- capability gap.
