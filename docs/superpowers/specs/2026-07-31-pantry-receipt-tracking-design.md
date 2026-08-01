# Pantry: receipt capture, expiration tracking, recipes

Photograph a grocery receipt; the items land in a review screen with dates you
confirm; the fridge contents stay current through voice; food about to be
wasted pushes the day before; and the deep path suggests what to cook from
what you actually have.

## Why this shape

Three settled decisions drive everything below.

**You are the authority on expiration dates.** Receipts do not print them, so a
date has to come from somewhere. It comes from a checked-in shelf-life table as
a *default*, shown to you in a review screen you must pass through. The model
never sets a date. When milk is consistently wrong you edit one line of a table
rather than a prompt, and the correction applies to every future gallon.

**Consumption is a voice action.** Nothing else works with your hands full in a
kitchen. `consume_item` joins the existing Haiku router; there is no new input
surface to remember.

**Recipes are deep-path work.** A Haiku one-shot inventing meals from a list is
exactly the output that teaches you to ignore the feature. The deep agent reads
real inventory, leads with what expires soonest, and can fetch an actual recipe.

## Layout

A new `pantry/` package, sibling to `ingest/`:

| Module | Job | Depends on |
| :-- | :-- | :-- |
| `pantry/extract.py` | Photo → line items. One forced-tool vision call. | Anthropic |
| `pantry/shelflife.py` | Category → typical shelf life. A checked-in dict. | nothing |
| `pantry/inventory.py` | Add / consume / query, shopping list logic. | sqlite |
| `pantry/expiry.py` | Items expiring tomorrow, not yet announced. | sqlite |

The split exists to serve design principle 3. `inventory.py` and `expiry.py`
have no LLM dependency, so `scheduler/` can import them without breaking the
rule that the scheduler keeps working when the agent does not.

## Data flow

```
 photo ──POST /receipts──▶ receipts row (status=extracting)
                              │
                        extract.py — model reads pixels only
                              ▼
                        pantry_items (status=pending)
                              │  shelflife.py fills expires_on
                              ▼
                     ┌─ iOS review screen ─┐   ← you confirm every date
                     │  edit dates, delete  │
                     └──────────┬───────────┘
                    POST /receipts/{id}/confirm
                              ▼
              items → active, receipt → confirmed
                     ──▶ ONE mutations row on `receipts`

 "we're out of milk" ─▶ router consume_item ─▶ item consumed + shopping_list row
 scheduler tick ──▶ expiry.py ──▶ expiring tomorrow ──▶ APNs + shopping_list
 "what can I make" ─▶ router escalate ─▶ Claude Code ─▶ MCP pantry_inventory()
```

Extraction runs in a background task. Vision on a receipt is 3-6s, far outside
the 2s fast-path budget, so `POST /receipts` returns a receipt id immediately
and the review screen polls — the same contract as `/jobs/{id}`.

`PANTRY_VISION_MODEL` defaults to `claude-haiku-4-5`. Thermal receipt print is
hard OCR and Haiku's accuracy on it is unproven here. The review screen means a
bad extraction costs edits rather than correctness, and moving to Sonnet is an
env change rather than a code path.

## Schema — migration `008_pantry.sql`

```sql
CREATE TABLE receipts (
  id            INTEGER PRIMARY KEY,
  image_sha256  TEXT NOT NULL UNIQUE,   -- re-uploading the same shot returns
                                        -- the existing receipt, not a second one
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

CREATE TABLE pantry_items (
  id            INTEGER PRIMARY KEY,
  receipt_id    INTEGER REFERENCES receipts(id) ON DELETE CASCADE,
  raw_text      TEXT,                   -- 'GV WHL MLK 1GAL', verbatim, always kept
  name          TEXT NOT NULL,          -- 'whole milk'
  category      TEXT,                   -- shelflife.py key: 'milk'
  quantity      REAL,
  unit          TEXT,
  location      TEXT NOT NULL DEFAULT 'pantry',   -- fridge|freezer|pantry
  expires_on    TEXT,                   -- YYYY-MM-DD. NULL = nothing expires it
  expiry_source TEXT,                   -- 'default' | 'user'
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|active|consumed|discarded
  consumed_at   TEXT,
  notified_on   TEXT,                   -- YYYY-MM-DD the expiry push went out
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_pantry_active ON pantry_items(expires_on) WHERE status = 'active';

CREATE TABLE shopping_list (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  reason         TEXT,                  -- 'out' | 'expiring' | 'manual'
  source_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
  status         TEXT NOT NULL DEFAULT 'open',   -- open|purchased|removed
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  resolved_at    TEXT
);

-- Saying "we're out of milk" twice must not make two entries.
CREATE UNIQUE INDEX idx_shopping_open ON shopping_list(lower(name))
  WHERE status = 'open';
```

