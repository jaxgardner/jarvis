# Jarvis for iOS

The client that replaces the Shortcut.

    open ios/Jarvis.xcodeproj

## Before the first build

1. **`Config.xcconfig`** — set `JARVIS_BUNDLE_ID` and `JARVIS_DEVELOPMENT_TEAM`.
   Everything else reads from there, so there is no bundle ID buried in
   `project.pbxproj` to go stale.

2. **Register the App ID** in the Developer portal with the **Push
   Notifications** capability, using the same bundle ID.

3. **`JARVIS_BUNDLE_ID` must equal `APNS_BUNDLE_ID`** in the server's `.env`.
   APNs rejects a push whose `apns-topic` doesn't match, and it surfaces as
   silent non-delivery rather than an error you'd notice.

The project builds and runs in the simulator without any of this — but with
`CODE_SIGNING_ALLOWED=NO`, and push does nothing there.

## Layout

| File | What it is |
| :-- | :-- |
| `JarvisApp.swift` | Entry point; picks Setup or Talk based on enrollment |
| `SetupView.swift` | One-time enrollment, plus the (thin) settings sheet |
| `TalkView.swift` | The screen that replaces the Shortcut |
| `Transcriber.swift` | `SpeechAnalyzer` / `SpeechTranscriber`, on device, plus the pause detector |
| `Speaker.swift` | `AVSpeechSynthesizer` — speaks `reply` verbatim |
| `JarvisAPI.swift` | The server contract |
| `Keychain.swift` | Where the device token lives. Not `UserDefaults` |
| `PushRegistrar.swift` | APNs registration + the Snooze / Done / Undo buttons |
| `AppDelegate.swift` | Token hand-off and notification action handling |
| `JarvisIntents.swift` | App Intents — Action Button, Siri, Shortcuts, Spotlight |
| `LaunchRouter.swift` | Latch that gets `StartListening` from the intent to the mic |
| `AgendaView.swift` | What's coming up; swipe a reminder to snooze or complete |
| `ActivityView.swift` | What you said and what it changed; swipe-to-undo |
| `JobsView.swift` | Deep-path history, with live refresh while one runs |
| `HealthView.swift` | p50/p95 against the 2s budget, plus token spend |

## Tests

    xcodebuild test -project ios/Jarvis.xcodeproj -scheme Jarvis \
      -destination 'platform=iOS Simulator,name=iPhone 17 Pro'

`JarvisTests/ContractTests.swift` decodes **real captured server responses**
from `JarvisTests/Fixtures/`, not hand-written approximations — a fixture you
wrote yourself only proves the decoder matches what you imagined the server
sends. This is the seam that breaks silently: a column gets renamed, the
decoder throws, and the screen renders empty with an error nobody reads.

Re-capture a fixture after changing an endpoint:

    curl -s localhost:8000/activity -H "Authorization: Bearer $JARVIS_TOKEN" \
      > ios/JarvisTests/Fixtures/activity.json

## Sending

Tap the mic, talk, stop talking. The app detects the pause and sends — there is
no second tap and no Send button, because a confirmation step would make this
slower than the Shortcut it replaces.

Tapping the button while it is listening still sends immediately, and that is
the path to use in a room too loud to measure. Settings has the pause length
(0.8 / 1.2 / 2.0 seconds) and a switch to turn the whole thing off, which puts
the second tap back.

The detection is energy-based and lives on the audio thread; `Endpointer` in
`Transcriber.swift` carries the reasoning, and `JarvisTests/EndpointerTests.swift`
pins the cases that matter — a café, a mic that opens mid-sentence, a breath
mid-reminder, and a door slam that produces no text.

## The Action Button

There is no Action Button API. You expose an App Intent, and the button gets
it via Shortcuts:

**Settings → Action Button → swipe to Shortcut → Choose a Shortcut → Talk to
Jarvis.**

`StartListening` is the one to bind: it opens the app with the mic already
live, so it is press-and-talk rather than press-then-aim-at-a-button. It is
the only intent with `openAppWhenRun`; the rest answer without launching
anything.

Siri works off the same declarations — "Hey Siri, talk to Jarvis", "Hey Siri,
ask Jarvis…", "Hey Siri, what's on my Jarvis agenda". Every phrase must contain
the app name; that is an Apple constraint, not a choice, and omitting the
`\(.applicationName)` token is a compile error.

## Enrollment

On first launch the app asks for the Mini's **MagicDNS name** and the shared
`JARVIS_TOKEN`. It trades that for a token belonging to this device
(`POST /devices`) and writes it to the Keychain; the shared token is not
retained. Losing the phone then costs one `DELETE /devices/{id}` rather than
re-keying every client.

Use the MagicDNS name, never an IP. App Intents run in a separate
extension process that has to reach the tailnet too, and a stale IP breaks the
Siri path while the app itself keeps working.

## What is deliberately not here yet

- **The Control Center control.** Needs a widget
  extension target, which also needs an App Group and a shared Keychain
  access group, because a widget is a separate process and cannot see this
  app's Keychain items as things stand.
- **The dashboard** (agenda, activity, jobs, health screens). `JarvisAPI`
  already has `agenda()` and `undo()` because the notification actions need
  them.
- **A wake word.** Settled, and the answer is no: Apple reserves the
  wake-word layer for Siri, and a third-party app cannot register one at the
  OS level. An in-app one needs a permanently live mic — a red status-bar
  indicator, real battery drain, and a session iOS reclaims anyway. If
  hands-free across the room is the requirement, the honest answer is a
  dedicated mic on the Mini, not the phone.

## Verified, and not

Built clean for Debug and Release against the iOS 26.5 SDK, launched in the
simulator, and the server contract exercised end to end with curl: enrollment,
device-token auth, `/agenda` shapes, snooze, ack, undo, revocation.

**Untested on hardware:** the microphone path and APNs delivery. Neither can
run in a simulator. `Transcriber` in particular — audio format conversion into
`SpeechAnalyzer` is the part most likely to need adjustment on a real device.

The pause detector is tested against synthetic buffers, which covers its logic
but not its constants: whether 0.006 RMS is really the floor of a quiet room,
and whether 0.03 clears yours, are questions only a real microphone answers. If
it sends mid-sentence, raise the pause length first; if it never sends, the room
is louder than the ceiling and `adaptiveCeiling` is the number to look at.

## TestFlight

Internal testing group, up to 100 testers, **no Beta App Review** — installable
minutes after upload.

The recurring cost is that **builds expire 90 days from upload**. Add a
`fastlane beta` lane so re-upload is one command, and have Jarvis remind you at
80 days. The assistant should be the thing that keeps the assistant alive.
