-- The morning brief's one irreducible part.
--
-- Only the *mail* summary is stored. The rest of a brief — what is on the
-- calendar, what reminders are due, what is about to spoil, which report is
-- still waiting on an answer — is recomputed live every time it is asked for,
-- because all of it can be. Storing a whole brief at 7am would mean reading
-- back a 9am standup at four in the afternoon; storing only the part that
-- cannot be recomputed makes that impossible by construction.
--
-- One row per day. `brief_on` is `_on` for the same reason
-- `gratitude_entries.entry_on` is: a day has no time of day. Unlike gratitude
-- there is no cutoff — the job runs at 7am, well clear of midnight, so the
-- local date is unambiguous.
--
-- A missing row is normal and permanent, not pending: the job may have found
-- nothing worth saying, or the machine may have been asleep at 7. `query`
-- omits the BRIEF line and the agenda answers alone, which is why there is
-- nothing to backfill.

CREATE TABLE briefs (
  id           INTEGER PRIMARY KEY,
  brief_on     TEXT NOT NULL UNIQUE,   -- YYYY-MM-DD, local
  mail_summary TEXT,                   -- NULL when nothing was worth saying
  message_count INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