### `expires_on`, not `expires_at`

CLAUDE.md forbids a bare date "for something that has a time". An expiration
genuinely has no time — the milk expires *on the 4th*. Rather than invent a
fake midnight offset, the column is named `_on` so the distinction is legible
at every call site. Add the convention to CLAUDE.md as part of this work:
`_at` is an instant with offset, `_on` is a calendar date.

### Mutations, and one deliberate asymmetry

`WRITABLE` gains `receipts`, `pantry_items`, and `shopping_list`. How they are
logged differs on purpose:

- **Confirming a receipt logs exactly one mutation**, on the `receipts` row.
  Item rows are written in the same transaction but not individually logged.
  Thirty mutation rows per shopping trip would bury your last real action and
  make `/undo` useless — the same reasoning CLAUDE.md already applies to synced
  writes. `receipt_id` is `ON DELETE CASCADE`, so reversing that single insert
  takes the whole trip with it, which is what "I photographed the wrong
  receipt" means.
- **Consume and shopping-list edits are logged per row.** Those are voice
  actions, and voice is what `/undo` exists for.

### `undo_last` reverses an utterance, not a row

Today `mutations.undo_last` reverses the single most recent mutation. Extend it
to reverse every mutation sharing that row's `utterance_id`, newest first.

"We're out of milk" writes twice — the item update and the list insert — and
undoing half of it is worse than not undoing at all. The change also fixes an
existing gap: `handlers._lookup_or_create` inserts a `people` or `projects` row
alongside the event, so `/undo` on "lunch with Sarah" currently deletes the
event and leaves Sarah behind.

Mutations with a NULL `utterance_id` keep the current one-row behaviour.

### Image storage

`~/Library/Application Support/jarvis/receipts/<sha256>.jpg` — beside the
database, outside the repo, for the same reason the database is. iOS downscales
to 1568px on the long edge before upload; past that the vision API gains
nothing and you only pay tokens. Images are pruned 30 days after confirmation,
their only purpose being to re-read a bad extraction.

## API

All bearer-auth, consistent with the rest of the service.

| Endpoint | Purpose |
| :-- | :-- |
| `POST /receipts` | Upload a photo. Returns `{receipt_id, status}` immediately |
| `GET /receipts/{id}` | Poll extraction; returns items once `status='pending'` |
| `PATCH /receipts/{id}/items` | Batch edit from the review screen |
| `POST /receipts/{id}/confirm` | Items go `active`, receipt `confirmed`. One mutation. Undoable |
| `POST /receipts/{id}/discard` | Throw the whole receipt away unreviewed |
| `GET /pantry` | Inventory, grouped by location, expiry-sorted |
| `GET /shopping-list` | The open list |
| `POST /shopping-list` | Add an entry |
| `DELETE /shopping-list/{id}` | Clear one |

`PATCH` and `discard` are valid only while the receipt is `pending`; both
return 409 once it is `confirmed`. After that the items are real inventory and
are edited as inventory — a confirmed receipt is a historical record, not a
document that stays editable.

Discarding sets the receipt to `discarded` and its pending items with it. The
row is kept rather than deleted so `image_sha256` still blocks re-uploading a
receipt you already rejected.

`/health` gains a `pantry` block: receipts stuck in `extracting` past five
minutes (extraction died), and active items past their expiry (you stopped
confirming). Both are quiet failures, which is the kind this project surfaces.

## Router

Two new tools. The prompt grows by roughly fifteen lines and remains far under
Haiku 4.5's 4096-token cacheable minimum, so nothing changes about caching.

| Tool | Parameters |
| :-- | :-- |
| `consume_item` | `name`, `amount?` |
| `add_to_list` | `name` |

