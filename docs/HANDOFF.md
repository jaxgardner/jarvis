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
| 6 — ingestion | Planned + auth spike built. **Blocked on a human** (§2.1) |
| 7 — iOS app | a/b/c/d complete. Tested on hardware; icon landed after that test |
| 8 — voice | Specced only. Not started |

Branch `phase-7-ios` is pushed to `github.com/jaxgardner/jarvis` (private),
five commits ahead of `master`. Working tree clean.

### Verified

- 136 offline Python tests; 13 iOS contract tests; iOS Debug + Release build clean.
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

---

## 2. What needs a human

### 2.1 Google auth — this gates all of Phase 6

Ten minutes in [console.cloud.google.com](https://console.cloud.google.com),
then two commands. Full steps in `docs/phase-6-ingestion.md` §2.

1. New project → **APIs & Services → Library** → enable **Google Calendar
   API** and **Gmail API**.
2. **OAuth consent screen** → External → add yourself as a test user →
   **Publishing status → Publish app → In production**.
3. **Credentials → OAuth client ID → Desktop app** → put the ID and secret in
   `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.

> **Step 2 is the whole point.** Left in *Testing*, Google issues refresh
> tokens that expire after **seven days**, silently, with no error until
> they're used. That yields an assistant that ingests perfectly for a week and
> then stops. Production status keeps the app unverified — you click through a
> warning — but the token persists.

Then, on the Mini (opens a browser):

    uv run python -m ingest.google_auth --authorize

**Then ask Jarvis to remind you in eight days to run:**

    uv run python -m ingest.google_auth --check

Exit 0 = the credential survived, Phase 6 proceeds. Exit 1 = the publishing
status didn't take; fix it before any importer is written.

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

**In order.** Do not start §3.2 before §3.1 has run for two weeks.

### 3.1 Calendar ingester — *blocked on §2.1 passing on day 8*

Spec: `docs/phase-6-ingestion.md` §3. Schema is already in place (`005`).

- `ingest/calendar.py`: full fetch, then `syncToken` incremental sync.
- **`syncToken` is not an optimization.** Incremental sync is the only way
  deletions arrive (`status: "cancelled"`). A cancelled meeting that lingers is
  worse than one never imported, because you plan around it.
- `singleEvents=true` so recurring events arrive expanded, one row per
  occurrence. Storing rules would mean reimplementing RRULE in `/agenda`.
- `source='calendar'`, `external_id` = Google event id. `idx_events_ext` (a
  *partial* unique index, in the schema since `001`) does the dedupe. Nothing
  to migrate.
- Read-only, one direction. Voice events stay `source='voice'` and are not
  pushed to Google. Two-way sync is a much larger problem and none of it is
  needed to make `/agenda` useful.
- launchd plist shaped like `com.jarvis.scheduler`; write `sync_state` every
  run so silence is detectable.

Then **two weeks of calendar only** before touching Gmail.

### 3.2 Gmail → proposals

Spec: §4. Narrow query (flights, appointments, deliveries, reservations) — the
query filters, not the model. Track `historyId` in `sync_state`.

**Extraction writes to `proposals`, never to `events`.** Acceptance goes
through the mutations helper so it's logged and undoable. The risk isn't
being occasionally wrong; it's that one invented appointment teaches you to
distrust the agenda, and an agenda you don't trust is decoration.

Add a Review tab to the app. Cap Haiku spend per run — `/say` has a human
waiting, an inbox sweep doesn't, and the token columns already measure it.

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

    # Python — offline, always safe, ~1.5s
    uv run pytest tests/ -q --ignore=tests/test_utterances.py

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
