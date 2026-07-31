# Phase 8 — A voice worth listening to

Replaces `AVSpeechSynthesizer` with a real TTS model running on the Mini.
Like `CLAUDE.md` and `docs/phase-7-ios.md`, this file exists so settled
decisions stay settled. If something here seems wrong, say so — don't
silently do it differently.

Prerequisite: Phase 7a shipped and the app is the primary client.

---

## 1. Why this is a phase and not a setting

`AVSpeechSynthesizer` with a premium voice is the best iOS gives you for free,
and it still sounds like a screen reader. The gap is not tuning — a better
rate, a different voice, more punctuation in the reply — it is the class of
model. Concatenative and formant synthesis cannot sound like a person; a
neural vocoder can.

This phase is worth doing *last* precisely because it changes nothing about
what the assistant can do. Everything else on the roadmap makes it more
capable. This makes it pleasant, which is not the same thing and should not
be confused with it.

---

## 2. The model — settled

**Kokoro-82M**, running on the Mini.

| | |
| :-- | :-- |
| License | Apache-2.0 — no usage terms to re-read later |
| Size | 82M parameters. Not a typo; it is genuinely small |
| Voices | 54, including `bm_george`, `bm_daniel`, `bm_lewis`, `bm_fable` |
| Package | `kokoro` on PyPI (0.9.4 at time of writing), plus `misaki` for G2P |
| Runs on | The Mini. Nothing leaves the tailnet |

The British male voices are the reason this is the pick rather than a
general-quality argument. `bm_george` is the register the whole project is
named after. Start there; the others are one string change away.

**Python stays pinned at 3.12.** `CLAUDE.md` already records this — the Kokoro
stack trails new CPython on prebuilt wheels. That pin was made for this phase.

### What was rejected, and why

**ElevenLabs and other hosted TTS.** They sound superb. They also send every
reply to a third party, bill per character, and put a network round trip
inside the 2s budget. That is three design principles traded for one nicer
voice — and the one thing a personal assistant should not do is make your
private calendar someone else's server log.

**Apple's Personal Voice.** Clones *your* voice. Correct feature, wrong person.

**Anything requiring a GPU.** The Mini is the whole deployment. If it doesn't
run acceptably on an M4 at 82M parameters, the answer is to keep the fallback,
not to buy hardware.

---

## 3. Latency is the actual design problem

Quality is settled. The open question is where generation sits relative to the
2s budget, and the honest answer is that nobody knows until it's measured on
this machine.

**Benchmark first.** Before writing the service, measure Kokoro's real-time
factor on the M4 for a typical reply — one or two sentences, ~15 words. That
number decides the architecture, and guessing it wrong means either a sluggish
assistant or a pointless streaming implementation.

Two shapes, chosen by what the benchmark says:

| If generation is… | Then |
| :-- | :-- |
| Comfortably under ~300ms | Generate inline; return the audio with the reply |
| Slower, or variable | Stream the first chunk as it's produced; don't wait for the whole clip |

Either way the contract is the same, and it is deliberately additive:

    // /say response gains one optional field
    {"reply": "...", "route": "fast", "audio_url": "/audio/412.wav", ...}

`reply` keeps its current guarantee — a single plain-text TTS-safe string —
because it is what the fallback speaks and what the Siri path reads aloud.
`audio_url` is an *upgrade*, never a requirement.

---

## 4. The fallback is not optional

**`AVSpeechSynthesizer` stays in the app forever.** The app speaks Kokoro audio
when it arrives promptly and falls back the moment it doesn't — Mini asleep,
Tailscale off, generation slow, service crashed.

This is the same reasoning as the ntfy/APNs dual-send in Phase 7b: a voice
upgrade must not become a new way for the assistant to go silent. A reply you
can't hear is worse than a reply in a robot voice.

Concretely: the app requests audio with a short timeout. On timeout it speaks
locally and discards whatever arrives late — it does not speak the reply twice.

---

## 5. Work

- [ ] **Benchmark** Kokoro on the M4 — real-time factor for a 15-word reply,
      cold and warm. This decides §3.
- [ ] **`tts/` service** — load the model once at startup, not per request.
      Model load is seconds; synthesis is milliseconds.
- [ ] **`GET /audio/{utterance_id}`** — serve the generated clip. Cache by
      utterance so a replay doesn't regenerate.
- [ ] **`audio_url` on `/say`** — optional field, absent when TTS is off or
      failed.
- [ ] **`Speaker.swift`** — prefer remote audio, fall back on timeout. The one
      place in the app where this decision lives.
- [ ] **`TTS_ENABLED` / `TTS_VOICE`** in `.env`, defaulting to off, so the
      cutover is a flag and the rollback is the same flag.

Reuse the reminder path too: a fired reminder's notification can carry a
Kokoro-generated audio attachment, which is strictly better than the system
voice reading a notification body.

---

## 6. Risks

| Risk | Mitigation |
| :-- | :-- |
| Generation blows the 2s budget | Benchmark before building; stream if needed; fallback always present |
| The voice becomes a new silent-failure mode | Timeout + local fallback, same discipline as the APNs cutover |
| Model load on every request | Load once at service start; that is what makes it a service |
| Scope creep into voice cloning | Not this phase. The register is `bm_george` and it is settled |
