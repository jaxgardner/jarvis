# Speed, senses, and memory — plan index

> **For agentic workers:** This is a map, not a plan. Do not implement from
> this file. Open the plan for the part you were sent to and follow it
> task-by-task using superpowers:subagent-driven-development or
> superpowers:executing-plans.

Spec: `docs/superpowers/specs/2026-08-04-speed-senses-memory-design.md`

Six parts, one plan each. They are independent except where stated below.

| Part | Plan | Depends on | Ships what |
| :-- | :-- | :-- | :-- |
| 1 | [`2026-08-04-part1-turn-latency.md`](2026-08-04-part1-turn-latency.md) | — | ~3000ms → ~1500ms perceived |
| 2 | [`2026-08-04-part2-local-router.md`](2026-08-04-part2-local-router.md) | **Part 1** | A local `route()`, or a recorded dead end |
| 3 | [`2026-08-04-part3-messages-calls.md`](2026-08-04-part3-messages-calls.md) | — | Texts and missed calls |
| 4 | [`2026-08-04-part4-order-tracking.md`](2026-08-04-part4-order-tracking.md) | — | Amazon / Walmart deliveries |
| 5 | [`2026-08-04-part5-commerce.md`](2026-08-04-part5-commerce.md) | Part 4 | Drafting and placing orders |
| 6 | [`2026-08-04-part6-vault.md`](2026-08-04-part6-vault.md) | — | Obsidian as the notes surface |

## Order

**Start with Part 1.** It is the only part another part depends on, and it
installs the measurement — `turn_ms` — that every later part is judged
against. Part 2 cannot be evaluated at all until Part 1 exists, because its
ship/no-ship gate is stated in `turn_ms`.

After Part 1, the rest are genuinely independent. The recommended order is
**3 → 4 → 5 → 6 → 2**, on the reasoning that Parts 3–6 add capability while
Part 2 only adds speed to something already fast enough, and Part 2 is the
one allowed to fail.

Part 5 depends on Part 4 for the `orders` table and the Gmail body allowlist.

## Migration numbers are reserved

Claim them in this order regardless of which part you build first. Numbers are
assigned per part, not per completion, so two parts built out of order cannot
collide.

| Part | Migration |
| :-- | :-- |
| 1 | `016_turn_timings.sql` |
| 3 | `017_messages_calls.sql` |
| 4 | `018_orders.sql` |
| 5 | `019_commerce.sql` |
| 6 | `020_vault.sql` |

Part 2 has no migration.

## What every part shares

These come from the spec and apply to every task in every plan. Do not
re-derive them and do not contradict them.

- **Python 3.12**, pinned. `uv` manages the venv at `.venv/`.
- **Timestamps are ISO 8601 with offset.** Never naive local time. A column
  ending `_at` is an instant; a column ending `_on` is a bare `YYYY-MM-DD`
  for something with genuinely no time of day.
- **Every human-initiated write goes through `app/mutations.py`**, in the same
  transaction as the domain write. Synced and derived writes deliberately do
  not — see the spec for which is which in each part.
- **`app.db.connect()` only.** Never a raw `sqlite3.connect()`; `foreign_keys`
  is per-connection and `connect()` is what sets it.
- **`reply` is one plain-text line**, safe to hand to a TTS engine. No
  markdown, no lists, no emoji, no newlines.
- **Tests run with `uv run pytest`.** Live-model tests are marked and skipped
  without `ANTHROPIC_API_KEY`.
- **Commit after every task**, never in the middle of one.

## Where to go when a part is done

Each plan ends with a "Next" section naming the following plan. Follow it. If
you finished Part 1, go to Part 3 — not Part 2 — unless you were told
otherwise; Part 2's gate wants a few days of real `turn_ms` data behind it,
and running it on the afternoon Part 1 lands measures a cold prompt cache
against a warm API connection and concludes the wrong thing.
