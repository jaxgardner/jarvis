# Speed, senses, and memory

Three things at once: make a turn feel immediate, let the assistant see the
rest of the machine's data, and give it somewhere to keep what it learns.

## Why one spec, against convention

Every other spec here covers one feature. This one covers six, deliberately,
because they were designed together and share a budget: memory on a 16 GB box,
GPU time on a 10-core M4, and prompt tokens in a router that already measures
3244 input tokens on the median call. Specifying them separately would have
each part quietly assume it was the only claimant.

Each part below is written to lift cleanly into its own implementation plan.
Part 2 is the only one that depends on another — it is worth building only if
Part 1's measurements say the model call still dominates.

## What is actually slow

Measured on the Mini, 2026-08-04, from `utterances` (n=119) and live probes.
These numbers are the justification for every decision in Part 1.

| grouping | n | avg ms |
| :-- | --: | --: |
| one model call | 64 | 1410 (3244 in-tok, 76 out-tok) |
| two model calls (`query`) | 16 | 2675 (6290 in-tok) |
| `undo_last`, the cheapest single call | 2 | 860 (min **686**) |
| fast p50 / p95 | 103 | 1356 / **3207** |

Live `/say`, one-call write: 1349 / 1433 / 1571 ms.
Live `/speech` first chunk (`X-Synth-First-Ms`): 739 / 892 / 780 ms.

**`latency_ms` is not what the user experiences, and that is the finding that
reorders the whole plan.** It times `/say`. The turn is:

| hop | ms |
| :-- | --: |
| endpointer pause | 800 |
| transcript finalization + LAN | ~110 |
| `/say`, one call | 1410 |
| phone issues `/speech` | ~140 |
| first chunk, net of the prefetch head start | ~640 |
| **endpoint → first syllable** | **≈ 3000** |

**Sub-second is arithmetically unavailable and the spec does not pretend
otherwise.** With a free model and free synthesis the endpointer alone is
800 ms. The target is **1.1–1.4 s**, and it is reached by removing a model call
and shortening a timer — not by changing the model layer, which is what Part 2
exists to test rather than assume.

**p95 is 3207 ms against the project's own 2 s rule.** The cause is the second
model call on `query`. Part 1 removes it.

---

# Part 1 — The turn

## Why this shape

**The number to optimize has to be the number you feel.** Everything in this
part is judged against endpoint-to-first-syllable, not `/say`. The two differ
by roughly 1550 ms, and optimizing the smaller one is how a system gets faster
on paper and no faster in the room.

**Pre-retrieval, not a second call.** `query` costs 2675 ms against 1410 ms for
one reason: it searches *after* the router has decided to search. But the
search is free — measured SQLite time across the whole request is ~3 ms — so
there is no reason to spend a 660 ms round trip deciding to do a 3 ms thing.
Run FTS over the raw utterance text before the router call and hand the result
in as `CONTEXT`; the router answers from it via `answer`, or calls `query` when
it does not contain the answer.

**`CONTEXT` is question-derived, and unlike `TODAY` it says so.** `TODAY` is
safe because it is built before the utterance is read, so no search over the
user's words can put a wrong thing in it. `CONTEXT` gives that up knowingly.
The safety comes from a different place: `query` stays reachable and the router
is told a hit is a candidate, not an answer. A miss therefore degrades to
today's two-call path, which is the behaviour being replaced — the worst case
is the current case.

**The endpointer is the largest fixed block and always was.** `VoiceSettings`
already records this in a comment. 800 ms is spent waiting on someone who has
finished talking. `Endpointer` has `minimumSpeech = 0.35` and `rearm()`, so a
premature fire resumes listening rather than truncating; that is what makes a
shorter timer safe to try.

**No new transport.** The phone spends ~140 ms asking for audio after `/say`
returns. A persistent socket would recover most of it and cost a subsystem.
Not worth it; `prefetch` already covers the larger share of that window.

## What is measured

`turn_ms` is measured on the phone, from the endpointer firing to the first
audio buffer being scheduled, and reported by a fire-and-forget `POST /turns`
after playback has started. Off the critical path, and it always reports — a
piggyback on the next `/say` would silently drop the last turn of every
session, which is the turn most likely to have been the slow one.

Server-side, `_say` records its own hops into a `timings` JSON column and
returns them as a `Server-Timing` header.

On iOS, `OSSignposter` intervals cover `endpoint→stop`, `stop→say-sent`,
`say-sent→say-returned`, `say-returned→speech-requested`, and
`speech-requested→first-audio`, so the whole turn reads as one timeline in
Instruments.

