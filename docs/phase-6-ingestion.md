# Phase 6 — Ingestion

What turns Jarvis from a notepad into something that knows your life. Like
`CLAUDE.md` and the other phase docs, this file exists so settled decisions
stay settled. If something here seems wrong, say so — don't silently do it
differently.

**The original plan said "Calendar first, and do it locally" via EventKit.
That is void.** It assumed Calendar.app on the Mini as the source of truth.
Everything is Google, so the source of truth is Google's API and this phase is
network-shaped rather than local-shaped. §2 is the consequence.

---

## 1. What gets ingested, in order

One source at a time, read-only, and each one proves dedupe before the next
starts. That ordering is about dependency, not soak time — there is no waiting
period here.

| Order | Source | Into | Why this order |
| :-- | :-- | :-- | :-- |
| 1 | **Google Calendar** | `events` | Highest value, cleanest data, no extraction step. Structured events in, structured events out |
| 2 | **Gmail** | `proposals` → review → `events` | Real value, but extraction is lossy. Never lands directly |
| 3 | **Morning brief** | push + app | Consumes the first two. Pointless before them |
| — | ~~Location~~ | — | Cut. "High creep factor for modest benefit" was right |

Location is not deferred, it is **declined**. Continuous location history for a
marginal scheduling improvement is a bad trade, and having the file say so
stops it being revisited every few months.

---

## 2. Auth — the part that will bite

This is the highest-risk decision in the phase and it is not about code.

**One OAuth client, both scopes, `calendar.readonly` and `gmail.readonly`.**
One credential, one refresh path, one thing to re-authorize.

### The trap: refresh tokens expire in 7 days in Testing mode

A Google Cloud OAuth consent screen left in **Testing** publishing status
issues refresh tokens that **expire after 7 days**. Nothing warns you. What
you get is an assistant that ingests perfectly for a week and then quietly
stops, which is the exact failure shape this project keeps designing against —
`reminders silently stop firing`, now with a calendar.

**Set the publishing status to "In production" before generating the first
refresh token.** It stays unverified, you click through Google's own
"unverified app" warning as the developer, and the refresh token persists.

Verify this rather than trusting the doc: authorize, record the token, and
have the ingester log loudly on a refresh failure rather than treating it as a
transient network error. A 7-day silence is much easier to catch if day one
says out loud what it expects.

### The escape hatch, if Google's posture on restricted scopes changes

`gmail.readonly` is a *restricted* scope, and Google's requirements for
unverified apps using restricted scopes have moved before. If it becomes
untenable, both sources have a no-OAuth fallback:

- **Calendar** — the private "secret address in iCal format" URL. No auth, no
  expiry, and it fits the ntfy pattern already in use here: the URL *is* the
  credential, so treat it as a password. The cost is no incremental sync and
  Google caching the `.ics`, so changes can lag by hours. That lag is why it
  is the fallback and not the plan.
- **Gmail** — IMAP with an App Password. Requires 2FA on the account. No token
  refresh at all.

Do not build both paths. Build OAuth; write down that this exists.

### Running the spike

