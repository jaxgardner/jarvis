# Jarvis

Personal assistant. Mac Mini M4 (always-on) + iPhone (primary client).
Python 3.12 + FastAPI + SQLite. Fast path: Claude Haiku via the Messages API.
Deep path: Claude Code headless.

This file exists so settled decisions stay settled. If something here seems
wrong, say so — don't silently do it differently.

## Design principles

1. **Two tiers, not one.** Simple capture and lookup never touch Claude Code.
   Only genuinely agentic work does. Mixing them makes everything feel slow.
2. **SQLite is the source of truth.** Not a markdown file, not the context
   window. Every component reads and writes the same database.
3. **Reminders must fire even when the agent is broken.** The scheduler is a
   separate process with no LLM dependency.
4. **Every mutation is logged and reversible.** Voice input is lossy; you will
   mis-hear things and need to undo them.
5. **Latency is a budget, measured per hop.** Store `latency_ms` on every
   utterance from day one. You can't optimize what you don't record.

## Layout

    app/         FastAPI: routes, Haiku router, handlers, reply templates
    mcp/         jarvis-mcp stdio server
    worker/      claude -p job runner
    scheduler/   reminder firer, no LLM
    ingest/      Google Calendar + Gmail importers
    migrations/  numbered .sql, applied by migrate.py
    tests/

The database lives **outside** the repo, at `$JARVIS_DB`
(default `~/Library/Application Support/jarvis/jarvis.db`), so it is never
committed and survives a re-clone.

## Schema

See `migrations/001_init.sql` — it is the authoritative copy, applied in full
up front. Domain tables: `events`, `reminders`, `people`, `projects`,
`notes`. Operational: `utterances`, `mutations`, `jobs`. Search: `notes_fts`
(external-content FTS5 + three sync triggers). Ingestion:
`sync_state`, `proposals`, `email_messages` + `email_fts`.

Timestamps are **ISO 8601 with offset**. Never naive local time, never a bare
date for something that has a time. This is the single most common source of
bugs in this kind of system.

Schema subtleties worth not rediscovering:
- `idx_events_ext` is a *partial* unique index — it dedupes synced events while
  still allowing many NULL `external_id` rows.
- Notes are soft-deleted, which fires the FTS *update* trigger, not the delete
  trigger. Soft-deleted rows stay in the index; search must join `notes` and
  filter `deleted_at IS NULL`. Email rows are *hard*-deleted when they age out,
  so `email_fts` needs no such join.
- `idx_proposals_ext` excludes rejected rows, so it does **not** stop a message
  being re-proposed after you said no. `ingest.gmail.candidates()` enforces
  that, by skipping any message with a proposals row of any status.
- `proposals.event_id` is `ON DELETE SET NULL` (migration 007). It has to be:
  accepting a proposal is undoable, and `/undo` reverses an insert with a hard
  delete, which a RESTRICT reference refuses.

## Ingestion

Read-only, one direction. Voice events stay `source='voice'` and are never
pushed to Google. Two sources, two very different postures:

- **Calendar → `events`, directly.** Structured in, structured out, nothing to
  extract and nothing to review.
- **Gmail → two places.** Metadata and Google's snippet into `email_messages`,
  which `query` reads so the assistant can answer "did the landlord write
  back?". Separately, a narrow query feeds a capped Haiku extractor whose
  output lands in `proposals` — a review queue. **No path from email to
  `events` without a human accepting it.**

Message bodies are never stored. `format=metadata` means Gmail does not return
them, so there is no path to storing them by accident.

**Synced writes bypass the mutations helper.** This is an exception to the
invariant below, and a deliberate one: the log exists to make *voice* input
reversible, a sync is not a user action, and a few hundred synced rows would
bury the user's last real action and make `/undo` useless for exactly what it
was built for. Anything a human *accepts* goes through the helper as normal.

Cursors expire on Google's schedule — Calendar answers **410**, Gmail **404**.
Both are routine: drop the cursor, refetch in full. An ingester that treats
either as fatal stops permanently after a quiet week.

## API contracts

### `POST /say`

    // request
    {"text": "...", "client": "shortcut", "tz": "America/Denver"}

    // fast path
    {"reply": "...", "route": "fast", "utterance_id": 412, "latency_ms": 580}

    // deep path
    {"reply": "On it. I'll ping you when it's done.", "route": "deep",
     "job_id": 27, "utterance_id": 413}