## Schema

Migration `016_turn_timings.sql`:

```sql
ALTER TABLE utterances ADD COLUMN turn_ms INTEGER;   -- client-measured, nullable
ALTER TABLE utterances ADD COLUMN timings TEXT;      -- JSON, server hop breakdown
```

Both nullable and both permanently so: a Shortcut client reports no `turn_ms`,
and that is a client without a microphone rather than a missing measurement.

## Server

`POST /turns` — `{"utterance_id": 412, "turn_ms": 1840}`. Fire-and-forget,
returns 204, ignores an unknown id rather than erroring. A late or duplicated
report is not worth a failure path.

`GET /metrics` gains a `turn` block beside `latency`: p50/p95 of `turn_ms`
over the same window, counted only over utterances that reported one.

`handlers.context_block(text)` runs `notes_fts` and `email_fts` over the raw
utterance, returns at most 5 hits as `NOTE:` / `EMAIL:` lines in the shared
format `agenda_lines` already establishes. Empty string when nothing matches,
so the prompt loses the block entirely rather than carrying an empty heading.

`synth.prefetch(reply)` moves above `_finish()` in `_say`. Small and free.

`speech/segment.py` takes a lower length floor for the first chunk only. First
sound is gated by chunk 1; the comma floor of 24 characters is usually what
sets it. Later chunks keep today's floors, so intonation is unchanged
everywhere the listener is already listening.

## iOS

`VoiceSettings.choices` gains `("Fast 0.45s", 0.45)` and `defaultPause` becomes
0.45. The comment stays: this is the one number here to re-decide by ear.

`Speaker` reports `turn_ms` once the first buffer is scheduled. `TalkView` owns
the start timestamp, set when `didEndpoint` latches.

`HealthView` shows turn p50/p95 above the existing `/say` pair, because it is
the number the goal is stated in.

## Testing

- `test_context_block` — a note that matches produces a `NOTE:` line; nothing
  matching produces an empty string, not a bare heading.
- `test_context_is_not_today` — `TODAY` is byte-identical for two different
  utterances, proving `CONTEXT` is the only question-derived block.
- `test_query_still_reachable` — a question whose answer is absent from
  `CONTEXT` still routes to `query`. This is the safety property; it is the
  test that must never be deleted.
- `test_turn_report_ignores_unknown_id` — 204, no row, no raise.
- `EndpointerTests` gains a 0.45 s case across the existing fixtures — café,
  mid-sentence breath, mic opening mid-utterance, door slam.

---

# Part 2 — The local router

## Why this shape

**This part may not ship, and the spec is written to make that a normal
outcome.** It is an experiment with a pass/fail gate, not a migration.

**Prefill, not throughput, is the risk.** The prompt is 3244 input tokens on
the median call with a 5253-token static prefix. On a 10-core M4 GPU a cold
prefill of that exceeds the 686 ms Haiku floor it would be replacing. Only a
persisted KV cache makes a local model competitive, which is why this is
`mlx-lm` and not Ollama: `make_prompt_cache` / `save_prompt_cache` and the
server's `--prompt-cache-dir` are explicit control, where Ollama's prefix
caching is implicit and re-prefills silently when the prefix shifts.

**The prompt is already shaped for it.** `_SYSTEM_STATIC` is byte-stable and
carries the `cache_control` marker; `_SYSTEM_LIVE` holds the clock and the
tables. That split was built for Anthropic prompt caching and maps one-to-one
onto a persistent MLX cache. The work is already done.

**3B, not 8B.** The router picks one of nine tools from a fixed schema — a
constrained-decoding problem, not a reasoning one. And 8B does not fit: the
box is 16 GB with 3.2 GB of swap already committed and the FastAPI process
alone resident at 1.58 GB (torch, Piper, Kanade, WavLM). Paging model weights
mid-inference is a 10x cliff.

**Tool calls must be forced, not requested.** There is no local equivalent of
`tool_choice: any`. Grammar-constrained decoding against the tool schema is
what keeps hard-won distinctions — `log_gratitude` versus `add_note` being the
one that already regressed once — from drifting.

**GPU contention is the reason this might lose.** Today the model runs on
Anthropic's servers, so generation and Kanade conversion are genuinely
parallel. Local, they contend for the same GPU and serialize. A model that
wins on tokens/sec can lose on `turn_ms`, which is why Part 1's measurement
lands first.

## Shape