Built: `ingest/google_auth.py`. Three things to do in
[console.cloud.google.com](https://console.cloud.google.com), then two
commands.

> **The console moved.** What used to be "APIs & Services → OAuth consent
> screen" is now **Google Auth Platform**, split into *Branding*, *Audience*
> and *Clients*. Publishing status lives under **Audience**. The steps below
> use the current names; older write-ups of this same procedure will not match
> what you see.

1. **New project** (any name). Then **APIs & Services → Library** and enable
   **Google Calendar API** and **Gmail API**. Ingestion fails with a
   confusing 403 if the API is off, rather than saying so. Both are needed —
   `--check` probes each one precisely because enabling one and forgetting the
   other is easy and its symptom is opaque.

2. **Google Auth Platform → Audience.** User type *External* (a personal
   `@gmail.com` account has no Workspace to be Internal to). Then — the step
   this whole section is about — **Publishing status → Publish app → In
   production.** It stays unverified and shows a warning screen you click
   through; that is expected and fine for a single-user app.

3. **Google Auth Platform → Clients → Create client → Desktop app.** Copy the
   client ID and secret into `.env` as `GOOGLE_CLIENT_ID` /
   `GOOGLE_CLIENT_SECRET`. Desktop clients may redirect to any loopback port,
   so `http://127.0.0.1:8765/` needs no registering.

**Leave every checkbox ticked on the consent screen.** Google renders each
scope separately and a half-awake click can untick one. `--authorize` now
refuses a partial grant rather than storing it, because the resulting
credential stores, refreshes and looks healthy, then 403s the first time the
ingester it belongs to runs.

Then, on the Mini (it opens a browser):

    uv run python -m ingest.google_auth --authorize

The refresh token lands in `~/Library/Application Support/jarvis/google_token.json`
at mode 600 — beside the database and the APNs `.p8`, never in the repo.

**Then put a reminder in Jarvis for eight days out and run:**

    uv run python -m ingest.google_auth --check

It forces a real refresh rather than reusing a cached access token — a cached
one would pass for an hour after the credential died — then reads your calendar
list **and** your Gmail profile. Both, because checking one and declaring the
credential healthy is how day 8 goes green while Gmail is dead. Exit 0 means
the credential survived. Exit 1 on day 8 means the publishing status didn't
take.

### One thing the Google path buys for free

The EventKit design had a real problem: Calendar access is TCC-protected and
prompts per-user through the GUI, so a root LaunchDaemon could never obtain
it. The importer would have had to be a LaunchAgent — different from every
other Jarvis process, and interacting badly with the FileVault constraint in
`CLAUDE.md`. **Google's APIs are ordinary network calls, so that problem
disappears** and the ingester is a LaunchDaemon like the scheduler and worker.

---

## 3. Calendar

### Sync, not polling

Use `syncToken` incremental sync. The first run is a full fetch; every run
after asks only for what changed. This matters for more than efficiency —
incremental sync is the only way to hear about **deletions**, which arrive as
entries with `status: "cancelled"`.

A cancelled meeting that lingers in the agenda is worse than one that was
never imported, because you will plan around it.

    events.source      = 'calendar'
    events.external_id = the Google event id

`idx_events_ext` — the partial unique index on `(source, external_id)` — has
been in the schema since `001_init.sql` waiting for exactly this. Voice
captures keep their NULL `external_id` and are unaffected. Nothing to migrate
for dedupe.

Expand recurring events (`singleEvents=true`) so each occurrence arrives with
its own id and lands as its own row. Storing the series and expanding at read
time would mean reimplementing RRULE in `/agenda`, and the scheduler's
recurrence support is deliberately limited to `daily` / `weekly:MO,WE`.

### New state to track

    sync_state(source TEXT PRIMARY KEY, token TEXT, last_run_at TEXT, detail TEXT)

Same shape as `heartbeats`, same purpose: an ingester that stops working
should be a *detectable* condition, not something noticed a week later.

### Synced writes bypass the mutations helper

This is an exception to an invariant `CLAUDE.md` states flatly ("every write
goes through one helper"), so it is written down rather than discovered.

The mutations log exists to make **voice** input reversible, because voice is
lossy and you will mis-hear things. A calendar import is not a user action:
there is nothing to regret, and `/undo` on a synced row is meaningless because
the next sync re-adds it. Worse, routing a few hundred rows per sync through
the log would bury your last real action under them and make `/undo` useless
for exactly the thing it was built for.

So: `source='calendar'` writes go direct. Voice writes keep the helper.
Anything a human *accepts* — a Gmail proposal — goes through the helper,
because that one is a user action.

### Details that are easy to get wrong

- **`showDeleted=true`.** Without it, cancellations are omitted entirely and
  deleted meetings stay in the agenda forever. It is the whole reason for
  incremental sync.
- **Save `nextSyncToken` only after pagination completes.** Only the final
  page carries it; saving early skips every event on the pages not yet read.
- **`timeMin` cannot be combined with `syncToken`** — Google rejects the
  request. The window is baked into the token by the first sync.
- **HTTP 410 is normal, not a crash.** Google expires sync tokens on its own
  schedule. Treat it as "drop the cursor and refetch in full"; an ingester
  that treats it as fatal stops permanently after a quiet week.
- **All-day events arrive as a bare `YYYY-MM-DD`** with no offset. Anchor them
  to local midnight in `DEFAULT_TZ` before storing — a naive timestamp
  violates the schema's rule and sorts wrongly against everything else.
- **Clear `deleted_at` on update.** An event can be cancelled and then
  restored; leaving it soft-deleted hides a meeting that is back on.

### What it must not do

**Read-only, one direction, first release.** Voice-created events stay
`source='voice'` and are not pushed to Google. Two-way sync is a much larger
problem — conflict resolution, echo suppression, and a bug that deletes real
calendar entries — and none of it is needed to make `/agenda` useful.

---

## 4. Gmail

### Two passes, not one

**This section originally specified one output — `proposals` — and that was
half the story.** Proposals answer "should this become a calendar entry?". They
do not answer "did the landlord ever email me back?", which is the question
that actually makes an assistant feel like it knows your life. So Gmail is read
twice, with deliberately different risk profiles:

| Pass | Output | Model | Cost | Can reach `events`? |
| :-- | :-- | :-- | :-- | :-- |
| **Context** | `email_messages` | none | free | **No** |
| **Proposals** | `proposals` | Haiku, capped | ~$0.10/run ceiling | Only via a human |

The context pass stores metadata and Google's own `snippet` — never bodies.
`format=metadata` is what makes that structural rather than a promise: Gmail
does not return the body at all, so there is no path to storing one by
accident. A snippet is ~200 characters Google already computed, so there is no
extraction step and nothing invented. The assistant can quote what arrived; it
cannot embroider it.

That is the whole safety argument, and it is worth being explicit about why it
differs from the proposals rule below. The danger of email is not that the
assistant *knows* about it — it is that a model turns a marketing email into a
dentist appointment and puts it on the calendar. Reading is safe. Writing is
what needs a human.

`handlers.search_email` feeds `query`, and the MCP server exposes it so the
deep path sees mail too. One wrinkle worth keeping: when a question matches
both a note and an email, the templated note answer is *suppressed* and both go
to the model. "Did Sarah email me?" asked by someone who also has a note
mentioning Sarah must not come back as "You noted: …".

### Narrow by query, not by model

For the proposals pass, the extraction step is expensive and noisy, so the
query does the filtering first: flights, appointments, deliveries,
reservations. A broad query that leans on the model to reject irrelevant mail
costs a Haiku call per message and is worse at it.

Track `historyId` in `sync_state` for incremental fetch. Gmail expires history
IDs on its own schedule and answers **404** when it has — routine, and handled
exactly like Calendar's 410: drop the cursor, refetch.

`email_messages.examined_at` is what stops the extractor paying twice. Without
it, every run re-pays a Haiku call for the same marketing email that matched
the query and yielded nothing, forever.

### Proposals, never direct writes

    proposals(
      id, source, external_id, kind,
      payload_json,          -- the extracted event, unvalidated
      confidence,
      status,                -- pending | accepted | rejected
      created_at, decided_at
    )

Extraction writes here. **Nothing reaches `events` without a human accept**,
and acceptance goes through the mutations helper like every other write, so it
is logged and undoable.

This is the one place the plan is emphatic and it is right: an assistant that
invents a dentist appointment from a marketing email is worse than one that
knows nothing about your email. The failure is not that it is wrong
occasionally — it is that you stop trusting the agenda, and then the whole
system is decoration.

The dashboard gets a Review tab. A proposal you never look at stays pending
forever, which is the correct default.

### Cost

One Haiku call per candidate message, and unlike `/say` there is no human
waiting, so batch and cap it. Today's whole-day spend was under two cents; an
uncapped inbox sweep could dwarf that.

**Two ceilings, both wanted** (`ingest/gmail.py`):

    MAX_EXTRACTIONS_PER_RUN = 25       # bounds the work
    MAX_SPEND_USD_PER_RUN   = 0.10     # bounds the damage

The count assumes messages are small. A forwarded thread with fifty quoted
replies breaks that assumption and the count would not notice, so the dollar
figure is checked against the live `usage` tally between messages and stops the
run early. Whatever is left is picked up next run — the daemon fires every 30
minutes, so nothing is lost, only delayed.

Worst-case daily spend is therefore `48 × $0.10`, and being able to compute
that number is the point of having the second ceiling at all.

---

## 5. Morning brief

A scheduled push assembling the day: events, reminders, anything pending in
proposals.

**Templated first, model second.** `agenda_rows` and `speak_datetime` already
produce good spoken output, and principle 3 says the scheduler must work when
the agent is broken. A brief that fails because Haiku is down is a brief that
fails on exactly the morning you needed it.

Once the templated version works, an optional Haiku pass can make it read
better — with the templated string as the fallback, not as a thing that was
replaced.

Delivered by APNs, so it can carry actions the way fired reminders already do.

---

## 6. Work

- [x] **Auth spike.** `ingest/google_auth.py` — loopback + PKCE, refresh, and
      a `--check` that forces a real refresh against *both* APIs. Hardened
      since the spike: atomic token writes, partial-grant detection, a clear
      error on a corrupt token file. Console steps in §2.
- [ ] **Run `--authorize`, then `--check` on day 8.** This is the gate, and it
      is the one thing here that needs a human. Everything below is written and
      tested offline, but **none of it has run against Google.**
- [x] `migrations/005_ingest.sql` — `sync_state`, `proposals`.
- [x] `migrations/006_email.sql` — `email_messages`, `email_fts`.
- [x] `migrations/007_proposal_event_fk.sql` — `event_id ON DELETE SET NULL`,
      without which accepting a proposal was not actually undoable.
- [x] `ingest/client.py` — one HTTP path, retries, typed `ApiError` so 410/404
      are routine rather than fatal.
- [x] `ingest/state.py` — `sync_state` reads and writes.
- [x] `ingest/calendar.py` — full fetch, then `syncToken`; handles `cancelled`,
      410 recovery, all-day anchoring, per-calendar cursors.
- [x] `deploy/com.jarvis.calendar.plist` (15 min) and `com.jarvis.gmail.plist`
      (30 min), same shape as the scheduler. Added to `install-daemon.sh`.
- [x] `ingest/gmail.py` — context pass plus extraction into `proposals`, with
      both spend ceilings.
- [x] `handlers.search_email` + the `query` context builder + an MCP tool, so
      mail reaches both the fast and the deep path.
- [x] `GET /proposals`, `POST /proposals/{id}/accept|reject` — accept goes
      through `mutations`. `GET /inbox`. `/health` reports ingest staleness.
- [ ] Review tab in the app. The endpoints are ready; the Swift is not written.
- [ ] Morning brief, templated.

---

## 7. Risks

| Risk | Mitigation |
| :-- | :-- |
| Refresh token dies at 7 days, ingestion stops silently | Production publishing status; loud failure on refresh; `--check` on day 8; `/health` reports `sync_state` staleness |
| A scope is silently unticked on the consent screen | `--authorize` refuses a partial grant; `--check` re-verifies and probes both APIs |
| The refresh token is destroyed by a crash mid-write | Atomic save: temp file at 0600 plus `os.replace` |
| Google tightens unverified restricted-scope access | Documented fallback: iCal URL + IMAP app password (§2) |
| Cancelled meetings linger in the agenda | Incremental sync only; handle `status: "cancelled"` as a soft delete |
| The same meeting on two calendars flaps between copies | `external_id` is `{calendarId}:{eventId}`, so the two rows cannot collide |
| Email extraction pollutes the agenda | Proposals table; no path from extraction to `events` without a human |
| Email *context* pollutes the agenda | It structurally cannot — `email_messages` is not a domain table and is never read as one |
| Recurring events explode the table | `singleEvents=true`, bounded window, and re-fetch rather than store rules |
| Uncapped Haiku spend on an inbox sweep | Two ceilings: message count and dollars, checked against the live tally |
| One broken calendar stops the others | Per-calendar cursors and error isolation in `sync()` |
