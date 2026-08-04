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
    gratitude/   three things a day: entries, and the evening prompt
    brief/       the 7am morning brief: mail summary, and the job
    projects/    named spaces: the queries, and a directory per project
    ingest/      Google Calendar + Gmail importers
    migrations/  numbered .sql, applied by migrate.py
    tests/

The database lives **outside** the repo, at `$JARVIS_DB`
(default `~/Library/Application Support/jarvis/jarvis.db`), so it is never
committed and survives a re-clone.

## Schema

See `migrations/001_init.sql` — it is the authoritative copy, applied in full
up front. Domain tables: `events`, `reminders`, `people`, `projects`,
`project_links`, `notes`. `events`, `reminders` and `jobs` each carry a
nullable `project_id` (migration 014).
Operational: `utterances`, `mutations`, `jobs`. Search: `notes_fts`
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

**Snippets are cleaned on the way in, and that is a latency fix, not tidiness.**
Marketing mail pads its preheader with zero-width characters so the inbox
preview shows the hook and nothing after it, and Gmail hands them back inside
`snippet`. They are invisible and they are not free: each is its own codepoint
outside the tokenizer's common set and costs two or three tokens. Measured on
one real morning, 611 of them were **1248 tokens — 66% of the entire context**
handed to `router.answer`, and stripping them took that call from ~2190ms to
~1540ms. `ingest.gmail.clean_snippet` filters by Unicode category (`Cf`, plus
U+034F, which is a combining mark and so would survive a `Cf`-only filter);
migration 015 cleaned the rows already stored, since `prune` would otherwise
have kept charging for them until they aged out.

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

## Gratitude

Three things a day, prompted at 10pm and spoken into Talk. The whole feature
is one table, one router tool, one sweep and one screen.

- **The prompt is a scheduler sweep, not a `reminders` row** — the same call
  the pantry expiry sweep made, for the same reasons. A reminder shows up in
  `/agenda` among your appointments, and one scheduled ahead of time would
  still fire after you logged your three at eight. The sweep reads
  `gratitude_entries` live, so a finished day cannot notify.
- **The day runs to 4am** (`GRATITUDE_DAY_START`). A 10pm prompt answered at
  half past midnight belongs to the day you were thinking about; under a
  midnight rule it opens a new day and leaves the old one looking skipped,
  which is the streak breaking for doing the thing. `entries.day_for` is the
  only place the boundary is decided, and every `entry_on` comes through it.
  The push window is separate and is real clock time, so nothing fires at
  00:30.
- **Three is a target, not a limit.** A fourth thing is stored and shown and
  the day still reads complete. Refusing gratitude because a counter is full
  is the pedantry `consume_item` already declines.
- **An incomplete today does not break the streak.** Today at zero is a day in
  progress; the streak ends at the first *finished* day that fell short. A
  number that turns into a reproach at 6pm is a number that gets muted along
  with the notification.
- **One push an evening, and no catch-up.** Deduped through a `heartbeats` row
  named `gratitude` whose `detail` holds the day it last pushed for — the same
  use `_selfcheck` already makes of that table. Nothing is stamped unless the
  push landed. A Mini asleep all evening produces no push and nothing the next
  morning: a gratitude prompt at 8am is about a day that is already gone.
- **Capture is voice-only.** The page is read-only and there is no POST. Talk
  already works in the dark with your eyes shut, which is when this gets
  answered; a text field on the page would be a second way to write the same
  rows.
- **The router needs telling that a note is a note.** The first version of
  `log_gratitude`'s description triggered on pleasant *content*, so "note that
  my sister called" became a gratitude. It now says the user must actually say
  they are grateful, and that an instruction to note something is always
  `add_note` however nice the thing is. `test_gratitude_is_not_a_note` in the
  live router set is what caught it and is what will catch it again.
- **`query` knows nothing about gratitude.** Reading the page is the only way
  to look back. Every misroute starts in the router, and a `kind='gratitude'`
  branch is surface area bought for a question nobody has asked.