**`route()` only. `answer()` stays on Haiku, permanently.** The two calls in
`router.py` are not alike and must not be moved together. `route()` emits a
tool call against a fixed schema — constrained decoding, which is what a 3B is
good at and what the grammar makes safe. `answer()` produces the prose spoken
to the user verbatim, and it is the one call in the system where a smaller
model's quality loss is *audible* rather than merely measurable. Part 1's
pre-retrieval already removes most `answer()` calls by folding them into the
first hop; what remains is the handful of questions that genuinely needed a
search, and those are the ones worth spending a good model on.

So the local model is confined to the decision and never reaches the words.
That boundary is the point of this part, not an implementation detail of it.

`ROUTER_BACKEND=haiku|mlx`, read at import, governing `route()` alone.
`app/router_mlx.py` implements `route()`'s signature; `router.py` dispatches.
Model `Qwen2.5-3B-Instruct-4bit` under `$JARVIS_DB`'s parent, beside
`voices/` — the filesystem is the switch here too, and a missing model falls
back to Haiku rather than failing a turn.

## The gate

Ships only if, over the live routing suites, it **ties** on routing and
**wins** on `turn_ms`:

- `tests/test_router_prompt.py`
- `tests/test_gratitude_routing.py`
- `tests/test_projects_routing.py`

Losing either is a result, not a bug to work around. Record the numbers in
CLAUDE.md either way — a measured dead end is worth more than an untested
assumption, and this one will otherwise be re-proposed every six months.

---

# Part 3 — Messages and calls

## Why this shape

**Full Disk Access goes on a purpose-built binary, never on the interpreter.**
Verified on this machine: `chat.db` returns `authorization denied` and
`CallHistoryDB/` returns `Operation not permitted` even to `ls`. Granting FDA
to `.venv/bin/python` would grant it to every script that interpreter ever
runs, forever, including anything a deep job decides to execute. `helpers/tccread`
is a small compiled binary that holds the grant, reads read-only, and emits
NDJSON on stdout. It is the only thing in the system with that reach, and its
whole surface is two queries.

**Read-only, one direction, like every other ingester.** Nothing is written
back to Messages. `chat.db` is live WAL and is opened `mode=ro`.

**Message text is often not in `text`.** Modern macOS stores it in
`attributedBody` as a typedstream archive. An importer that reads `text` alone
appears to work and silently drops most of the corpus.

**`ZCALLRECORD.ZDATE` is Core Data epoch**, 2001-01-01, not Unix. Off by
31 years, and plausible enough at a glance to survive review.

**Synced writes bypass the mutations helper**, as Calendar and Gmail already
do, for the reason CLAUDE.md gives: the log exists to make voice input
reversible, and a few thousand imported messages would bury the user's last
real action.

## Schema

Migration `017_messages_calls.sql`: `messages` (external id, chat handle,
direction, sent_at, body, service), `messages_fts` (external-content FTS5 with
the three sync triggers, matching `notes_fts`), and `calls` (handle,
direction, answered, duration, occurred_at). Both take a nullable `person_id`
so a handle that matches `people` can be named in an answer.

Hard-deleted on prune, like `email_messages`, so the FTS index needs no
join-and-filter — the soft-delete subtlety that applies to `notes` does not
apply here.

## Router

No new tool. `query` gains `kind='message'` and `kind='call'`, and
`context_block` from Part 1 searches `messages_fts` alongside notes and email.
A missed call is a fact about the day, so `today_block` gains a line for
unanswered calls since the last brief.

## Testing

- `test_attributed_body_decodes` — a real typedstream fixture yields its text.
- `test_core_data_epoch` — a known `ZDATE` maps to the right instant.
- `test_tccread_absent_is_not_fatal` — no binary, or no grant, marks the source
  stale in `/health` and leaves every other ingester running.

---

# Part 4 — Order tracking

## Why this shape

**This bends a stated invariant, so it names itself.** CLAUDE.md says message
bodies are never stored and that `format=metadata` means there is no path to
storing them by accident. That stays true for the general pass. This adds a
second, narrow pass at `format=full` restricted to an explicit sender
allowlist, and CLAUDE.md is rewritten to say so. A documented exception is
worth more than an invariant that has quietly become false.

**Bodies are retained, not just extracted.** Order emails are re-parsed as the
extractor improves, and refetching a body Gmail may have aged out is not
available. The cost is disk and a wider blast radius on the allowlisted
senders; both are accepted knowingly. FileVault is on, so at-rest encryption
is already the machine's answer.

