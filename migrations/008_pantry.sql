-- Pantry: what I bought, what is about to spoil, what I need to buy.
--
-- Receipts do not print expiration dates, so a date has to come from
-- somewhere. It comes from a checked-in shelf-life table as a *default*,
-- shown in a review screen the user must pass through. The model's job is
-- reading pixels — `GV WHL MLK 1GAL` into a name and a category. It never
-- sets a date. That is what makes the dates auditable: when milk is
-- consistently wrong you edit one line of a table, and every future gallon
-- is fixed.
--
-- Same posture as `proposals` in 005, for the same reason: one invented
-- expiry teaches you to distrust the whole inventory, and an inventory you
-- don't trust is decoration.

CREATE TABLE receipts (
  id            INTEGER PRIMARY KEY,
  -- Re-uploading the same photo returns the existing receipt rather than a
  -- second one. Also what makes a discarded receipt stay discarded.
  image_sha256  TEXT NOT NULL UNIQUE,
  image_path    TEXT,                   -- outside the repo, beside the db
  store         TEXT,
  purchased_on  TEXT,                   -- YYYY-MM-DD, off the receipt
  total_cents   INTEGER,
  status        TEXT NOT NULL DEFAULT 'extracting',
                                        -- extracting|pending|confirmed|discarded
  extract_error TEXT,                   -- a failed read is visible, not silent
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  confirmed_at  TEXT
);

CREATE INDEX idx_receipts_pending ON receipts(created_at) WHERE status = 'pending';

CREATE TABLE pantry_items (
  id            INTEGER PRIMARY KEY,
  -- CASCADE, not SET NULL: /undo on a receipt confirm hard-deletes the
  -- receipt row, and "I photographed the wrong receipt" has to take the
  -- whole trip with it.
  receipt_id    INTEGER REFERENCES receipts(id) ON DELETE CASCADE,
  raw_text      TEXT,                   -- 'GV WHL MLK 1GAL', verbatim, always kept
  name          TEXT NOT NULL,          -- 'whole milk'
  category      TEXT,                   -- shelflife.py key: 'milk'
  quantity      REAL,
  unit          TEXT,
  location      TEXT NOT NULL DEFAULT 'pantry',   -- fridge|freezer|pantry
  -- A bare date on purpose. An expiration genuinely has no time — the milk
  -- expires *on the 4th* — so rather than invent a fake midnight offset the
  -- column is named `_on`. Convention: `_at` is an instant with offset,
  -- `_on` is a calendar date. NULL means nothing expires it.
  expires_on    TEXT,
  expiry_source TEXT,                   -- 'default' | 'user'
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|active|consumed|discarded
  consumed_at   TEXT,
  notified_on   TEXT,                   -- YYYY-MM-DD the expiry push went out
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- The sweep's exact query: active rows ordered by expiry.
CREATE INDEX idx_pantry_active ON pantry_items(expires_on) WHERE status = 'active';
CREATE INDEX idx_pantry_receipt ON pantry_items(receipt_id);

CREATE TABLE shopping_list (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  reason         TEXT,                  -- 'out' | 'expiring' | 'manual'
  source_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
  status         TEXT NOT NULL DEFAULT 'open',   -- open|purchased|removed
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  resolved_at    TEXT
);

-- Saying "we're out of milk" twice must not make two entries. Partial, so
-- resolved rows accumulate harmlessly and buying milk again can re-add it.
CREATE UNIQUE INDEX idx_shopping_open ON shopping_list(lower(name))
  WHERE status = 'open';
