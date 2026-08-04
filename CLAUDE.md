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

## Pantry

A photographed receipt becomes inventory, but only through a human.

`receipts` → `pantry_items` mirrors `proposals` → `events`: extraction
proposes, a person disposes. The extractor's job is narrow — it reads pixels
and maps `GV WHL MLK 1GAL` to a name and a category from a fixed enum. **It is
never asked for an expiry date.** Dates come from `pantry/shelflife.py`, a
checked-in table, so a wrong date is one line to edit rather than a prompt to
re-tune. The review screen pre-fills them and you confirm.

Things worth not rediscovering:
- **Confirming a receipt logs exactly one mutation**, on the `receipts` row —
  the item writes deliberately bypass the helper. Thirty log rows per shopping
  trip would bury your last real action, the same reasoning that keeps synced
  writes out of the log. `pantry_items.receipt_id` is `ON DELETE CASCADE`, so
  reversing that one insert takes the whole trip with it.
- **`undo_last` reverses every mutation sharing an `utterance_id`**, not one
  row. Consuming an item writes twice — the item and the shopping list — and
  so does `add_event` with a new person. A NULL `utterance_id` is not a group
  key and still undoes one row at a time.
- **Expiry pushes are a scheduler sweep, not `reminders` rows.** Reminders
  would appear in `/agenda` among your appointments, and one scheduled ahead
  of time would still fire after you finished the milk. The sweep reads
  `pantry_items` live, so a gone item cannot notify. One batched push a day,
  at `PANTRY_EXPIRY_HOUR` (default 17:00) local.
- **Receipt extraction tokens are not in `/metrics`.** That block is
  per-utterance and a receipt has no utterance behind it. One vision call per
  shopping trip is not what a spend report is for.
- `PANTRY_VISION_MODEL` defaults to `claude-haiku-4-5`. Thermal receipt print
  is hard OCR; if reviewing is tedious, move it to Sonnet in `.env` rather
  than touching a code path.

## Voice

Replies are synthesized on the Mini in two stages and played by the phone;
Apple's `AVSpeechSynthesizer` is the fallback. Two round trips — `/say`
returns text on its old budget, `/speech` returns audio separately — because
folding synthesis into `/say` would put half a second inside the endpoint
whose p95 is the system's headline number.

**The voice is a Piper voice converted toward a reference clip.** Piper
supplies the words, the accent and the timing; Kanade, a neural codec,
re-voices that audio to match a reference speaker and a vocoder emits it at
24kHz. Both halves were arrived at by listening, and the reasons are worth
keeping:

- Piper alone was right in character and audibly fuzzy — its training format
  is fixed at 22.05kHz mono, so no Piper voice clears that ceiling.
- Conversion alone, driven from Kokoro's default American voice, produced
  American phrasing in the right timbre. **Voice conversion keeps the
  source's prosody**, which is why the source is a British Piper voice and
  not whatever was convenient.
- The round trip *raises* fidelity rather than lowering it: the vocoder
  reconstructs from the codec's training distribution, not from Piper's
  artifacts. This is the one place in the system where a lossy stage makes
  the output better, and it is not obvious enough to rediscover.

**This put PyTorch in the repo, reversing an earlier decision.** `kokoro-onnx`
was chosen over the reference `kokoro` package specifically to avoid torch.
Kanade is torch and there is no ONNX build, so the dependency list grew by
~2.5GB. It bought the only voice that sounded right; that is the whole
justification, and if the voice ever changes this should be revisited rather
than inherited.

**Synthesis is chunked, and that is still the latency story, but the numbers
moved.** Conversion dominates — about 40ms of Piper against ~385ms of Kanade
for one clause — so first sound is ~680ms rather than Kokoro's ~305ms. That
was a knowing trade. Chunking matters more than before, not less: without it
a three-sentence reply waits on the whole conversion. Measured end to end
over HTTP; `/health` reports both numbers.

- **The filesystem is the switch.** No `TTS_ENABLED`. If the weights are not
  in `$JARVIS_DB`'s parent `voices/` directory, `/speech` answers 503 and the
  phone uses the Apple voice. A flag that can disagree with the filesystem is
  a flag that eventually will.
- **Cuts land only on punctuation that already carried a pause**, and the
  floor scales with how much pause the mark carries: a full stop can cut at
  any length, a dash or semicolon needs 8 characters behind it, a comma needs
  24. Each chunk is a separate utterance to Piper and gets its own intonation
  contour, so a cut anywhere else is audible. `TTS_STREAM_CHUNKS=0` turns it
  off — like the voice itself, whether this sounds right is a question for
  ears. Unchunked, a long reply can exceed the converter's single-pass window
  (~7.4s of audio), which `speech/clone.py` handles by windowing the mel and
  vocoding once; that path exists only for `TTS_STREAM_CHUNKS=0`.