**The allowlist is configuration, not inference.** `ORDER_SENDER_ALLOWLIST` in
`.env`. Nothing is fetched in full because its subject looked like an order —
the same naming-beats-guessing rule that governs project attachment.

## Schema

Migration `018_orders.sql`: `email_bodies` (`email_message_id` unique,
`body_text`, `fetched_at`, `ON DELETE CASCADE` from `email_messages`), and
`orders` (merchant, external order id, placed_at, status, eta_on, total_cents)
with `order_items` hanging off it.

`eta_on` is `_on`, a bare date: a delivery estimate has no time of day, and the
`pantry_items.expires_on` precedent settles it.

`email_bodies` gets its own retention sweep, longer than the metadata prune,
because the whole point is re-parsing later.

## Router

`query` gains `kind='order'`. `today_block` gains a line for anything arriving
today — a delivery is a fact about the day in exactly the way an appointment
is, and the brief already has the shape for it.

---

# Part 5 — Commerce

## Why this shape

**Drafting and submitting are two different verbs and two different tools.**
`draft_order` builds a cart. `confirm_order` submits one, and it takes a draft
id read from a `DRAFTS` block in the system prompt — the same
`id  merchant  total` shape as `REPORTS` and `PROJECTS`. A tool that submits
whatever was most recently drafted is the `is_follow_up` mistake with money
attached.

**The confirmation reads the total back and does not accept "yes".** Voice is
lossy — that premise is why `/undo` exists — and "yes" is the single easiest
word to hallucinate out of room noise. The reply states merchant, item count
and total, and requires a distinct phrase. This is the spoken confirmation turn
Conventions already requires for destructive operations, with the bar raised
because this one is irreversible by `/undo`.

**Cookie injection into headless Chromium does not work and the spec says so
up front.** Retail bot detection fingerprints TLS, canvas and WebGL, not just
cookies. The approach is a persistent user-data-dir on the real Chrome channel.
Sessions are expected to expire; expiry is a notification, not an exception.

**Automated access is against Walmart's terms and the account holds real
payment methods.** The failure mode is a banned account, not a failed request.
Recorded here so the trade is visible to whoever reads this next.

**DoorDash is specced and blocked.** `dd-cli` is official
(`doordash-oss/doordash-cli`, macOS arm64, built to be driven by agents) and
waitlist-gated. The wrapper is written against its documented interface; the
plan cannot complete until access lands.

## Schema

Migration `019_commerce.sql`: `order_drafts` (merchant, state, total_cents,
`created_at`, `submitted_at` nullable, `external_order_id` nullable) with
`order_draft_items` hanging off it. A submitted draft keeps its row and gains
`external_order_id`, so the `DRAFTS` block can exclude it while `/activity`
still shows what happened.

`order_drafts` joins `mutations.WRITABLE`. Nothing else in this spec does —
`messages`, `calls`, `email_bodies` and `orders` are all synced or derived, and
follow the existing rule that a sync is not a user action.

## Shape

`commerce/walmart.py` — Playwright, persistent context, real Chrome channel,
add-to-cart and read-cart only. `commerce/doordash.py` — `subprocess` around
`dd-cli`, treating an auth-expired exit code as a push event.

Sessions live in the macOS Keychain, never in `$JARVIS_DB` and never in the
repo. `/health` gains a `commerce` block with per-merchant `last_ok_at`,
following the `ingest` block's two-timestamp convention — equal means healthy,
a gap means running-and-failing, both old means not running.

`draft_order` and `confirm_order` both write through the mutations helper.
`/undo` on a submitted order cannot recall it and says so rather than
reporting success.

## Testing

- `test_confirm_requires_draft_id` — no id, no submission.
- `test_confirm_rejects_bare_yes` — the phrase gate holds.
- `test_expired_session_pushes` — expiry notifies and marks unhealthy; it does
  not raise into the turn.

---

# Part 6 — The vault

## Why this shape

**The vault is where notes live; SQLite is still what makes them undoable.**
The intent is that notes move into an Obsidian vault and are edited there. The
constraint is that `app/mutations.py` requires the domain write and its
`mutations` row to land in the same transaction, and a filesystem write cannot
join a SQLite transaction. Making markdown authoritative would make `/undo`
non-atomic for notes — and `/undo` is load-bearing precisely because voice
input is lossy.

