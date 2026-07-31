-- Phase 2: the scheduler.
--
-- Adds a heartbeat table so "reminders silently stopped firing" is a
-- detectable condition rather than something you notice a week late. This
-- matters more here than in the design doc's baseline, because FileVault
-- means an unattended reboot leaves the machine at the unlock screen with
-- nothing running.

CREATE TABLE heartbeats (
  name        TEXT PRIMARY KEY,        -- 'scheduler', 'selfcheck'
  last_run_at TEXT NOT NULL,
  detail      TEXT
);

-- reminders.status gains 'missed': claimed by the scheduler but deliberately
-- not delivered because it came due while the machine was down and is now too
-- stale to be useful. Distinct from 'fired' so the distinction survives in the
-- record — marking these 'fired' would claim a notification was sent that
-- never was.
--
-- Full set: pending | firing | fired | missed | acked | cancelled
