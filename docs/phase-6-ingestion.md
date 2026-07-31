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
starts.

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

### What it must not do

**Read-only, one direction, first release.** Voice-created events stay
`source='voice'` and are not pushed to Google. Two-way sync is a much larger
problem — conflict resolution, echo suppression, and a bug that deletes real
calendar entries — and none of it is needed to make `/agenda` useful.

---

## 4. Gmail

### Narrow by query, not by model

The extraction step is expensive and noisy, so the query does the filtering
first: flights, appointments, deliveries, reservations. A broad query that
leans on the model to reject irrelevant mail costs a Haiku call per message
and is worse at it.

Track `historyId` in `sync_state` for incremental fetch.

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
uncapped inbox sweep could dwarf that. `model_calls` and the token columns
already record it — put a hard per-run ceiling in config and log against it.

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

- [ ] **Auth spike first.** Google Cloud project, consent screen **in
      production**, one refresh token, and prove it still works on day 8.
      Everything else is wasted if this is wrong.
- [ ] `migrations/005_ingest.sql` — `sync_state`, `proposals`.
- [ ] `ingest/calendar.py` — full fetch, then `syncToken`; handle `cancelled`.
- [ ] launchd plist, same shape as the scheduler; heartbeat so silence is
      detectable.
- [ ] Two weeks of calendar-only before touching Gmail.
- [ ] `ingest/gmail.py` + extraction into `proposals`, with a spend ceiling.
- [ ] Review tab in the app; accept goes through `mutations`.
- [ ] Morning brief, templated.

---

## 7. Risks

| Risk | Mitigation |
| :-- | :-- |
| Refresh token dies at 7 days, ingestion stops silently | Production publishing status; loud failure on refresh; heartbeat in `sync_state` |
| Google tightens unverified restricted-scope access | Documented fallback: iCal URL + IMAP app password (§2) |
| Cancelled meetings linger in the agenda | Incremental sync only; handle `status: "cancelled"` as a soft delete |
| Email extraction pollutes the agenda | Proposals table; no path from extraction to `events` without a human |
| Recurring events explode the table | `singleEvents=true`, bounded window, and re-fetch rather than store rules |
| Uncapped Haiku spend on an inbox sweep | Per-run ceiling in config, measured against the token columns |
