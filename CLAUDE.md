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
    mcp/         jarvis-mcp stdio server            (Phase 3)
    worker/      claude -p job runner               (Phase 3)
    scheduler/   reminder firer, no LLM             (Phase 2)
    migrations/  numbered .sql, applied by migrate.py
    tests/

The database lives **outside** the repo, at `$JARVIS_DB`
(default `~/Library/Application Support/jarvis/jarvis.db`), so it is never
committed and survives a re-clone.

## Schema

See `migrations/001_init.sql` — it is the authoritative copy, applied in full
during Phase 0. Domain tables: `events`, `reminders`, `people`, `projects`,
`notes`. Operational: `utterances`, `mutations`, `jobs`. Search: `notes_fts`
(external-content FTS5 + three sync triggers).

Timestamps are **ISO 8601 with offset**. Never naive local time, never a bare
date for something that has a time. This is the single most common source of
bugs in this kind of system.

Two schema subtleties worth not rediscovering:
- `idx_events_ext` is a *partial* unique index — it dedupes synced events while
  still allowing many NULL `external_id` rows.
- Notes are soft-deleted, which fires the FTS *update* trigger, not the delete
  trigger. Soft-deleted rows stay in the index; search must join `notes` and
  filter `deleted_at IS NULL`.

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
  the Kokoro stack trail new CPython on prebuilt wheels. Matters at Phase 4.

## Conventions

- Every write goes through one helper that records `before_json`/`after_json`
  in `mutations`, in the **same transaction** as the domain write.
- Destructive operations over voice require a spoken confirmation turn.
- Log every utterance with `latency_ms`. Treat p95 > 2s as a bug.
- `foreign_keys` is per-connection — `app.db.connect()` sets it. Don't open
  raw `sqlite3.connect()` elsewhere.