- **Activity moved behind Health** to free the tab. Health's nav group was
  already "the surfaces with no home of their own", and the activity log is
  one: you want it when something you said came out wrong. **Review followed
  it** — email extraction is not currently producing proposals worth
  reviewing, and a permanent tab advertising an empty queue is worse than a
  row you can find. Six tabs, not seven: "Gratitude" wrapped its last letter
  at a seventh of the bar, so the tab reads "Grateful" while the screen is
  still called Gratitude, and the label is `lineLimit(1)` so no future one can
  wrap the whole bar taller.

## The morning brief

A 7am job summarizes the night's unread mail; asking "what've I got going on
today" answers from that plus live data.

- **Only the mail summary is stored.** `briefs` has one row a day holding one
  paragraph. The calendar, the reminders, the food about to spoil and the
  reports that landed overnight are all recomputed at the moment you ask,
  because all of them can be. Storing a whole brief at 7am would mean reciting
  a 9am standup at four in the afternoon; storing only the irreducible part
  makes staleness impossible rather than merely unlikely.
- **It is a launchd job, not a scheduler tick.** It makes a Haiku call, and
  design principle 3 keeps `scheduler/run.py` free of anything that reaches a
  model. `com.jarvis.brief` runs at 7am on `StartCalendarInterval`; generation
  is idempotent by the day, so a late run after a sleeping machine, a retry
  and a manual run cannot produce two pushes or two calls.
- **Snippets, never bodies.** It reads `email_messages`, which holds Google's
  snippet and nothing else, because `format=metadata` means Gmail never
  returns a body. The worst failure available is a dull brief, not an invented
  one.
- **The push says it is ready, not what it says.** A brief is a paragraph and
  iOS truncates it. Tapping opens Talk with the mic live — the same latch the
  gratitude prompt uses, for the opposite reason: that one is *answered* by
  talking, this one is *asked for* by talking.
- **The row and the push are separate facts, and conflating them cost a
  morning.** The row means the summary exists; a `heartbeats` row named
  `brief`, holding the day it last pushed for, means you were told. The push
  used to be reachable only from the branch that generated the row, so
  anything writing today's row early — a manual run, a `--force`, a retry
  after a failed push — left the 7am job printing `already ran today` and
  sending nothing. That is exactly what happened on 2026-08-04: a 2:33am
  development run wrote the row, and 7am had nothing left to do. `push()` now
  dedupes and stamps itself, `main()` calls it unconditionally, and nothing
  is stamped unless the push landed — the same bookkeeping in the same table
  as `gratitude.nudge`, for the same reasons.
- **It always writes a sentence, even on a quiet morning.** The first version
  let the model reply `NOTHING` when the mail was all newsletters, and on a
  real mailbox that fired immediately: 29 messages, no summary, no push, a
  feature that looked broken on day one. A quiet inbox and a dead job are
  indistinguishable from silence, so "twenty-nine messages, nearly all job
  alerts" is now the required answer. `mail.summarize` returns None only for
  an empty mailbox or a failed call.
- **The window is 24 hours, not the backlog.** 866 unread messages is a filing
  decision nobody made. One Haiku call over ~25 snippets, capped at 60.
- **This is the third metered call in the system**, after the report summary
  and receipt extraction, and like both it stays out of `/metrics` — that
  block is per-utterance and a brief has no utterance behind it.
- **`query` gains `kind='brief'`**, and an `agenda` question picks up the mail
  line too. The router can reasonably call "what's on today" either one, and
  an answer that depends on which way it went is an answer you cannot trust.

### The email review queue is dormant

`ingest.gmail`'s pass 2 no longer runs on a schedule — `--proposals` is the
only way in and nothing passes it. The table, endpoints and screen all remain;
Review sits in Health's nav group, alongside Activity and — since Projects took
the sixth tab — Reports.

