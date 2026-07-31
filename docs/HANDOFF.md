# Handoff — 2026-07-31

State of the project, what needs a human, and what to build next. Written for
whoever (or whatever) picks this up cold.

Read `CLAUDE.md` first — it holds the settled decisions. This file holds the
*current position*. The phase docs (`docs/phase-6-ingestion.md`,
`phase-7-ios.md`, `phase-8-voice.md`) hold the reasoning per phase.

---

## 1. Where things stand

| Phase | State |
| :-- | :-- |
| 0–3 — foundations, fast path, scheduler, deep path | Shipped, in daily use |
| 4 — local speech / desktop | **Superseded.** iOS gave STT (7a); TTS became Phase 8 |
| 5 — dashboard | **Absorbed into 7d.** Do not build the HTMX version |
| 6 — ingestion | Server side **built and tested offline.** Never run against Google — still blocked on a human (§2.1) |
| 7 — iOS app | a/b/c/d complete. Tested on hardware; icon landed after that test |
| 8 — voice | Specced only. Not started |

Branch `phase-7-ios` is pushed to `github.com/jaxgardner/jarvis` (private),
five commits ahead of `master`. Working tree clean.

### Verified

- 231 offline Python tests; 13 iOS contract tests; iOS Debug + Release build clean.
- APNs end to end on real hardware: a fired reminder pushed, Snooze tapped
  from the lock screen, row requeued 10 minutes out, mutation logged, device
  authenticated with its own per-device token without the app opening.
- Server endpoints exercised against a live scratch server with curl.

### Not verified

- **The dashboard screens have never rendered with live data.** They compile
  and their decoders are pinned against real captured JSON, but no screen has
  been watched drawing. (Keychain doesn't persist across processes on an
  unsigned simulator build, so this needs the device.)
- **The app icon** landed after the last hardware test. Needs a rebuild.
- **Kokoro** has never been run on this machine. Phase 8's first task is a
  benchmark, not an implementation.
- **No part of Phase 6 has touched Google.** Both ingesters, the OAuth
  hardening, the review queue and the new endpoints are covered by offline
  tests against captured response shapes — which pins the logic, not the
  assumption that Google's responses look the way I think they do. §2.1 is the
  gate, and it is unchanged: nothing here is proven until `--check` exits 0.

---

## 2. What needs a human

### 2.1 Google auth — this gates all of Phase 6

