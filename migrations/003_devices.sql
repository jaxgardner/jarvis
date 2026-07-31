-- Registered devices, per-device bearer tokens, APNs.
--
-- Two jobs in one table:
--
--   1. Where to send a push. `apns_token` is the device token APNs hands the
--      app; it changes on reinstall, restore-from-backup, and occasionally for
--      no reason at all, so the app re-registers on every launch.
--
--   2. Who is calling. Until now there was one shared JARVIS_TOKEN, which
--      means a lost phone can only be locked out by re-keying every client.
--      Each device gets its own bearer token so revocation is one row.
--
-- The bearer token itself is NEVER stored — only sha256 of it. The plaintext
-- is returned exactly once, at registration, and lives in the iOS Keychain
-- from then on. A stolen copy of jarvis.db must not be a stolen credential.

CREATE TABLE devices (
  id           INTEGER PRIMARY KEY,
  label        TEXT NOT NULL,                   -- 'Jaxon iPhone'
  platform     TEXT NOT NULL DEFAULT 'ios',     -- ios|macos|shortcut
  token_hash   TEXT NOT NULL UNIQUE,            -- sha256 hex of the bearer token
  apns_token   TEXT,                            -- hex device token; NULL until push is granted
  apns_env     TEXT NOT NULL DEFAULT 'prod',    -- prod|sandbox — different APNs hosts
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_seen_at TEXT,
  revoked_at   TEXT
);

-- Partial, like idx_events_ext: one live registration per physical device,
-- while still allowing many NULL apns_token rows (a device that has not been
-- granted push permission yet) and many revoked rows for the same token
-- (reinstalls accumulate).
CREATE UNIQUE INDEX idx_devices_apns ON devices(apns_token)
  WHERE apns_token IS NOT NULL AND revoked_at IS NULL;

CREATE INDEX idx_devices_live ON devices(token_hash) WHERE revoked_at IS NULL;

-- reminders.status finally uses 'acked' — declared in 001, described in 002,
-- and until now never set by anything. It is what the notification's Done
-- button writes: delivered AND dealt with, as distinct from merely 'fired'.
--
-- Full set: pending | firing | fired | missed | acked | cancelled