Two reasons, and the second is why the code is still there. The brief now
tells you a dentist confirmation arrived and "add dentist Thursday at 3"
already works, so the accept/reject queue buys a screen rather than a
capability. And **it never worked**: `sync_proposals` applies its `LIMIT`
before intersecting with the narrow Gmail query, so `candidates()` returns the
newest hundred unexamined messages whether they match or not — four days of
newsletters at 25 messages a day. Measured on the real mailbox: 39 narrow
matches, 100 candidates, **intersection zero**. Nothing is examined, so
`examined_at` is never stamped, so the window never advances. `proposals` has
zero rows of any status and the log read `examined=0` from the day it shipped.
Re-enabling means fixing that first — filter by the narrow ids inside the SQL
so the `LIMIT` applies to matching messages.

## Projects

A named space collecting the notes, reports, dated items, links and
agent-written files belonging to one thing you are working on. Started by
voice, optionally with research attached; added to by naming it; read on a
screen or by asking where you are.

The `projects` table has existed since migration 001 and nothing ever wrote to
it. 014 gave it the columns a real project needs and hung the rest of the
domain off it.

- **Attachment is by naming, never by inference.** A PROJECTS block in the
  router prompt lists active projects as `id  name`, and every write tool takes
  an optional `project_id` drawn from it. Nothing is attached because its
  content looked related, and there is no sticky "current project" a later
  utterance inherits. The same conclusion `escalate`'s `job_id` reached when
  `is_follow_up` was deleted: naming beats guessing.
- **`add_note`'s free-text `project` is gone.** It used to create the project
  when the name was new, which meant a misheard word spawned a ghost row that
  nothing could merge away. An id the router invents rather than reads files
  the row *nowhere* — `handlers._project_ref` checks it exists. Keeping the
  note and dropping the association is the right half to lose.
- **No status is stored.** There is no "state of play" paragraph. The screen
  shows the rows and `query(kind='project')` gathers them live at the moment
  you ask. This is the brief's rule applied where there is no irreducible part
  at all: storing one would mean a metered call per note and a paragraph that
  can disagree with the rows beneath it.
- **`start_project` exists because one sentence does two things.** "Start a
  project on X and research it" is one utterance and `tool_choice: any` emits
  one call, so the tool takes an optional `research_task` and enqueues a job
  under the new project. It lives in `main.py` beside `escalate` rather than in
  `FAST_HANDLERS` — both may enqueue a job and so must shape the response
  themselves. `DEEP_TOOLS` is the set.
- **Starting a project twice is one project.** Idempotent by name, because
  voice is lossy enough without a near-duplicate per repetition. Route is
  `deep` only when research came with it; a bare creation is a fast-path write
  and reporting it as deep would have the phone poll a job that does not exist.
- **`slug` is stored, not derived from `name`.** The working directory is
  `work/projects/<id>-<slug>/` and reports quote paths inside it, so a rename
  must not move the directory. `PATCH /projects/{id}` deliberately leaves the
  slug alone.
- **`ON DELETE SET NULL` on events, reminders and jobs**, the call 007 made for
  `proposals.event_id`: deleting a project must not take a dentist appointment
  off the calendar. `notes.project_id` keeps its no-action FK, so **a project
  with notes cannot be deleted at all** — the notes are the point of it. You
  mark it done.
- **The agent reads a project and never writes to it.** `project_context` is
  read-only and there is no MCP tool that files notes or links back. Sources it
  found are in the report text. Same human-disposes rule as `proposals` and
  `receipts`.
- **A project's deep job runs in the project's own directory** and is told to
  call `project_context` first. Research started three weeks in should see the
  three weeks of thinking, not just the sentence that kicked it off. A *reply*
  gets no preamble — the session it resumes already has one.
- **Status changes are screen-only.** A twelfth router tool for a
  once-per-project action taken with your eyes open is prompt budget and
  misroute surface spent badly.
- **Links are typed, not spoken**, because you cannot say a URL. It is the one
  input on the screen, and it goes through the mutations helper like every
  other human action.