`reply` is always a single plain-text string safe to hand straight to a TTS
engine — no markdown, no lists, no emoji.

### Supporting endpoints

| Endpoint | Purpose |
| :-- | :-- |
| `GET /agenda?days=1` | Events + reminders in window |
| `POST /undo` | Reverse the most recent non-undone mutation |
| `GET /jobs/{id}` | Poll deep-path status |
| `GET /health` | Liveness for launchd and uptime checks |
| `GET /metrics` | p50/p95 latency by route, last 24h |
| `POST /devices` | Register a device (or refresh its APNs token) |
| `GET /devices`, `DELETE /devices/{id}` | List, and revoke a lost one |
| `POST /reminders/{id}/snooze`, `/ack` | Notification action buttons |
| `GET /activity` | Utterances + what each one changed; drives swipe-to-undo |
| `GET /jobs` | Deep-path history (results truncated; full text on `/jobs/{id}`) |
| `GET /proposals` | Pending email extractions awaiting review |
| `POST /proposals/{id}/accept`, `/reject` | Dispose of one. Accept writes through `mutations` |
| `GET /inbox` | Recent ingested mail; `?q=` searches, `?unread_only=` filters |

`/health` gains an `ingest` block: per-source `last_run_at` / `last_ok_at` and
a `stale` list. The two timestamps are separate on purpose — equal means
healthy, a gap means running-and-failing, both old means not running at all,
and those need different fixes.

`/metrics` takes `?days=N` and includes a `spend` block — token counts are
stored on `utterances` and costed at read time, so a price change re-costs
history instead of freezing the old rate. Only the fast path is counted; the
deep path runs on the Claude Code subscription, not API credits.

Auth is a bearer token: either `JARVIS_TOKEN` (shared — the Shortcut uses it,
and it is what enrolls a device) or a per-device token minted by `POST
/devices` and returned exactly once. Only the sha256 is stored. The
implementation is `app/devices.py`.

### Fast-path router

Call Haiku (`claude-haiku-4-5`) with `tool_choice: {"type": "any"}` so you
**always** get structured output — never free text to parse. The router *is*
the tool choice; there is no separate classifier model.

| Tool | Parameters |
| :-- | :-- |
| `add_event` | `title`, `starts_at`, `ends_at?`, `location?`, `all_day?` |
| `add_reminder` | `body`, `fire_at`, `recurrence?` |
| `add_note` | `body`, `tags?`, `project?`, `person?` |
| `query` | `question`, `window_days?` |
| `undo_last` | — |
| `escalate` | `restated_task` — routes to the deep path |

The system prompt must include current datetime with offset, timezone name,
day of week, and an instruction to resolve all relative times to absolute
ISO 8601.

**Confirmations are templated, not generated.** Once you have
`add_reminder(body=..., fire_at=...)`, format the reply in Python.
Deterministic, and it saves a round trip.

## Local facts that are easy to get wrong

- **Prompt caching does not fire here.** Haiku 4.5's minimum cacheable prefix
  is 4096 tokens; the router prompt plus six tool definitions is well under it.
  Keep the prompt byte-stable anyway (free, and matters if it grows), but don't
  budget for the savings.
- **The Claude Code subscription cannot serve the fast path.** Measured
  `claude -p --model haiku` at 1.88 / 2.12 / 1.97s for a trivial prompt — that
  is startup alone, against a 2s end-to-end budget. `ANTHROPIC_API_KEY` is a
  real requirement, not a convenience.
- **FileVault is ON**, so auto-login is unavailable and the machine boots to a
  pre-boot unlock screen where neither LaunchAgents nor LaunchDaemons run.
  Unattended recovery from power loss is not currently possible; use
  `sudo fdesetup authrestart` for planned reboots.
- **Push is ntfy**, not Pushover. One `POST {NTFY_SERVER}/{NTFY_TOPIC}`. The
  topic name is the only secret — treat it as a password.
- Python is pinned to **3.12** (not the system 3.14) because `ctranslate2` and
  the Kokoro stack trail new CPython on prebuilt wheels. Matters if local TTS
  ever lands.

## Conventions

- Every write goes through one helper that records `before_json`/`after_json`
  in `mutations`, in the **same transaction** as the domain write.
- Destructive operations over voice require a spoken confirmation turn.
- Log every utterance with `latency_ms`. Treat p95 > 2s as a bug.
- `foreign_keys` is per-connection — `app.db.connect()` sets it. Don't open
  raw `sqlite3.connect()` elsewhere.
