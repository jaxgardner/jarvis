# Phase 7 — Native iOS client

Replaces the Shortcut + ntfy front end with a real app. Like `CLAUDE.md`, this
file exists so settled decisions stay settled. If something here seems wrong,
say so — don't silently do it differently.

Prerequisite: paid Apple Developer Program membership. **Already purchased.**

---

## 1. What this phase is and is not

**Is:** a native SwiftUI app that is the primary client — talk to it, see the
agenda, act on notifications without opening anything, undo mistakes by swipe.

**Is not:** a way to say "Hey Jarvis." Read §2 before building anything, because
that was the original motivation and it is the one thing that cannot be
delivered.

This phase also absorbs **Phase 5 (dashboard)**. Building the agenda/undo/latency
views natively is strictly better than HTMX-over-Tailscale now that there is an
app to put them in. Do not build both.

---

## 2. The wake word — settled, and the answer is no

Apple reserves the wake-word layer for Siri. A third-party app **cannot** register
one at the OS level. iOS 26.2 began laying groundwork for replacing the assistant
bound to the Side Button, but it is **Japan-only**; the DMA may extend it to the
EU. The US is not in that conversation. Do not plan around it.

Four approximations, in the order they should be built:

| Approach | Effort | Verdict |
| :-- | :-- | :-- |
| Control Center control / Action Button → listening mode | ~60 lines | **Build first.** Best ergonomics per unit of work |
| "Hey Siri, Jarvis, …" via App Intents | ~half a day | **Build.** Not shorter than today; much better quality |
| In-app wake word (Porcupine / openWakeWord) | days, ongoing | **Skip.** See below |
| Wait for Apple to open the assistant slot | — | Not a plan |

### Why the in-app wake word is a trap

It is technically achievable: `audio` background mode, a live `AVAudioSession`,
a small on-device keyword model. The costs are what kill it:

- Permanent red mic indicator in the status bar.
- Real, continuous battery drain.
- iOS suspends the session on calls, Siri, and route changes, and eventually
  reclaims the app entirely. It works reliably only while the app is foreground
  or freshly backgrounded — which is exactly when pressing a button is easy.
- The Phase 4 notes already concluded this about the desktop: *"false triggers
  are genuinely maddening."* A phone in a pocket is a worse acoustic environment
  than a desk, not a better one.

App Store review would reject it. That part is irrelevant — internal TestFlight
skips review — but the battery and reliability costs are not.

**If hands-free-across-the-room is the real requirement, the honest answer is a
dedicated always-on mic on the Mini (Phase 4) or an ESP32 satellite. Not the
phone.**

### The App Intents constraint worth knowing up front

Every `AppShortcut` phrase **must** contain the `\(.applicationName)` token. It is
a compile error to omit it. There is no phrasing that drops the app name. Name the
bundle `Jarvis` and add `INAlternativeAppNames` for pronunciation robustness.

---

## 3. Distribution

**TestFlight, internal testing group.** Up to 100 testers, **no Beta App Review** —
builds are installable minutes after upload.

The one recurring cost: **builds expire 90 days from upload.** Mitigate with a
`fastlane beta` lane so re-upload is one command, and a recurring Jarvis reminder
at 80 days. The assistant should be the thing that keeps the assistant alive.

Free personal-team signing is a dead end for this — 7-day profile expiry, 3
devices, 10 App IDs. Not viable for something depended on daily.

---

## 4. Push: APNs replaces ntfy

`app/notify.py` was written as a single swappable function for exactly this. The
Mini signs a JWT with a `.p8` key and POSTs to `api.push.apple.com` over HTTP/2.
No SDK. The `.p8` never expires, unlike a `.p12` cert.

### Contract that must not break

`notify.push()` **returns a bool and never raises.** `scheduler/run.py` depends on
this: a `False` return puts the reminder back to `pending` for the next tick, and
a raised exception there would take down every other due reminder in the same
run. The APNs implementation must preserve both properties exactly.

Keep ntfy live in parallel for two weeks — "reminders silently stop firing" is on
the risk table, and a push backend swap is precisely how that happens. Env flag,
both fire, drop ntfy once APNs has been quiet-free for a fortnight.

### The actual win: actionable notifications

Aesthetics are the smaller half. A fired reminder gains **Snooze 10m / Done /
Undo** buttons handled without launching the app.

Note that `reminders.status` already includes **`acked`** (migration `002`), and
nothing currently ever sets it. The Done button is what that state was for.

Deep-path job completions become deep links that open to the job result instead
of dumping prose into a notification body.

### Free side effect

APNs rides Apple's infrastructure, so **reminders arrive even when Tailscale is
off.** Strictly better than today.

---

## 5. Server-side work

Small. The existing API contracts are already the right shape — `reply` being a
single TTS-safe plain string is exactly what the app wants.

- [x] **`migrations/003_devices.sql`** — `devices` table: APNs token, platform,
      label, `last_seen_at`, revoked flag.
- [x] **`POST /devices`** — register/refresh an APNs token. Plus `GET /devices`
      and `DELETE /devices/{id}`; revocation is not a feature without a way to
      invoke it.