- **Projects took the sixth tab and Reports moved into Health's nav group**,
  beside Activity and Review. Every project screen lists its own reports with
  the same `ReplyBox`, so what is left on the standalone screen is the loose
  ones and the old ones. A "Job finished" push can no longer switch to a tab
  that does not exist, so `RootView` **presents** `ReportsView` — which reads
  the pending id itself and pushes straight to the report, which is what the
  old tab switch was there to do.

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

## Replying to a report

Deep reports routinely end in a question. Answering one re-queues **the same
job** against its stored `session_id`; there is one report per task however
many times you answer it.

- **The job was never blocked.** `claude -p` runs to completion and exits, so
  a report that "asks a question" is a `done` row whose `result` ends in one.
  Replying starts a second run of that row, it does not unblock a process.
- **`prompt` is immutable; `pending_input` is transient.** The worker passes
  `prompt` as `-p`, so the reply needs its own column or the original ask —
  the only record of what the report is *for* — is destroyed.
- **The resumed run is told to restate the whole report**, self-contained,
  with no reference to what it said before. That instruction is what makes
  overwriting `result` safe, and it is why there is no version history, no
  diff view, and nothing to reconcile. `worker.REPLY_WRAPPER` owns the
  wording, in the worker rather than at each call site, so all three surfaces
  produce identically-shaped runs.
- **A reply resets `attempts` to 0.** `MAX_ATTEMPTS` counts across the life of
  the row, so a job that already failed once and recovered would give your
  reply no retries at all.
- **Replying to a `queued` or `running` job is a 409.** You cannot resume a
  session mid-run, and a reply that vanished into a job already working is the
  worst failure this feature has available.
- **`result` is not cleared on reply.** You keep reading the old report while
  the rerun works; the worker overwrites it on finish.
- **Nothing detects that a report is asking you something** — no marker the
  agent must remember to emit, no classifier call on every job. Every finished
  report can be replied to. You find out by reading it, which you were doing
  anyway.
- **A spoken answer names its report.** The router's system prompt carries a
  `REPORTS` block — the last ten finished jobs as `id  original ask` — and
  `escalate` returns a `job_id`, which routes to the same
  `handlers.reply_to_job` the reply box calls. It briefly used an
  `is_follow_up` boolean resolving to "the most recent finished job", which was
  right often enough to feel fine and wrong in the case you would care about:
  answering this morning's report after asking for something else at lunch.
  Naming beats guessing, so the boolean and `resume_latest_job` were both
  deleted rather than kept alongside.
- **Answering a report that is already running says so.** `reply_to_job`
  returns `live`, and the templated reply is "That one's still working. I'll
  leave it be." Quietly starting a second piece of work you did not ask for is
  the failure this avoids.
- **`query` can read a report too.** With a `job_id` it adds one
  `REPORT (<ask>): <summary>` line beside the `NOTE:` and `EMAIL:` lines, and
  `router.answer` speaks from it — so reports are another thing the assistant
  knows about rather than a mode it enters, and one question can draw on a
  report and your mail together.
- **Voice reads the summary, not the report.** `jobs.summary` is written by one
  Haiku call in `app/reports.py` when a run finishes. A report runs to tens of
  kilobytes and would spend the whole context and latency budget on a question
  a sentence answers. **The accepted cost: a question about a detail the
  summary dropped is answered "it didn't say"** — the detail is in `result`, on
  screen. NULL is normal and permanent, not pending; `query` falls back to the
  first 1500 characters of `result`, which is why there was no backfill.
- **The summary is the one metered call in the deep path.** Everything else
  there rides the Claude Code subscription; this is Haiku against a few
  thousand tokens, billed to API credit. Fractions of a cent per job and deep
  jobs are rare, but it is a real exception to how the two tiers are funded.
  It stays out of `/metrics`, which is per-utterance, and a summary has no
  utterance behind it. It cannot fail a job: `reports.summarize` swallows its
  own errors and `worker._store_summary` swallows them again, because the
  report is already saved and the push is already owed.