- **`/say` starts synthesis before the phone asks.** The reply exists ~900ms
  into `/say` and `/speech` does not arrive until ~140ms after it returns;
  `synth.prefetch()` spends that gap. It cannot raise and cannot block, or the
  reason synthesis is a second endpoint would be undone.
- **One cached utterance, not many.** The only text ever prefetched is the
  reply the phone is about to ask for, so a second slot would never be read.
- **The fallback is unconditional only up to the first sound.** Everything
  before it — unreachable, 503, a chunk that isn't a WAV — takes the same
  path, because they have the same remedy. After audio has started, a failure
  truncates instead: hearing the first clause again in a different voice is
  worse than hearing it once.
- **The `/speech` timeout scales with the text**, 4s to 15s. It was a flat 3s,
  chosen when synthesis was assumed quick; measured, one sentence is about a
  second, so a three-sentence answer could cross the line and drop to the
  Apple voice — silently, because that is what an unconditional fallback does.
- **The body is length-prefixed WAVs, not raw PCM.** Four-byte big-endian
  length, then that many bytes of complete WAV, repeated. An unframed PCM
  stream would play a stray error body as noise; a chunk that will not parse
  is simply not playable and takes the fallback path like everything else.
- **`warm()` runs a throwaway synthesis, not just a model load.** onnxruntime
  defers graph optimization and arena allocation to the first inference —
  measured at 850ms even with the session already built. Loading without ever
  calling moved that cost onto the first thing you say after a reboot, which
  is exactly what `warm()` exists to prevent.
- **`_lock` and `_slot_lock` are separate.** Claiming the cache slot must not
  wait behind an inference already running, or `/say`'s prefetch would block
  on the previous reply finishing.
- **`_synthesize()` holds a reentrant lock.** It calls `engine()`, which takes
  the same lock to build the session lazily. A plain `Lock` deadlocks on the
  first synthesis after a cold start, and only after a cold start — which is
  the worst kind of bug to ship.
- **`/speech` pulls the first chunk before it answers at all.** A streaming
  response commits to its status code when the headers go out, so anything
  that can fail has to fail before then — otherwise an unspeakable voice
  reaches the phone as a truncated 200 instead of the 503 it turns into "use
  the Apple voice". It costs nothing; the first chunk is the thing being
  waited for anyway.
- **`/speech` is a `def`, not an `async def`.** Inference now runs on its own
  thread, but this endpoint blocks waiting for it, and blocking the event loop
  would stall every other request. Starlette runs sync endpoints in a
  threadpool.
- **The phone keeps one audio session across the turn.** `Transcriber.stop()`
  deliberately does not deactivate it and `Speaker` does not switch category —
  `.playAndRecord` is already right for both. Tearing the session down after
  capture and rebuilding it for playback is a route reconfiguration sitting on
  the critical path, and it costs the first syllable. `Speaker` releases the
  session when it has finished speaking; `Transcriber.abort()` still releases
  it, because in that path no reply is coming.
- **Voice and pace are `.env`, not code.** `TTS_MODEL`, `TTS_REFERENCE` and
  `TTS_SPEED`, read at import — a change needs a daemon restart. Which voice
  is right was decided by listening, and re-deciding costs one line.
- **Changing the voice is changing a file, not retraining anything.** The
  conversion is zero-shot, so `TTS_REFERENCE` is the entire knob: a different
  clip is a different assistant. The current one is the Piper model's own
  sample, which means it is one lossy generation removed from the source it
  imitates — a genuinely clean recording is the largest remaining improvement
  available and needs no code.
- **There is no phonemization setting.** Piper carries `espeak.voice` in the
  `.onnx.json` beside its weights. Kokoro needed the language derived from the
  voice name because nothing else knew it; Piper ships the answer, so the two
  cannot disagree at all. `synth.lang_for` was deleted rather than ported —
  the Kokoro language map now lives in `speech/audition.py`, its last caller.
- **`available()` requires both stages.** Piper without a reference clip would
  speak in Piper's own voice rather than fail, and a confidently wrong voice
  is worse than the Apple fallback. Half an install is not an install.
- **All weights live under `voices/`, including torch's.** `speech/clone.py`
  sets `TORCH_HOME` before torchaudio is imported so WavLM lands there instead
  of `~/.cache`. Nothing in the speech path reaches the network at runtime,
  which is what keeps "the filesystem is the switch" true rather than
  approximately true.
- **The audition is a real tool, not a one-off.** `speech/audition.py` renders
  every candidate over the same lines, loudness-matched per voice, whole and
  chunked. `AUDITION_EXTERNAL=<dir>` ingests pre-rendered WAVs, which is how
  candidates that would never be dependencies get compared fairly. Every voice
  decision in this file was made with it, and the next one should be too.