- [x] **Per-device bearer tokens**, replacing the single shared `JARVIS_TOKEN`, so
      a lost phone can be revoked without re-keying everything. Keep
      `JARVIS_TOKEN` working as a fallback so the Shortcut doesn't break mid-migration.
- [x] **`POST /reminders/{id}/snooze`** and **`/ack`** — the notification action
      handlers. Both go through the mutations helper like every other write.
- [x] **`app/notify.py`** — APNs backend behind the existing signature.
      Selected by `PUSH_BACKENDS`; `ntfy,apns` dual-sends.
- [ ] *Optional:* SSE on the deep path so the app shows progress instead of
      polling `GET /jobs/{id}`.

Consumed as-is, no changes needed: `/say`, `/agenda`, `/undo`, `/jobs/{id}`,
`/metrics`.

### Two things the implementation settled

**Snoozing drops the recurrence rule.** The scheduler inserts the next
occurrence at fire time, so a snoozed row that kept its rule would insert a
second one when it fires again — a daily reminder quietly becoming two. The
snoozed copy is a one-off.

**No registered device means `push()` returns False**, which makes the
scheduler retry the reminder every tick until it ages out at six hours. That
is deliberate: the alternative is reporting delivery of a notification that
went nowhere, which is the exact failure this subsystem exists to make loud.
It only bites if `PUSH_BACKENDS=apns` is set before a device has enrolled —
which is why the default stays `ntfy`.

### Keychain, not UserDefaults

The bearer token is a credential. `UserDefaults` is plaintext in the app
container.

---

## 6. Client architecture

**SwiftUI, native. Not React Native.** App Intents, Siri, widgets, and Control
Center controls are the entire point of this phase and are first-class only in
Swift.

### Screens

| Screen | Source | Notes |
| :-- | :-- | :-- |
| Talk | `POST /say` | Mic button, transcript, reply spoken via `AVSpeechSynthesizer` |
| Agenda | `GET /agenda` | Use the server's `when` strings — do not re-format dates client-side |
| Activity | recent utterances + mutations | Swipe-to-undo → `POST /undo` |
| Jobs | `GET /jobs/{id}` | Deep-path status, live |
| Health | `GET /health`, `/metrics` | p50/p95 panel; p95 > 2s is a bug |

### Speech in

iOS 26's `SpeechAnalyzer` / `SpeechTranscriber` — faster and more accurate than
the old `SFSpeechRecognizer`, which stays as the fallback path. On-device.

### App Intents to ship

`SayToJarvis(text:)` as the catch-all, plus `AddReminder` and `WhatsMyAgenda` as
typed intents. Use `ProvidesDialog` for spoken confirmation and
`$param.requestValue(...)` for follow-ups. iOS 26 interactive snippets let a
reminder confirmation render as real UI in the Siri overlay, with a Snooze button
in it.

### Networking

Tailscale, via the **MagicDNS name — never a hardcoded IP**. App Intents execute
in a separate extension process that must reach the tailnet too; a stale IP
breaks the Siri path while the app itself keeps working, which is a miserable
thing to debug.

---

## 7. Build order

| Step | Scope | Est. | State |
| :-- | :-- | :-- | :-- |
| **7a** | Shell app: mic → STT → `POST /say` → reply, spoken | ~2 days | Built — `ios/`. Not yet run on hardware |
| **7b** | APNs: `devices` table, notify.py swap, actionable reminders | ~1 day | **Done and verified on hardware** — 2026-07-31. Snooze from a locked screen requeued reminder 5 and logged mutation 17 |
| **7c** | App Intents (+ interactive Siri snippet) | ~1 day | Done. Action Button, Siri, Shortcuts, Spotlight |
| **7d** | Native dashboard (absorbs Phase 5) | ~1–2 days | In progress |

**The Control Center control is cut**, 2026-07-31. It was the third item in §2's
table and the cheapest ergonomics-per-line on paper, but the Action Button
binding covers the same "start listening now" need without a widget extension —
and a widget extension is a second process, which would drag in an App Group and
a shared Keychain access group just to read the device token. Not worth it for a
second way to do one thing.

Next phase: **`docs/phase-8-voice.md`** — Kokoro on the Mini, because
`AVSpeechSynthesizer` sounds like a screen reader and no amount of tuning fixes
that.

**7a is done when it fully replaces the Shortcut** — including on cellular, with
Tailscale cold.

Do 7a–7c, then live with it before starting 7d. Same two-week usage gate the
plan applies everywhere else; the dashboard should be shaped by what actually
annoys you.

---

## 8. Risks

| Risk | Mitigation |
| :-- | :-- |
| TestFlight build silently expires, app dies | `fastlane beta` lane + recurring 80-day Jarvis reminder |
| APNs swap breaks reminder delivery | Dual-send with ntfy for two weeks; preserve the bool-return contract |
| Hardcoded Mini IP breaks the Siri path only | MagicDNS name; smoke-test the App Intent separately from the app |
| Phase 7 becomes a second dashboard project | 7d explicitly replaces Phase 5 — do not build both |
| Wake-word scope creep | §2 is settled. Revisit only if Apple opens the assistant slot in the US |