- **Ten reports is the reach of voice.** Older ones are unreachable by voice
  and still repliable on the Reports screen. It is one constant,
  `handlers.recent_reports`'s `limit`.
- **An expired Claude Code session is a normal failure.** `--resume` fails, the
  run retries and lands in `failed` with the CLI's message on the detail view.
  Ask again from scratch. Tracking session lifetime we do not control is not
  worth it.

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
| `GET /gratitude` | Today's three, the streak, and the days behind it |
| `GET /projects` | Every project with its counts and last activity |
| `GET /projects/{id}` | One project: notes, reports, dated items, links, files |
| `GET /projects/{id}/files/{name}` | One artifact the agent wrote. Escapes are 404 |
| `POST /projects/{id}/links` | Paste a URL. Through `mutations` — a human tapped it |
| `PATCH /projects/{id}` | Rename, or move between active/paused/done. Slug unchanged |
| `GET /jobs` | Deep-path history (results truncated; full text on `/jobs/{id}`) |
| `POST /jobs/{id}/reply` | Answer a report that asked you something; resumes it in place |
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
deep path runs on the Claude Code subscription, not API credits — with one
exception, the per-report summary call described under "Replying to a
report".

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
| `add_event` | `title`, `starts_at`, `ends_at?`, `location?`, `all_day?`, `project_id?` |
| `add_reminder` | `body`, `fire_at`, `recurrence?`, `project_id?` |
| `add_note` | `body`, `tags?`, `project_id?`, `person?` |
| `start_project` | `name`, `description?`, `research_task?` — deep when research came with it |
| `log_gratitude` | `items[]` |
| `query` | `question`, `kind`, `window_days?`, `job_id?`, `project_id?` |
| `answer` | `reply` — spoken verbatim. The one tool that talks to the user |
| `undo_last` | — |
| `escalate` | `restated_task`, `job_id?`, `project_id?` — routes to the deep path |

`project_id` is one shared property object referenced by every tool that can
file something, not five copies: they must read identically, and a description
that drifts on one tool is a routing bug you find months later in exactly one
phrasing.

The system prompt must include current datetime with offset, timezone name,
day of week, and an instruction to resolve all relative times to absolute
ISO 8601.

**Confirmations are templated, not generated.** Once you have
`add_reminder(body=..., fire_at=...)`, format the reply in Python.
Deterministic, and it saves a round trip.

**The router now talks, for exactly one tool, and this reverses an earlier
rule.** It used to be told "you never talk to the user — you only choose a
tool", and `answer` breaks that deliberately. The reason is measured: a
question about today cost `route` and then `router.answer`, two *sequential*
Haiku calls, and a Haiku call has a **~660ms floor** that is neither network
(18ms TCP+TLS to the API) nor prefill nor generation. Two round trips was the
whole of a 3-second answer. Since the router already has the day in its
prompt, the second call was buying a rephrasing of context it had also been
given.

- **`TODAY` is question-independent, and that is what makes it safe.** It is
  built before the utterance is read — the stored mail summary, the live
  agenda, and `_needs_doing` — so there is no search over the user's words in
  it. Anything needing a note, an email, a project or an old report still
  routes to `query`, which searches and then makes the second call. The
  prompt says so twice, because the failure mode is answering "what did I say
  about the fence" out of a block that never contained it.
- **`handlers.agenda_lines` is shared by `query` and `today_block`** so the
  router and the answering model are shown the day in identical words. Two
  formatters would drift, and the drift would surface as an answer that
  changed depending on which path it took — the thing the unified
  brief/agenda context was built to prevent in the first place.
- **The reply is squeezed to one line.** `/say` promises a single plain-text
  string safe to hand to a TTS engine, and a newline in a tool argument is a
  pause that is not in the sentence.
