# A voice worth listening to

Replies are spoken by a local neural TTS running on the Mini, in a British
male voice, instead of by Apple's on-device synthesizer. The phone asks the
server for audio and plays it; if the server can't answer, it falls back to
what it does today.

## Why this shape

**The robotic quality is Apple's, not ours.** `Speaker.swift` already asks for
a premium voice and settles for whatever exists. On a phone with nothing
downloaded that is the compact `en-US` voice, which is a screen reader. No
amount of rate and pitch tuning moves a compact voice into the territory the
word "JARVIS" implies.

**Offline was never a property we had.** Every reply comes from the Mini over
Tailscale; the phone has never spoken without a round trip. So moving
synthesis to the server costs no capability that exists today. This is the
observation that makes the whole design cheap.

**Local, because the alternative is renting your assistant's voice.** Cloud
neural TTS is better. It also means every reply text leaves the house, and a
per-character bill on a system whose entire point is that it is yours.
Kokoro-82M on an M4 is good enough that the gap stops mattering once you've
heard it.

**PyTorch is not worth 2.5 GB here.** `kokoro-onnx` runs the same weights on
onnxruntime at a seventh the install. The dependency list of this repo is five
packages and should stay recognizable.

## Layout

A new `speech/` package, sibling to `pantry/` and `ingest/`:

| Module | Job | Depends on |
| :-- | :-- | :-- |
| `speech/synth.py` | Text → 24 kHz mono WAV bytes. Owns the model. | kokoro-onnx |
| `speech/wav.py` | float32 samples → PCM16 WAV. Stdlib `wave`. | nothing |
| `speech/audition.py` | Writes the same replies in every candidate voice. | synth |

`wav.py` is separate so nothing drags in `soundfile` and libsndfile for a job
the standard library does in ten lines. It is also the only part with logic
worth unit-testing without a 310 MB model on disk.

## Data flow

```
 POST /say ─────────────▶ {"reply": "...", ...}      ← unchanged, same latency
                                  │
 iOS: reply lands, text renders   │
                                  ▼
 POST /speech {"text": ...} ─▶ synth.py ─▶ audio/wav ─▶ AVAudioPlayer
                                  │
                            503 / timeout / any error
                                  ▼
                          AVSpeechSynthesizer, as today
```

Two round trips, deliberately. Folding audio into `/say` would put 500–1000 ms
of synthesis inside the endpoint whose p95 is the system's headline number, and
`/metrics` would start reporting a regression that is really a feature. The
text still arrives on budget and renders while the audio is being made.

## The endpoint

`POST /speech`, bearer auth like everything else.

    // request
    {"text": "Reminder set for five o'clock."}

    // 200
    audio/wav, 24 kHz mono PCM16
    X-Synth-Ms: 640

    // 503 — model files not present on this machine
    {"detail": "tts unavailable"}

Text in, audio out, with no reference to `utterances`. A reply is not the only
thing worth speaking — a job result or a notification body would use the same
endpoint — and coupling audio to a row would rule that out for no gain.

It is a `def`, not an `async def`. onnxruntime inference is CPU-bound and would
block the event loop; Starlette runs sync endpoints in a threadpool. A
`threading.Lock` around inference keeps concurrent callers from sharing one
session, which the phone will not do but the scheduler eventually might.

## Model files, config, health

The weights live beside the database, outside the repo, for the reason
everything else does — 310 MB is not a thing to commit, and a re-clone should
not re-download it.

    ~/Library/Application Support/jarvis/voices/kokoro-v1.0.onnx
    ~/Library/Application Support/jarvis/voices/voices-v1.0.bin

| Setting | Default | Why |
| :-- | :-- | :-- |
| `TTS_VOICE` | `bm_george` | Picked by the audition, not by me. One line to change. |
| `TTS_SPEED` | `1.0` | Kokoro's natural pace. The audition sets the real value; if it reads as hurried for a butler, `.env` says `0.9`. |
| `TTS_MODEL_DIR` | beside the DB | Escape hatch; not normally set. |

There is no `TTS_ENABLED`. Presence of the model files is the switch: a fresh
checkout that has never run the download script gets a 503 and a phone that
speaks the old way, which is the correct behaviour and needs no flag to
express. `/health` gains a `tts` block — voice, whether the model loaded, and
the last synth's milliseconds — so "why is it robotic again" has an answer that
isn't a log file.

The model is loaded lazily into a module global and warmed once at startup, so
the first real reply of the day doesn't eat the load.

## Fallback

`Speaker.speak()` becomes async: fetch, and on **any** failure — non-200,
transport error, or a 3-second timeout — hand the same string to
`AVSpeechSynthesizer` and return. The timeout is the load-bearing part. Silence
while a dead server is waited on is worse than the compact voice; the compact
voice is at least an assistant.

This preserves the existing invariant that a spoken reply always happens, which
is the same reasoning that keeps `notify.push()` returning a bool instead of
raising.

## Testing

**Python.** `synth.py` takes its engine as an injected callable, so endpoint
tests run against a fake returning three sine cycles — no model on disk, no
onnxruntime in CI. Cases: 200 returns a parseable WAV with the right rate and
channel count; 503 when the model directory is absent; 401 unauthenticated; and
that the text reaches the engine byte-for-byte, since `Speaker`'s contract is
that nobody "fixes up" server text. `wav.py` gets real unit tests — header
fields, clipping at ±1.0, empty input.

**iOS.** The fetch goes behind a protocol so `SpeakerTests` can assert the
fallback fires on error, on non-200, and on timeout, without a network or an
audio device. `AVAudioPlayer` itself is not under test.

## Order of work

1. **The audition, before anything else.** Install `kokoro-onnx` on the Mini,
   render three real replies through `bm_george`, `bm_lewis`, `bm_daniel`, and
   `bm_fable`, and listen. The entire design assumes one of them sounds right;
   twenty minutes settles it before a line of endpoint code exists. This also
   resolves whether `brew install espeak-ng` is genuinely required — recent
   `kokoro-onnx` bundles it through `espeakng-loader` — which is currently the
   only open question in the dependency story.
2. `wav.py` and its tests.
3. `speech/synth.py`, the download script, `POST /speech`, `/health`.
4. `Speaker.swift` and its fallback tests.
5. Measure. `X-Synth-Ms` plus the gap you actually perceive decides whether
   streaming is ever worth building.

## Not in scope

**Streaming.** Sentence-by-sentence synthesis would put first audio ~300 ms
out instead of ~700 ms, at the cost of chunked playback on iOS and a streaming
response on the server. Step 5 decides whether that trade is real; guessing now
builds it for nothing.

**Caching.** Templated confirmations repeat, so a hash-keyed WAV cache is
obvious and easy. It is also premature until a measured synth time says the
repeat is worth avoiding.

**Diction.** JARVIS is also *phrasing* — "Certainly, sir" rather than "Added to
the pantry." Replies are templated in Python and that is a genuinely cheap
change, but it is a different problem from timbre and mixing them means neither
gets judged on its own.