**Everything else in Phase 6 is now written. This is the only thing standing
between you and a working calendar.** Ten minutes in
[console.cloud.google.com](https://console.cloud.google.com), then two
commands. Full steps in `docs/phase-6-ingestion.md` §2.

1. New project → **APIs & Services → Library** → enable **Google Calendar
   API** and **Gmail API**. Both — `--check` probes each, because enabling one
   and forgetting the other fails as an opaque 403.
2. **Google Auth Platform → Audience** → External → **Publishing status →
   Publish app → In production**.
3. **Google Auth Platform → Clients → Create client → Desktop app** → put the
   ID and secret in `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

> The console was reorganized: what older notes call "APIs & Services → OAuth
> consent screen" is now **Google Auth Platform**, and publishing status lives
> under *Audience*.

> **Step 2 is the whole point.** Left in *Testing*, Google issues refresh
> tokens that expire after **seven days**, silently, with no error until
> they're used. That yields an assistant that ingests perfectly for a week and
> then stops. Production status keeps the app unverified — you click through a
> warning — but the token persists.

Then, on the Mini (opens a browser). **Leave every scope checkbox ticked** —
`--authorize` now refuses a partial grant rather than storing a credential that
looks healthy and 403s a week later:

    uv run python -m ingest.google_auth --authorize
    uv run migrate.py                      # 006 and 007 are new

Then a first sync, by hand, before letting launchd near it:

    uv run python -m ingest.calendar          # then --status
    uv run python -m ingest.gmail --context   # free: no model calls
    uv run python -m ingest.gmail             # adds the capped extractor
    sudo ./deploy/install-daemon.sh           # installs the two new daemons

**Then ask Jarvis to remind you in eight days to run:**

    uv run python -m ingest.google_auth --check

Exit 0 = the credential survived. Exit 1 = the publishing status didn't take.

### 2.2 Rebuild the app for the icon

The waveform icon landed after the last device install. ⌘R in Xcode.

### 2.3 TestFlight expiry — still not done

The one open item from Phase 7 §3. **Builds expire 90 days from upload.** Add
a `fastlane beta` lane so re-upload is one command, and a recurring Jarvis
reminder at 80 days. The assistant should be the thing that keeps the
assistant alive.

### 2.4 Restart the API (minor)

The running process predates the Phase 6 commit — `/health` doesn't report the
`google` key yet. Nothing is broken; it just isn't current.

    sudo launchctl kickstart -k system/com.jarvis.api

### 2.5 Decide what to do with the branch

`phase-7-ios` is five commits of work. Merge to `master`, or open a PR and
review first. Nothing depends on the answer.

---

## 3. What to build next

**In order**, because each depends on the last — not because anything
needs to soak. Build as fast as it goes.

### 3.1 ~~Calendar ingester~~ — built

`ingest/calendar.py`, `ingest/client.py`, `ingest/state.py`. Full fetch then
`syncToken`, `singleEvents=true`, `showDeleted=true`, 410 recovery, all-day
anchoring, per-calendar cursors, error isolation so one revoked shared calendar
doesn't stop your own. Every selected calendar syncs, plus primary
unconditionally. 26 offline tests.

One decision worth knowing about: `external_id` is `{calendarId}:{eventId}`,
not the bare event id. Google reuses an event's id across every calendar it
appears on, so the bare id would collide on `idx_events_ext` and the row would
flap between two calendars' copies on every sync. The cost is that a meeting on
two synced calendars shows twice. Visible and mildly annoying beats invisible
and wrong.

### 3.2 ~~Gmail~~ — built, with an addition to the spec

`ingest/gmail.py`, two passes. The proposals queue is as §4 specified. The
addition is a **context pass**: metadata and Google's snippet into
`email_messages`, feeding `handlers.search_email` → `query` and an MCP tool.

That is a deliberate deviation, written up in §4 of the phase doc. §4 as
written would have left the assistant unable to answer "did the landlord email
me back?" — proposals only decide what becomes a calendar entry. Bodies are
never stored (`format=metadata` makes that structural, not a promise), and
nothing from the context pass can reach `events`.

Still to do: **the Review tab in the app.** `GET /proposals` and
`POST /proposals/{id}/accept|reject` are built and tested; the Swift is not
written. Accept goes through `mutations`, so it is undoable — which took
migration `007`, because `proposals.event_id` defaulted to RESTRICT and `/undo`
hard-deletes.

### 3.3 Morning brief

Spec: §5. **Templated first, model second.** `agenda_rows` and
`speak_datetime` already produce good spoken output, and principle 3 says the
scheduler works when the agent is broken. A brief that fails because Haiku is
down fails on exactly the morning you needed it. Deliver by APNs so it can
carry actions.

### 3.4 Phase 8 — voice

Spec: `docs/phase-8-voice.md`. **First task is a benchmark, not code:**
Kokoro's real-time factor on the M4 for a ~15-word reply decides whether audio
is generated inline or streamed. Guessing wrong gives either a sluggish
assistant or a pointless streaming implementation.

`AVSpeechSynthesizer` stays forever as the timeout fallback. A voice upgrade
must not become a new way for the assistant to go silent.

### 3.5 Optional

SSE on the deep path so the app shows job progress instead of polling. The
polling in `JobsView` is fine; this is polish.

---

## 4. Traps — things that will bite

Each of these cost real time to find. They are all load-bearing.

| | |
| :-- | :-- |
| **OAuth Testing status** | Refresh tokens die at 7 days, silently. `--check` on day 8 is the gate |
| **A silently unticked scope** | Google's consent screen is per-scope checkboxes. A partial grant stores, refreshes and looks healthy, then 403s only when that ingester runs. `--authorize` refuses it now |
| **Google event ids are shared across calendars** | The same meeting on primary and a shared calendar carries the same id. Keyed on the bare id, the two rows collide on `idx_events_ext` and flap forever. Hence `{calendarId}:{eventId}` |
| **Sync cursors expire on Google's schedule** | Calendar 410, Gmail 404. Both routine — drop the cursor, refetch. Treated as fatal, the ingester stops permanently after a quiet week |
| **`timeMin` + `syncToken` is rejected** | Google refuses the combination outright. The window is baked into the cursor by the first sync |
| **`nextSyncToken` is only on the last page** | Saving it early skips every event on the unread pages — permanently, because the next run starts after them |
| **All-day events have no offset at all** | A bare `YYYY-MM-DD`. Stored verbatim it breaks the timestamp rule and sorts wrongly against everything else |
| **`proposals.event_id` was RESTRICT** | Accepting a proposal is undoable by design, and `/undo` reverses an insert with a *hard* delete — which the default foreign key refuses. Migration `007` makes it `ON DELETE SET NULL` |
| **`idx_proposals_ext` does not stop re-proposing** | It excludes rejected rows, so a rejected message can be proposed again. `ingest.gmail.candidates()` is what enforces the rule, not the index |
| **`notify.push()` returns bool, never raises** | `scheduler/run.py` calls it in a loop over due reminders; an exception takes out every *other* reminder in the same tick |
| **APNs environment must match the entitlement** | `APS_ENVIRONMENT` is per-configuration; the app reports `sandbox` on `#if DEBUG`. Hardcoding `production` is the classic works-on-TestFlight-fails-from-Xcode bug |
| **Snooze drops the recurrence rule** | The scheduler inserts the next occurrence at fire time. A snoozed row keeping its rule would insert a *second* one and a daily reminder becomes two |
| **Only the newest mutation is undoable** | `/undo` reverses the most recent non-undone one and nothing else. Offering the swipe elsewhere silently reverses something the user isn't looking at |
| **Zero registered devices → APNs push returns False** | Deliberate. Reporting delivery of a push that went nowhere is the failure this subsystem exists to make loud |
| **Prompt caching does not fire** | Haiku 4.5's minimum cacheable prefix is 4096 tokens; the router request measures 2557. Verified, not assumed. If the prompt grows past 4096 it starts caching and gets *cheaper* |
| **Soft-deleted notes stay in the FTS index** | Soft delete fires the *update* trigger, not the delete trigger. Search must join `notes` and filter `deleted_at IS NULL` |
| **iOS fixtures must be captured, not written** | `ContractTests` decodes real server responses. It caught `result_truncated` returning **null**, not 0 (SQLite: `length(NULL) > 280` is NULL) — an invented fixture would have said 0 and thrown on the first failed job |
| **Test fixtures glob migrations** | A hardcoded list keeps passing while silently testing an old schema |
| **FileVault is on** | No unattended reboot recovery. `sudo fdesetup authrestart` for planned reboots |

---

## 5. How to verify anything

    # Python — offline, always safe, ~30s
    uv run pytest tests/ -q --ignore=tests/test_utterances.py

    # Ingestion state, without touching Google
    uv run python -m ingest.calendar --status
    uv run python -m ingest.gmail --status
    curl -s localhost:8000/health -H "Authorization: Bearer $JARVIS_TOKEN" | jq .ingest

    # Python — live Haiku, costs a few cents, re-run after router changes
    uv run pytest tests/test_utterances.py -v

    # iOS — build and contract tests
    cd ios && xcodebuild test -project Jarvis.xcodeproj -scheme Jarvis \
      -destination 'platform=iOS Simulator,name=iPhone 17 Pro'

    # Re-capture an iOS fixture after changing an endpoint
    curl -s localhost:8000/activity -H "Authorization: Bearer $JARVIS_TOKEN" \
      > ios/JarvisTests/Fixtures/activity.json

    # What it's costing
    curl -s "localhost:8000/metrics?days=7" -H "Authorization: Bearer $JARVIS_TOKEN"

**A note on spend.** Roughly $1.40 to date, and the vast majority is
`tests/test_utterances.py` — it runs live against Haiku and writes to a temp
database, so those calls cost money and leave no trace in `jarvis.db`. Real
usage is about $0.003 per utterance. Don't diagnose a cost spike from the
utterances table alone.