`query` gains `kind: 'pantry'` with a `subject` — "what's in the fridge", "do
we have eggs", "what do I need at the store". Reusing the existing tool instead
of adding a third keeps the router's decision space small, which is where its
accuracy lives.

`consume_item` matches `name` against active items using the same fuzzy
approach as `reschedule`'s `_find_match`. No match is not an error: reply
"I don't have milk in the fridge, but I've added it to the list" and add it.
The useful half still happens.

`amount` is deliberately coarse. Only two outcomes exist:

- Absent, or a phrase meaning all of it ("we're out of", "finished the") —
  the item goes `consumed` and lands on the shopping list.
- A partial phrase ("used half the", "had some of the") — `quantity` is
  reduced if a number can be resolved, the item stays `active`, and **nothing
  is added to the list**.

There is no fractional consumption model beyond that. Tracking "0.4 of a
chicken" is precision the system cannot honestly maintain, and the only
decision that depends on it — do I need to buy more — is one you make at the
fridge door anyway.

Recipes need no new tool — one line in the system prompt sends cooking and meal
suggestions to `escalate`.

Confirmations stay templated in Python, per the existing convention.

## Expiry push

A new sweep in `scheduler/run.py`. Not `reminders` rows: those would pollute
`/agenda` with groceries, and finishing the milk early would strand a reminder
that still fires. The sweep reads `pantry_items` directly and is therefore
correct by construction — an item that is gone cannot notify.

- Fires once local time passes `PANTRY_EXPIRY_HOUR` (default 17:00) — late
  enough to still cook or shop, early enough to act on.
- **One batched push per day**: "3 things expire tomorrow — milk, spinach,
  chicken." Per-item pushes are how a useful feature becomes a muted one.
- Idempotent through `notified_on`. A `notify.push()` returning `False` leaves
  it NULL so the next tick retries — preserving the bool contract CLAUDE.md is
  explicit about, and never claiming a delivery that did not happen.
- Expiring items are added to `shopping_list` with `reason='expiring'` in the
  same sweep.

## Deep path

`mcp_server/server.py` gains one tool: `pantry_inventory()`, returning active
items with `days_until_expiry`, soonest first. Sorting is the whole point — the
agent sees "spinach: 2 days" at the top and builds the suggestion around it
rather than producing something generic. The answer returns by push on the
standard deep-path contract.

## iOS

A Pantry tab, three sections, using the existing `Theme.swift`:

1. **Capture** — button opens the camera, uploads, polls, then presents the
   **review sheet**: items sorted perishables-first, each with a pre-filled date
   stepper, swipe to delete a misread line, one Confirm button.
2. **Fridge** — grouped by location, expiry-sorted, urgency-colored.
3. **List** — the shopping list, swipe to clear.

## Error handling

| Failure | Behaviour |
| :-- | :-- |
| Extraction fails or times out | `extract_error` set; review screen offers Retry and Enter manually. Never a silently empty receipt |
| Same photo uploaded twice | `image_sha256` unique constraint returns the existing receipt |
| Receipt stuck `extracting` | Surfaced by `/health` after five minutes |
| Blurry or partial read | No special handling — the review screen is the mitigation, by design |
| No registered device at expiry time | `push()` returns `False`, `notified_on` stays NULL, next tick retries. Same posture as reminders |

## Testing

New files following existing `conftest.py` patterns: `tests/test_pantry.py`,
`tests/test_pantry_expiry.py`, `tests/test_receipt_extract.py`.

- Shelf-life table and date arithmetic — pure, no database.
- Extraction against a stubbed Anthropic client, matching the Gmail extractor
  tests, including malformed responses and API errors.
- Expiry sweep: fires once per day; does not fire for consumed items; requeues
  when `push()` returns `False`; batches several items into a single push.
- `/undo` after a receipt confirm removes every item from that trip.
- `/undo` after "we're out of milk" restores the item **and** removes the list
  entry — the multi-mutation fix.
- Router test set gains the new phrasings, including ones that must *not* route
  to `consume_item`: "buy milk" is a list add, "milk expired" is a discard.

## Out of scope

Barcode scanning, nutrition data, cost tracking over time, multi-person
households, and any write back to a grocery service. None are needed to answer
"what do I have, what is about to die, and what can I cook".
