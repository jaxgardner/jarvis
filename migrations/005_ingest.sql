-- Ingestion state.
--
-- Two tables, both about not trusting the ingester.
--
-- `sync_state` holds the cursor Google gives us (a syncToken for Calendar, a
-- historyId for Gmail) plus a last_run_at, so "the importer stopped working"
-- is a detectable condition rather than something noticed a week later when
-- the agenda has quietly gone stale. Same reasoning as `heartbeats` in 002 —
-- the failure that hurts is the silent one.
--
-- `proposals` is the review queue. Nothing extracted from email reaches
-- `events` without a human accepting it. The risk isn't that extraction is
-- occasionally wrong; it's that one invented dentist appointment teaches you
-- to distrust the agenda, and an agenda you don't trust is decoration.

CREATE TABLE sync_state (
  source       TEXT PRIMARY KEY,          -- 'calendar' | 'gmail'
  token        TEXT,                      -- syncToken / historyId, opaque to us
  last_run_at  TEXT,
  last_ok_at   TEXT,                      -- distinct from last_run_at: a run
                                          -- that errored still stamps the
                                          -- former, and the gap between them
                                          -- is what says "broken since".
  detail       TEXT
);

CREATE TABLE proposals (
  id           INTEGER PRIMARY KEY,
  source       TEXT NOT NULL,             -- 'gmail'
  external_id  TEXT NOT NULL,             -- message id, so re-runs don't duplicate
  kind         TEXT NOT NULL,             -- 'event' | 'reminder'
  payload_json TEXT NOT NULL,             -- the extraction, unvalidated
  summary      TEXT,                      -- one line, for the review list
  confidence   REAL,
  status       TEXT NOT NULL DEFAULT 'pending',   -- pending|accepted|rejected
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  decided_at   TEXT,
  event_id     INTEGER REFERENCES events(id)      -- set on accept
);

-- Partial unique index, same shape as idx_events_ext: one live proposal per
-- source message, while rejected ones accumulate harmlessly. Re-reading an
-- inbox must not re-propose what you already said no to.
CREATE UNIQUE INDEX idx_proposals_ext ON proposals(source, external_id)
  WHERE status != 'rejected';

CREATE INDEX idx_proposals_pending ON proposals(created_at) WHERE status = 'pending';
