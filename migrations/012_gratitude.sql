-- Three things a day, and the evening prompt that asks for them.
--
-- `entry_on` is `_on` and not `_at` for the same reason `pantry_items.
-- expires_on` is: a gratitude day has no time of day. It is also not simply
-- the local date of `created_at` — the day runs to 4am, so a prompt that
-- arrives at 10pm and is answered at 12:30 fills in the day you were actually
-- thinking about. Under a midnight rule that entry opens a new day and leaves
-- the old one looking skipped, which is the streak breaking for doing the
-- thing. `gratitude.entries.day_for` owns the cutoff; nothing else may
-- compute this column.
--
-- No position column. Order is arrival order, `created_at` carries it, and
-- there is no way to reorder three things you said out loud.

CREATE TABLE gratitude_entries (
  id         INTEGER PRIMARY KEY,
  body       TEXT NOT NULL,
  entry_on   TEXT NOT NULL,   -- YYYY-MM-DD, cutoff already applied
  created_at TEXT NOT NULL    -- ISO 8601, UTC
);

CREATE INDEX idx_gratitude_day ON gratitude_entries(entry_on);