- **Neither the weights nor synthesis appear in `/metrics`.** That block is
  per-utterance and costed against API spend; local synthesis has no token
  cost. `X-Synth-First-Ms` and `/health` carry the latency instead — and
  `/health` reports `last_synth_ms` and `last_first_chunk_ms` separately,
  because the first is what a reply costs and the second is what the phone
  actually waits for. A wide gap means chunking is working.

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

- **`mcp.json`'s `cwd` key is ignored.** Claude Code spawns the MCP server with
  the *parent's* working directory, which for a job is `WORK_DIR`, not the repo.
  `python -m mcp_server.server` therefore needs `env.PYTHONPATH` pointing at the
  repo; without it the server dies on ModuleNotFoundError. The failure is
  silent — the CLI reports no MCP error and omits `mcp_servers` from its JSON,
  so the agent just has no tools and the job *succeeds*, answering that it has
  no way to search your mail. Covered by
  `test_mcp_server_starts_from_outside_the_repo`.
- **Prompt caching does not fire here.** Haiku 4.5's minimum cacheable prefix
  is 4096 tokens. Measured with `count_tokens`, the router prompt plus its
  eleven tool definitions is **3322** — closer than it sounds, but under.
  Keep the prompt byte-stable anyway (free, and matters if it grows), but
  don't budget for the savings. Padding the prefix past 4096 on purpose would
  make the cache fire; it was measured and rejected, because the upside is
  only the prefill share of a ~900ms call and the cost is a permanently
  bloated prompt.
- **The Anthropic client sets its own httpx keepalive.** httpx expires idle
  connections after 5s by default and the SDK does not override it, so an
  assistant spoken to every few minutes would re-handshake on every request.
  Measured from the Mini that is only ~15ms, which is why this is one line in
  `_client()` and not a subsystem.
- **The fast path is the model call, essentially all of it.** Measured
  directly, `router.route` is 800–1065ms against a recorded end-to-end p50 of
  ~1.4s and a floor of 686ms. The four SQLite transactions per `/say` and the
  per-request `devices.touch` write look wasteful and are not worth
  optimizing — they are inside the noise. Optimize the hops around the model,
  not the bookkeeping.
- **The Claude Code subscription cannot serve the fast path.** Measured
  `claude -p --model haiku` at 1.88 / 2.12 / 1.97s for a trivial prompt — that
  is startup alone, against a 2s end-to-end budget. `ANTHROPIC_API_KEY` is a
  real requirement, not a convenience.
- **FileVault is ON**, so auto-login is unavailable and the machine boots to a
  pre-boot unlock screen where neither LaunchAgents nor LaunchDaemons run.
  Unattended recovery from power loss is not currently possible; use
  `sudo fdesetup authrestart` for planned reboots.
- **Push is APNs here**, selected by `PUSH_BACKENDS` — this deployment runs
  `apns` alone, the cutover having already happened. The code default is still
  `ntfy`, so a fresh checkout without a `.env` behaves differently from the
  Mini. `ntfy,apns` fans out to both, which is how the cutover was done.
  If ntfy is in use, its topic name is the only secret — treat it as a password.
- **`notify.push()` returns a bool and never raises.** `scheduler/run.py` calls
  it in a loop over every due reminder: `False` requeues that one reminder,
  where an exception would abort the tick and take every *other* due reminder
  with it. Preserve both halves.
- **No registered device makes `push()` return `False`**, so on `apns` alone
  the scheduler retries every tick until the reminder ages out at six hours.
  Deliberate: the alternative is reporting delivery of a notification that went
  nowhere, which is the exact failure this subsystem exists to make loud.
- Python is pinned to **3.12** (not the system 3.14) because `ctranslate2`,
  torch and the speech stack trail new CPython on prebuilt wheels. This is now
  load-bearing rather than precautionary: local TTS landed.
- **espeak-ng truncates its data path at 160 characters.** Past that it
  silently falls back to the path compiled into the wheel — which is a CI
  directory on a build machine — and reports a missing `phontab` for a file
  that is present. The venv here is well inside the limit; a deployment under
  a deeper path would not be, and the error names the wrong cause.

## Conventions

- Every write goes through one helper that records `before_json`/`after_json`
  in `mutations`, in the **same transaction** as the domain write.
- Destructive operations over voice require a spoken confirmation turn.
- Log every utterance with `latency_ms`. Treat p95 > 2s as a bug.
- `foreign_keys` is per-connection — `app.db.connect()` sets it. Don't open
  raw `sqlite3.connect()` elsewhere.
- Column naming: `_at` is an instant, ISO 8601 with offset. `_on` is a bare
  calendar date, `YYYY-MM-DD`, for things that genuinely have no time —
  `pantry_items.expires_on` is the case that introduced it. An expiration has
  no time of day, and inventing a midnight offset would be a lie the rest of
  the system would then have to reason about.