- Measured over 12 utterances spanning day questions, archive questions and
  writes: **zero misroutes**, and a brief question went from ~2900ms to
  ~1400ms. `query` keeps its templated shortcuts, which were already one
  call.

## Local facts that are easy to get wrong

- **`mcp.json`'s `cwd` key is ignored.** Claude Code spawns the MCP server with
  the *parent's* working directory, which for a job is `WORK_DIR`, not the repo.
  `python -m mcp_server.server` therefore needs `env.PYTHONPATH` pointing at the
  repo; without it the server dies on ModuleNotFoundError. The failure is
  silent — the CLI reports no MCP error and omits `mcp_servers` from its JSON,
  so the agent just has no tools and the job *succeeds*, answering that it has
  no way to search your mail. Covered by
  `test_mcp_server_starts_from_outside_the_repo`.
- **Prompt caching fires now, and what made it work was reordering the prompt,
  not growing it.** The cache prefix is ordered tools, then system, then
  messages, so everything up to the `cache_control` marker must be byte-stable.
  The system prompt used to carry the datetime on its third line, which left
  the tools as the only candidate — and the tools alone are **4378** tokens
  against Haiku 4.5's documented **4096** floor, which reads like it should
  work. **Probed directly, it did not**: two identical requests both reported
  zero on both counters, while the same probe padded to 7240 tokens cached
  6912 immediately.

  Splitting the prompt fixed it. `_SYSTEM_STATIC` holds the rules and carries
  the marker; `_SYSTEM_LIVE` holds the clock, the calendar table, `TODAY`,
  `REPORTS` and `PROJECTS`. The prefix measures **5253** tokens by
  `count_tokens` and the cache reports **4929** written and then read back on
  every subsequent call, against ~1048 uncached in the live tail. So the real
  threshold sits somewhere between 4199 and 4676, and `count_tokens` is not
  the number that decides it.

  **What this bought is spend, not speed.** Measured: 1229ms → 1153ms median,
  inside the noise, because a Haiku call has a ~660ms floor that prefill is a
  small share of. The economics: the median gap between real utterances is
  340s, so a 5-minute TTL reads the cache 48% of the time (0.69x of uncached,
  after the 1.25x writes) and a 1-hour TTL 73% (0.60x, after 2x writes).

  **A `cache_control` marker on a prefix below the minimum is a silent
  no-op** — no error, both counters zero, and it reads as a working
  optimization forever. `tests/test_router_prompt.py` therefore asserts the
  *behaviour*: declare `cache_control` and it must produce a real cache read.
  A second test asserts the cached block is byte-identical across two
  different timezones, report lists, project lists and days — one live value
  leaking into it kills caching silently and permanently.
- **The Anthropic client sets its own httpx keepalive.** httpx expires idle
  connections after 5s by default and the SDK does not override it, so an
  assistant spoken to every few minutes would re-handshake on every request.
  Measured from the Mini that is only ~15ms, which is why this is one line in
  `_client()` and not a subsystem.
- **The fast path is the model call, essentially all of it, and the unit to
  optimize is the *number of calls*.** The budget, measured per hop:

  | | |
  | :-- | :-- |
  | TCP+TLS to `api.anthropic.com` | 18ms |
  | `today_block` + the other SQLite reads | ~3ms |
  | One Haiku call, `max_tokens=1` | **660ms** |
  | …generating a full sentence on top | +50ms |
  | …prefill, 30 → 8730 input tokens | ~400ms, and non-monotonic |

  So a call costs ~660ms before it does anything, the network is a rounding
  error, the database is free, and **tokens barely matter** — 290x the input
  bought only ~400ms, inside the run-to-run variance. This is why shrinking
  prompts and shrinking context are not latency work: they are cost work that
  sometimes shows up as noise. Two sequential calls was the entire difference
  between a 1.4s answer and a 3s one, which is what the `answer` tool exists
  to remove. Beware measuring any of this with n=3; the spread on a single
  call runs 600–2400ms.
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