So: **SQLite is the transactional store of record and markdown is a
materialized projection**, written immediately after commit. Obsidian is a
genuine read/write surface, because a watcher imports external edits back
through the mutations helper. An edit made in Obsidian is as undoable as one
made by voice. What the filesystem does not get is authority, and that is the
only part of "replaces" being declined.

**One directory, not two.** The vault is the existing
`work/projects/<id>-<slug>/` tree. Agent-written artifacts and human notes
become the same files, which makes the projects feature better rather than
adding a parallel store beside it.

**The graph is an edge table, not a library.** `[[wikilinks]]` parse into
`vault_links(src, dst)`; `query_knowledge_graph` is a recursive CTE. Sub-
millisecond, no dependency, and it reuses the FTS pattern `notes_fts` already
establishes.

**Not the Local REST API plugin.** It requires the Obsidian GUI to be running,
which contradicts the filesystem-is-the-switch rule the speech stack already
follows. A headless Mini must not depend on a window being open.

**Writes are atomic** — temp file then `os.replace` — so Obsidian's indexer
never sees a partial file.

## Schema

Migration `020_vault.sql`: `notes` gains `vault_path` (nullable, unique) and
`vault_mtime`. New `vault_links(src_note_id, dst_note_id, dst_raw)` where
`dst_note_id` is **nullable** — `dst_raw` preserves a link to a note that does
not exist yet, and an unresolved wikilink is a normal state in a vault rather
than an error.

Backfill is `vault/backfill.py`, a one-off script, **not** a migration.
Migrations here are numbered `.sql` applied by `migrate.py`, and projecting
rows onto the filesystem is not something SQL can do; smuggling it in as a
`.py` migration would break the one rule that directory has. It projects every
existing note to a file with frontmatter carrying id, `created_at`, tags and
`project_id`. Soft deletion becomes a `deleted_at` frontmatter key, so the
existing "search must join and filter" rule survives unchanged.

## Components

`vault/project.py` — SQLite row to markdown file, atomic, called after commit.
`vault/watch.py` — `watchdog` observer; a changed file whose `vault_mtime`
does not match imports back through the mutations helper.
`vault/graph.py` — wikilink parsing and the recursive CTE.

The projector is deliberately not inside the transaction. A crash between
commit and projection leaves a note in SQLite with a stale file, which the
watcher reconciles on next start. The reverse — a file with no row — cannot
happen, and that asymmetry is the point.

## Router

`add_note` is unchanged. `query` gains `kind='vault'` for graph questions;
ordinary note lookup stays on `kind='note'`, because a second way to ask the
same question is misroute surface bought for nothing.

## Testing

- `test_projection_is_atomic` — no partial file is ever visible.
- `test_external_edit_is_undoable` — editing in Obsidian produces a mutations
  row, and `/undo` reverses it.
- `test_crash_between_commit_and_projection` — the watcher reconciles; no note
  is lost.
- `test_unresolved_wikilink` — a link to a nonexistent note stores and does not
  raise.

---

# Known edges, accepted

- **Sub-second is not reachable** and is not the target. 1.1–1.4 s is.
- **`CONTEXT` can miss**, and the router then spends the second call it spends
  today. The degradation is to the status quo.
- **Part 2 may not ship.** The gate is real, and even shipping it moves one
  of the system's six model call sites. `answer()`, the report summary, the
  brief, receipt vision and the dormant proposals extractor all stay on the
  API; the deep path stays on the Claude Code subscription.
- **Full Disk Access is a manual grant** in System Settings and cannot be
  scripted. It survives updates to the helper only if the binary is signed
  stably.
- **FileVault still blocks unattended reboot**, so none of this recovers from
  power loss without a person. Unchanged by this work, and worth remembering
  before relying on any of it while away.
- **Order bodies are retained** for allowlisted senders. A wider blast radius
  than the rest of the mailbox, accepted for re-parseability.
- **A submitted order is not undoable.** `/undo` says so instead of pretending.
- **Two new router tools** take the count from nine to eleven, spending prompt
  budget and misroute surface. `confirm_order` earns it by being the only
  guard on an irreversible action; if `draft_order` proves rare, fold it into
  `escalate` and take the count back to ten.
- **Four new `query` kinds** — `message`, `call`, `order`, `vault` — plus a
  `DRAFTS` block and a longer `TODAY`. The static prefix grows, which is fine
  for Anthropic caching but is exactly the prefill Part 2 has to survive.
  Re-measure the cached-prefix token count after Parts 3–6 land; the byte-
  stability test in `test_router_prompt.py` is what keeps the growth honest.
