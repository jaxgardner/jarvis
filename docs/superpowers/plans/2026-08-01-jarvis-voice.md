# Local Neural Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replies are spoken in a British male neural voice synthesized on the Mac Mini, with Apple's on-device synthesizer as the fallback whenever the server can't answer.

**Architecture:** A new `speech/` package wraps Kokoro-82M on onnxruntime and exposes `POST /speech` — text in, 24 kHz mono WAV out. `POST /say` is untouched, so the headline latency number does not move; the phone renders the text, then fetches and plays the audio on a second round trip. Any failure on that second trip falls through to `AVSpeechSynthesizer`.

**Tech Stack:** Python 3.12, FastAPI, `kokoro-onnx`, stdlib `wave`; Swift 6 / SwiftUI, `AVAudioPlayer`, Swift Testing.

Spec: `docs/superpowers/specs/2026-08-01-jarvis-voice-design.md`

## Global Constraints

- Python is pinned `>=3.12,<3.13`. Do not add a dependency that has no cp312 wheel for arm64 macOS.
- Do not add `soundfile`, `librosa`, or `torch`. WAV encoding is stdlib `wave`; the whole point of `kokoro-onnx` over `kokoro` is avoiding PyTorch.
- Model weights live at `$JARVIS_DB`'s parent directory, never in the repo. Nothing 310 MB gets committed.
- There is no `TTS_ENABLED` flag. Presence of the model files on disk is the switch.
- `/say` request and response shapes do not change. No audio is added to that endpoint.
- The server's plain-text guarantee holds: nothing in this plan reformats, strips, or "fixes up" reply text on its way to synthesis.
- Timestamps stay ISO 8601 with offset. `_at` is an instant; `_on` is a bare date.
- New endpoints require bearer auth via `Depends(require_token)`, like every other endpoint.
- Tests must pass with no model files present — CI has no 310 MB download.

---

### Task 1: The voice audition

The entire design assumes one of Kokoro's British males sounds like JARVIS to the user's ear. Twenty minutes settles that before any endpoint exists. This task also resolves the one open dependency question: whether `brew install espeak-ng` is genuinely required, or whether recent `kokoro-onnx` bundles it through `espeakng-loader`.

**This task ends with a human listening and choosing.** Do not proceed to Task 2 without a decided voice.

**Files:**
- Create: `speech/__init__.py`
- Create: `speech/audition.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to `dependencies`, keeping the commented style of its neighbours:

```toml
    # Local TTS. The onnxruntime build of Kokoro-82M, not the reference
    # `kokoro` package — same weights, a seventh of the install, and no
    # PyTorch in a repo whose dependency list should stay readable.
    "kokoro-onnx>=0.4",
```

Run: `uv sync`
Expected: resolves and installs. If it fails on Python 3.12, stop and report — do not relax the pin.

- [ ] **Step 2: Download the weights**

```bash
VOICES="$HOME/Library/Application Support/jarvis/voices"
mkdir -p "$VOICES"
curl -L -o "$VOICES/kokoro-v1.0.onnx" \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -L -o "$VOICES/voices-v1.0.bin" \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
ls -lh "$VOICES"
```

Expected: `kokoro-v1.0.onnx` around 310 MB, `voices-v1.0.bin` around 26 MB. If either is a few kilobytes, `curl` followed an HTML error page — check the URL against the project's current release list rather than retrying blindly.

- [ ] **Step 3: Create the package marker**

Create `speech/__init__.py`:

```python
"""Speech out. Text becomes audio here, and nowhere else."""
```

- [ ] **Step 4: Write the audition script**

Create `speech/audition.py`:

```python
"""Render the same replies in every candidate voice, for a human to judge.

Run once, before any of the rest of this exists:

    uv run python -m speech.audition

Kokoro ships four British males. Which one sounds like an assistant rather
than a narrator is not a thing to decide from a model card, so this writes
twelve files and gets out of the way.
"""

import sys
import wave as wavelib
from pathlib import Path

# Real replies, not lorem ipsum. The confirmations are what you hear forty
# times a day, and a voice that reads a paragraph beautifully can still be
# wrong for "Got it."
LINES = [
    "Got it. Reminder set for five o'clock.",
    "You have three things tomorrow. Dentist at nine, standup at ten thirty, "
    "and dinner with Sam at seven.",
    "The milk expires tomorrow, and you're out of coffee.",
]

VOICES = ["bm_george", "bm_lewis", "bm_daniel", "bm_fable"]

OUT = Path("/tmp/jarvis-audition")


def main() -> int:
    from kokoro_onnx import Kokoro

    from app import config

    model = config.TTS_MODEL_DIR / "kokoro-v1.0.onnx"
    voices = config.TTS_MODEL_DIR / "voices-v1.0.bin"
    if not model.exists() or not voices.exists():
        print(f"missing model files in {config.TTS_MODEL_DIR}", file=sys.stderr)
        return 1

    kokoro = Kokoro(str(model), str(voices))
    OUT.mkdir(parents=True, exist_ok=True)

    for voice in VOICES:
        for index, line in enumerate(LINES, start=1):
            samples, rate = kokoro.create(line, voice=voice, speed=1.0, lang="en-gb")
            path = OUT / f"{voice}-{index}.wav"
            with wavelib.open(str(path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(
                    b"".join(
                        int(max(-1.0, min(1.0, s)) * 32767).to_bytes(
                            2, "little", signed=True
                        )
                        for s in samples
                    )
                )
            print(path)

    print(f"\n{len(VOICES) * len(LINES)} files in {OUT}. Listen, then set "
          f"TTS_VOICE in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

This script duplicates the WAV writing that Task 2 turns into `speech/wav.py`. That is deliberate — the audition has to run before Task 2 exists, and Task 5 deletes this file's copy. Note the temporary duplication and move on.

- [ ] **Step 5: Add the config the script reads**

The script imports `config.TTS_MODEL_DIR`, which does not exist yet. Add it to `app/config.py`, after the pantry block:

```python
# ── speech ────────────────────────────────────────────────
# The weights are ~310 MB, so they live beside the database for the same
# reason it does: never committed, and a re-clone does not re-download them.
TTS_MODEL_DIR = Path(
    os.getenv("TTS_MODEL_DIR", "").strip() or DB_PATH.parent / "voices"
).expanduser()
```

- [ ] **Step 6: Run the audition**

Run: `uv run python -m speech.audition`
Expected: twelve paths printed, twelve WAV files in `/tmp/jarvis-audition`.

If it fails with an espeak-ng or phonemizer error, run `brew install espeak-ng` and try again. **Record which of the two happened** — it decides one line of `README` in Task 6.

- [ ] **Step 7: Listen and choose**

```bash
open /tmp/jarvis-audition
```

Play all twelve. **Stop here and ask the user which voice to use, and whether the pace wants slowing.** Two values come out of this: `TTS_VOICE` and `TTS_SPEED`.

If none of the four sound acceptable, stop and report that — the design's central assumption has failed, and the next move is a conversation, not a workaround.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock speech/__init__.py speech/audition.py app/config.py
git commit -m "feat(speech): kokoro-onnx and a voice audition script"
```

---

### Task 2: WAV encoding

**Files:**
- Create: `speech/wav.py`
- Test: `tests/test_speech_wav.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `speech.wav.encode(samples: Iterable[float], sample_rate: int) -> bytes` — a complete PCM16 mono WAV file, header included.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speech_wav.py`:

```python
"""float32 samples to a WAV file, using nothing but the standard library.

The clipping test is the one that matters. Kokoro occasionally returns
samples a hair outside ±1.0, and the naive `int(sample * 32767)` raises
OverflowError inside array('h') rather than producing quiet distortion —
which means one loud reply would 500 instead of sounding slightly wrong.
"""

import io
import wave

from speech import wav


def read(payload: bytes):
    with wave.open(io.BytesIO(payload), "rb") as handle:
        return handle, handle.readframes(handle.getnframes())


def test_encode_produces_a_readable_mono_wav():
    payload = wav.encode([0.0] * 100, 24_000)

    handle, frames = read(payload)
    assert handle.getnchannels() == 1
    assert handle.getsampwidth() == 2
    assert handle.getframerate() == 24_000
    assert handle.getnframes() == 100
    assert frames == b"\x00\x00" * 100


def test_full_scale_clips_instead_of_overflowing():
    payload = wav.encode([2.0, -2.0, 1.0, -1.0], 24_000)

    _, frames = read(payload)
    values = [
        int.from_bytes(frames[i : i + 2], "little", signed=True)
        for i in range(0, len(frames), 2)
    ]
    assert values == [32767, -32768, 32767, -32768]


def test_empty_input_is_a_valid_empty_wav():
    payload = wav.encode([], 24_000)

    handle, frames = read(payload)
    assert handle.getnframes() == 0
    assert frames == b""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_speech_wav.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speech.wav'`

- [ ] **Step 3: Write the implementation**

Create `speech/wav.py`:

```python
"""PCM16 WAV, from the standard library.

`soundfile` does this in one line and brings libsndfile onto the machine to
do it. This is ten lines and brings nothing.
"""

import array
import io
import sys
import wave
from collections.abc import Iterable


def encode(samples: Iterable[float], sample_rate: int) -> bytes:
    """Float samples in [-1.0, 1.0] to a complete mono WAV file.

    Out-of-range samples clip rather than raising. Kokoro returns the
    occasional sample a hair past full scale, and a reply that is imperceptibly
    distorted beats a reply that 500s.
    """
    pcm = array.array("h", (_quantize(sample) for sample in samples))
    if sys.byteorder == "big":
        # WAV is little-endian regardless of the host. Never true on the Mini,
        # one line to be right anyway.
        pcm.byteswap()

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _quantize(sample: float) -> int:
    if sample >= 1.0:
        return 32767
    if sample <= -1.0:
        return -32768
    return int(sample * 32767)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_speech_wav.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add speech/wav.py tests/test_speech_wav.py
git commit -m "feat(speech): stdlib PCM16 WAV encoding"
```

---

### Task 3: The synthesizer

**Files:**
- Create: `speech/synth.py`
- Modify: `app/config.py`
- Test: `tests/test_speech_synth.py`

**Interfaces:**
- Consumes: `speech.wav.encode(samples, sample_rate) -> bytes`; `config.TTS_MODEL_DIR: Path`.
- Produces:
  - `speech.synth.speak(text: str) -> bytes` — a WAV. Blocking, CPU-bound.
  - `speech.synth.available() -> bool` — can this machine speak at all.
  - `speech.synth.loaded() -> bool` — is the ONNX session resident.
  - `speech.synth.warm() -> None` — load it now, if possible. Safe to call always.
  - `speech.synth.last_synth_ms: int | None` — module global, set by `speak`.
  - Tests install a double by setting `speech.synth._engine`; anything with a
    `.create(text, voice=..., speed=..., lang=...) -> (samples, rate)` works.

- [ ] **Step 1: Add the remaining config**

In `app/config.py`, extend the speech block added in Task 1:

```python
# Set by the audition in docs/superpowers/plans/2026-08-01-jarvis-voice.md.
# One line to change if it ever grates.
TTS_VOICE = os.getenv("TTS_VOICE", "bm_george").strip()
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.0"))
```

Replace `bm_george` and `1.0` with whatever Task 1 Step 7 decided.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_speech_synth.py`:

```python
"""Loading, availability, and the fact that text arrives unaltered.

The model is 310 MB and CI does not have it, so every test here runs against
a double installed at `synth._engine`. What is actually under test is the
availability logic and the promise that nothing rewrites the server's text on
its way to the voice.
"""

import io
import math
import wave

import pytest

from speech import synth


class FakeKokoro:
    """A tenth of a second of sine, at Kokoro's real output rate."""

    def __init__(self):
        self.calls = []

    def create(self, text, voice, speed, lang):
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        return [math.sin(index / 12.0) for index in range(2_400)], 24_000


@pytest.fixture
def engine(monkeypatch):
    fake = FakeKokoro()
    monkeypatch.setattr(synth, "_engine", fake)
    monkeypatch.setattr(synth, "last_synth_ms", None)
    return fake


def test_speak_returns_a_playable_wav(engine):
    payload = synth.speak("Reminder set for five o'clock.")

    with wave.open(io.BytesIO(payload), "rb") as handle:
        assert handle.getframerate() == 24_000
        assert handle.getnchannels() == 1
        assert handle.getnframes() == 2_400


def test_text_reaches_the_engine_byte_for_byte(engine):
    text = "You're out of milk — and it's 5:30."
    synth.speak(text)

    assert engine.calls[0]["text"] == text


def test_speak_records_its_own_latency(engine):
    assert synth.last_synth_ms is None

    synth.speak("Got it.")

    assert synth.last_synth_ms is not None
    assert synth.last_synth_ms >= 0


def test_an_installed_engine_counts_as_available(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(synth.config, "TTS_MODEL_DIR", tmp_path / "nothing-here")

    assert synth.available() is True
    assert synth.loaded() is True


def test_no_files_and_no_engine_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(synth, "_engine", None)
    monkeypatch.setattr(synth.config, "TTS_MODEL_DIR", tmp_path / "nothing-here")

    assert synth.available() is False
    assert synth.loaded() is False


def test_warm_is_a_no_op_when_there_is_nothing_to_load(monkeypatch, tmp_path):
    """Called unconditionally at startup, including on a fresh checkout."""
    monkeypatch.setattr(synth, "_engine", None)
    monkeypatch.setattr(synth.config, "TTS_MODEL_DIR", tmp_path / "nothing-here")

    synth.warm()

    assert synth.loaded() is False
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_speech_synth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'speech.synth'`

- [ ] **Step 4: Write the implementation**

Create `speech/synth.py`:

```python
"""Text to speech, on this machine.

The weights are 310 MB and live outside the repo, so a fresh checkout that has
never run the download simply cannot speak: `available()` is False, /speech
answers 503, and the phone falls back to Apple's synthesizer. That is the
switch. There is no TTS_ENABLED, because a flag that can disagree with the
filesystem is a flag that will.
"""

import threading
import time
from pathlib import Path

from app import config
from speech import wav

MODEL_NAME = "kokoro-v1.0.onnx"
VOICES_NAME = "voices-v1.0.bin"

# Reentrant because `speak` holds it across `engine()`, which takes it too.
# A plain Lock deadlocks on the first synthesis after a cold start.
_lock = threading.RLock()
_engine = None

# Read by /health. Process-local and deliberately not persisted: the question
# it answers is "is the voice working right now", not "what was p95 in June".
last_synth_ms: int | None = None


def paths() -> tuple[Path, Path]:
    return config.TTS_MODEL_DIR / MODEL_NAME, config.TTS_MODEL_DIR / VOICES_NAME


def _files_present() -> bool:
    return all(path.exists() for path in paths())


def loaded() -> bool:
    return _engine is not None


def available() -> bool:
    """Can this machine speak? True once the files are on disk.

    An already-installed engine counts, which is what lets tests exercise the
    endpoint without 310 MB of weights.
    """
    return loaded() or _files_present()


def engine():
    global _engine
    with _lock:
        if _engine is None:
            from kokoro_onnx import Kokoro

            model, voices = paths()
            _engine = Kokoro(str(model), str(voices))
        return _engine


def warm() -> None:
    """Build the session ahead of the first reply.

    Loading is a couple of seconds. Paying that on the first thing you say
    after a reboot is exactly the impression this feature exists to avoid.
    Safe to call on a machine with no model files: it does nothing.
    """
    if not loaded() and _files_present():
        engine()


def speak(text: str) -> bytes:
    """Synthesize `text`. Blocking and CPU-bound — call it off the event loop.

    `text` is passed through untouched. The server guarantees replies are
    plain and speakable; anything "corrected" here would hide a bug that
    belongs upstream.
    """
    global last_synth_ms
    started = time.monotonic()
    with _lock:
        samples, rate = engine().create(
            text,
            voice=config.TTS_VOICE,
            speed=config.TTS_SPEED,
            lang="en-gb",
        )
    payload = wav.encode(samples, rate)
    last_synth_ms = int((time.monotonic() - started) * 1000)
    return payload
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_speech_synth.py -v`
Expected: 6 passed

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. Nothing so far touches an existing code path.

- [ ] **Step 7: Commit**

```bash
git add speech/synth.py tests/test_speech_synth.py app/config.py
git commit -m "feat(speech): kokoro synthesis with filesystem-driven availability"
```

---

### Task 4: `POST /speech` and the health block

**Files:**
- Modify: `app/main.py:31` (the `FastAPI(...)` construction), and append the endpoint
- Modify: `app/main.py:139-145` (the `/health` return)
- Modify: `.env.example`
- Test: `tests/test_speech_api.py`

**Interfaces:**
- Consumes: `synth.speak`, `synth.available`, `synth.loaded`, `synth.last_synth_ms`, `synth.warm`.
- Produces: `POST /speech` taking `{"text": str}`, returning `audio/wav` with an `X-Synth-Ms` header, or 503 `{"detail": "tts unavailable"}`. `GET /health` gains a `tts` object with keys `available`, `loaded`, `voice`, `last_synth_ms`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speech_api.py`:

```python
"""The endpoint the phone calls after the reply text has already landed.

Two round trips is the design: /say keeps its latency and its contract, and
audio is fetched separately. So the interesting cases here are the failure
ones — an unavailable model must be a clean 503, because that is the signal
the phone turns into "use the Apple voice" rather than "say nothing".
"""

import io
import math
import wave

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


class FakeKokoro:
    def __init__(self):
        self.calls = []

    def create(self, text, voice, speed, lang):
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        return [math.sin(index / 12.0) for index in range(2_400)], 24_000


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "speech.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


@pytest.fixture
def client(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from speech import synth

    fake = FakeKokoro()
    monkeypatch.setattr(synth, "_engine", fake)

    handle = TestClient(app)
    handle.headers["Authorization"] = f"Bearer {SHARED}"
    return handle, fake


def test_speech_returns_a_playable_wav(client):
    handle, _ = client

    response = handle.post("/speech", json={"text": "Reminder set for five."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(response.content), "rb") as audio:
        assert audio.getframerate() == 24_000
        assert audio.getnchannels() == 1
        assert audio.getnframes() == 2_400


def test_the_reply_text_is_not_rewritten_on_its_way_to_the_voice(client):
    handle, fake = client
    text = "You're out of milk — and it's 5:30."

    handle.post("/speech", json={"text": text})

    assert fake.calls[0]["text"] == text


def test_synth_latency_is_reported_per_hop(client):
    handle, _ = client

    response = handle.post("/speech", json={"text": "Got it."})

    assert int(response.headers["X-Synth-Ms"]) >= 0


def test_no_model_is_a_503_not_a_crash(db, tmp_path, monkeypatch):
    """A fresh checkout has no weights. The phone must get a clean refusal."""
    from fastapi.testclient import TestClient

    from app.main import app
    from speech import synth

    monkeypatch.setattr(synth, "_engine", None)
    monkeypatch.setattr(synth.config, "TTS_MODEL_DIR", tmp_path / "nothing-here")

    handle = TestClient(app)
    handle.headers["Authorization"] = f"Bearer {SHARED}"

    response = handle.post("/speech", json={"text": "Got it."})

    assert response.status_code == 503
    assert response.json()["detail"] == "tts unavailable"


def test_speech_requires_a_token(db):
    from fastapi.testclient import TestClient

    from app.main import app

    anonymous = TestClient(app)

    assert anonymous.post("/speech", json={"text": "Got it."}).status_code == 401


def test_empty_text_is_rejected(client):
    handle, _ = client

    assert handle.post("/speech", json={"text": ""}).status_code == 422


def test_health_reports_the_voice(client):
    handle, _ = client

    tts = handle.get("/health").json()["tts"]

    assert tts["available"] is True
    assert tts["loaded"] is True
    assert tts["voice"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_speech_api.py -v`
Expected: FAIL — 404 on `/speech`, `KeyError: 'tts'` on the health test.

- [ ] **Step 3: Warm the model at startup**

In `app/main.py`, add to the imports:

```python
import threading
from contextlib import asynccontextmanager
```

and to the project imports:

```python
from speech import synth
```

Then replace the app construction at `app/main.py:31`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Loading the ONNX session takes a couple of seconds. In a thread so it
    # does not hold the port closed, and unconditional because `warm` already
    # does nothing on a machine with no weights.
    threading.Thread(target=synth.warm, daemon=True).start()
    yield


app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, lifespan=lifespan)
```

- [ ] **Step 4: Add the endpoint**

Add `Response` to the `fastapi` import block at `app/main.py:16-24`. Then append to `app/main.py`:

```python
class SpeechRequest(BaseModel):
    # A reply is a sentence or two; the ceiling exists so a pasted deep-path
    # result cannot occupy the synthesizer for a minute.
    text: str = Field(min_length=1, max_length=4000)


@app.post("/speech", dependencies=[Depends(require_token)])
def speech(req: SpeechRequest) -> Response:
    """Text in, audio out. Deliberately unaware of `utterances`.

    A reply is not the only thing worth speaking — a job result or a
    notification body would use this too — and keying audio to a row would
    rule that out for no gain.

    `def`, not `async def`: onnxruntime inference is CPU-bound and would block
    the event loop. Starlette runs sync endpoints in a threadpool.
    """
    if not synth.available():
        # The phone turns this into "use the Apple voice". It is a normal
        # state on a machine that has never downloaded the weights, not an
        # error worth logging loudly.
        raise HTTPException(status_code=503, detail="tts unavailable")

    audio = synth.speak(req.text)
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"X-Synth-Ms": str(synth.last_synth_ms or 0)},
    )
```

- [ ] **Step 5: Add the health block**

At `app/main.py:139-145`, add `"tts"` to the returned dict:

```python
    return {
        "status": "ok",
        "db": db,
        "configured": config.configured(),
        "ingest": _ingest_health(),
        "pantry": pantry_health,
        "tts": _tts_health(),
    }
```

And add the helper beside `_ingest_health`:

```python
def _tts_health() -> dict:
    """Why it sounds like a screen reader again, answered without a log file.

    `available` and `loaded` differ for a few seconds after a restart while
    the warm-up thread runs, and differ permanently on a machine whose weights
    were deleted — which is the case worth being able to see.
    """
    return {
        "available": synth.available(),
        "loaded": synth.loaded(),
        "voice": config.TTS_VOICE,
        "last_synth_ms": synth.last_synth_ms,
    }
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_speech_api.py -v`
Expected: 7 passed

- [ ] **Step 7: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass. `/health` gained a key; check `tests/test_dashboard.py` and `tests/test_core.py` for an assertion on the exact shape of the health response and widen it if one breaks.

- [ ] **Step 8: Document the settings**

Append to `.env.example`, after the `DEFAULT_TZ` line:

```
# Local voice. The weights are ~310 MB and are NOT in the repo — see
# ios/README.md for the download. With them absent the server answers 503 on
# /speech and the phone falls back to Apple's synthesizer, which is a working
# configuration, just a worse-sounding one.
# Voices are Kokoro's: bm_george, bm_lewis, bm_daniel, bm_fable.
TTS_VOICE=bm_george
TTS_SPEED=1.0
```

Replace the two values with what Task 1 decided.

- [ ] **Step 9: Commit**

```bash
git add app/main.py tests/test_speech_api.py .env.example
git commit -m "feat(speech): POST /speech, warmed at startup, reported in /health"
```

---

### Task 5: The phone plays it

**Files:**
- Modify: `ios/Jarvis/Speaker.swift` (rewrite)
- Modify: `ios/Jarvis/JarvisAPI.swift` (add `speech(for:)` and the `SpeechSource` conformance)
- Modify: `ios/Jarvis/TalkView.swift:20`, `:223`, `:263`
- Modify: `ios/README.md`
- Delete: the duplicated WAV writing in `speech/audition.py` — replace it with `speech.wav.encode`
- Test: `ios/JarvisTests/SpeakerTests.swift`

The Xcode project uses file-system-synchronized groups, so a new file in `ios/JarvisTests/` is picked up without editing `project.pbxproj`.

**Interfaces:**
- Consumes: `POST /speech` returning `audio/wav`, 503 when unavailable.
- Produces:
  - `protocol SpeechSource { func audio(for text: String) async throws -> Data }`
  - `Speaker(source: SpeechSource?)`, `var source: SpeechSource?`, `func speak(_ text: String) async`, `func stop()`, `var isSpeaking: Bool`, `private(set) var didFallBack: Bool`
  - `JarvisAPI.speech(for text: String) async throws -> Data`, and `extension JarvisAPI: SpeechSource`

- [ ] **Step 1: Write the failing tests**

Create `ios/JarvisTests/SpeakerTests.swift`:

```swift
import Foundation
import Testing

@testable import Jarvis

/// The fallback, which is the whole point of the design.
///
/// A spoken reply always happens. The Mini's voice is better, but it is on
/// the other side of a network from a device that is frequently on cellular,
/// and silence while a dead server is waited on is worse than the compact
/// voice — the compact voice is at least an assistant.
///
/// `AVAudioPlayer` itself is not under test. What is under test is which of
/// the two paths gets taken.
///
/// The 3-second timeout is not covered here and cannot honestly be: it lives
/// in `URLSession`, and a timeout surfaces as exactly the thrown error
/// `Failing` already models. Task 6 Step 4 exercises it on a real device,
/// which is the only place the number means anything.
struct SpeakerTests {
    struct Failing: SpeechSource {
        struct Unreachable: Error {}
        func audio(for text: String) async throws -> Data { throw Unreachable() }
    }

    struct Garbage: SpeechSource {
        func audio(for text: String) async throws -> Data { Data("503 not a wav".utf8) }
    }

    struct Working: SpeechSource {
        func audio(for text: String) async throws -> Data { SpeakerTests.silence() }
    }

    /// A quarter-second of silence as a real 24 kHz mono WAV. Hand-built
    /// rather than checked in as a fixture: forty-four bytes of header is
    /// less to explain than a binary blob in the repo.
    static func silence(frames: Int = 6_000, sampleRate: Int = 24_000) -> Data {
        var data = Data()
        func ascii(_ text: String) { data.append(contentsOf: Array(text.utf8)) }
        func u32(_ value: Int) {
            withUnsafeBytes(of: UInt32(value).littleEndian) { data.append(contentsOf: $0) }
        }
        func u16(_ value: Int) {
            withUnsafeBytes(of: UInt16(value).littleEndian) { data.append(contentsOf: $0) }
        }

        let payload = frames * 2
        ascii("RIFF"); u32(36 + payload); ascii("WAVE")
        ascii("fmt "); u32(16); u16(1); u16(1)
        u32(sampleRate); u32(sampleRate * 2); u16(2); u16(16)
        ascii("data"); u32(payload)
        data.append(Data(count: payload))
        return data
    }

    @MainActor @Test func anUnreachableServerFallsBackToApple() async {
        let speaker = Speaker(source: Failing())

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func undecodableAudioFallsBackToApple() async {
        let speaker = Speaker(source: Garbage())

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func noConfiguredSourceFallsBackToApple() async {
        let speaker = Speaker()

        await speaker.speak("Reminder set for five o'clock.")

        #expect(speaker.didFallBack)
    }

    @MainActor @Test func serverAudioIsPlayedInsteadOfApple() async {
        let speaker = Speaker(source: Working())

        await speaker.speak("Reminder set for five o'clock.")

        #expect(!speaker.didFallBack)
    }

    @MainActor @Test func emptyTextSaysNothingAtAll() async {
        let speaker = Speaker(source: Working())

        await speaker.speak("")

        #expect(!speaker.isSpeaking)
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
xcodebuild test -project ios/Jarvis.xcodeproj -scheme Jarvis \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' 2>&1 | tail -30
```
Expected: compile failure — no `SpeechSource`, no `Speaker.didFallBack`, `speak` is not `async`.

- [ ] **Step 3: Rewrite Speaker**

Replace `ios/Jarvis/Speaker.swift` entirely:

```swift
import AVFoundation
import Foundation

/// Where spoken audio comes from.
///
/// A protocol rather than a direct `JarvisAPI` reference so `SpeakerTests` can
/// drive both paths without a server or an audio device.
protocol SpeechSource {
    func audio(for text: String) async throws -> Data
}

/// Speech out.
///
/// The server guarantees `reply` is a single plain-text string with no
/// markdown, lists, or emoji — written to be spoken. So this does no
/// processing at all; anything it "fixed up" would be papering over a server
/// bug that should be fixed at the source.
///
/// Two paths. The Mini synthesizes a British neural voice and this plays the
/// WAV; if that fails for any reason at all, `AVSpeechSynthesizer` speaks the
/// same string. The fallback is unconditional on purpose — a spoken reply
/// always happens, which is the same invariant `notify.push()` protects by
/// returning a bool instead of raising.
@MainActor
final class Speaker: ObservableObject {
    private let synthesizer = AVSpeechSynthesizer()
    private var player: AVAudioPlayer?

    /// Set once by `TalkView`, which gets `JarvisAPI` from the environment and
    /// so cannot pass it to a `@StateObject`'s initializer.
    var source: SpeechSource?

    /// Whether the last utterance used Apple's voice. Tests read it; so could
    /// a debug screen, if "why does it sound wrong today" ever needs an answer
    /// on the device rather than in `/health`.
    private(set) var didFallBack = false

    init(source: SpeechSource? = nil) {
        self.source = source
    }

    var isSpeaking: Bool { synthesizer.isSpeaking || (player?.isPlaying ?? false) }

    func speak(_ text: String) async {
        guard !text.isEmpty else { return }
        stop()
        activateSession()

        // `try?` swallows the error deliberately: every failure mode here —
        // no source, transport, 503, a body that is not a WAV — has the same
        // remedy, and distinguishing them would only be to log them.
        if let source, let data = try? await source.audio(for: text), play(data) {
            didFallBack = false
            return
        }

        didFallBack = true
        speakLocally(text)
    }

    func stop() {
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        player?.stop()
        player = nil
    }

    private func activateSession() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(
            .playback, mode: .spokenAudio, options: [.duckOthers]
        )
        try? session.setActive(true)
    }

    private func play(_ data: Data) -> Bool {
        guard let player = try? AVAudioPlayer(data: data) else { return false }
        self.player = player
        return player.play()
    }

    private func speakLocally(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = Self.preferredVoice()
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        synthesizer.speak(utterance)
    }

    /// Prefer a premium/enhanced voice when the user has downloaded one —
    /// the default compact voice is noticeably robotic for full sentences.
    private static func preferredVoice() -> AVSpeechSynthesisVoice? {
        let language = AVSpeechSynthesisVoice.currentLanguageCode()
        let candidates = AVSpeechSynthesisVoice.speechVoices()
            .filter { $0.language == language }
        return candidates.first { $0.quality == .premium }
            ?? candidates.first { $0.quality == .enhanced }
            ?? AVSpeechSynthesisVoice(language: language)
    }
}
```

- [ ] **Step 4: Add the client call**

In `ios/Jarvis/JarvisAPI.swift`, add beside the other request methods (after `uploadReceipt`, which is the other non-JSON one):

```swift
    /// Audio for a reply, in the Mini's local voice.
    ///
    /// Three seconds, deliberately short: this is racing `AVSpeechSynthesizer`,
    /// which is sitting right there and costs nothing. Waiting longer than that
    /// on a dead server buys silence where a worse voice would do.
    ///
    /// Unlike `send`, this touches neither `isReachable` nor `isUnauthorized`.
    /// A voice that failed is not an enrollment problem and must not put the
    /// app into a re-enrol state over it.
    func speech(for text: String) async throws -> Data {
        guard !host.isEmpty, let credential = deviceToken, !credential.isEmpty else {
            throw APIError.notConfigured
        }
        let authority = host.contains(":") ? host : "\(host):8000"
        guard let url = URL(string: "http://\(authority)/speech") else {
            throw APIError.notConfigured
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(credential)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
        request.timeoutInterval = 3

        let (data, response) = try await session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw APIError.server(status, "no voice")
        }
        return data
    }
```

And at the end of the file:

```swift
extension JarvisAPI: SpeechSource {
    func audio(for text: String) async throws -> Data { try await speech(for: text) }
}
```

- [ ] **Step 5: Wire it into TalkView**

`ios/Jarvis/TalkView.swift:263` — `speak` is now async:

```swift
            await speaker.speak(response.reply)
```

`ios/Jarvis/TalkView.swift:223` — `stop()` is unchanged, leave it.

Attach the source once the view has the environment object. `ios/Jarvis/TalkView.swift:91` currently reads `.task { await startIfRequested() }` — make it:

```swift
        .task {
            speaker.source = api
            await startIfRequested()
        }
```

Do not add a second `.task`; `api` is an `@EnvironmentObject` and is not available in the `@StateObject` initializer at line 20, which is why this is an assignment rather than `Speaker(source: api)`.

- [ ] **Step 6: Run the tests to verify they pass**

Run:
```bash
xcodebuild test -project ios/Jarvis.xcodeproj -scheme Jarvis \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' 2>&1 | tail -30
```
Expected: all tests pass, including the five in `SpeakerTests`.

If `serverAudioIsPlayedInsteadOfApple` fails because `player.play()` returned false in the simulator, that is a real finding about playback and not a test to weaken — report it rather than changing `play` to ignore its return value.

- [ ] **Step 7: Drop the duplicated WAV writing**

`speech/audition.py` hand-rolled its own encoder in Task 1 because `speech/wav.py` did not exist yet. Replace the body of the inner write with the real thing:

```python
from speech import wav as wavfile
...
            path.write_bytes(wavfile.encode(samples, rate))
```

and remove the now-unused `import wave as wavelib`.

Run: `uv run python -c "import speech.audition"`
Expected: no output, no ImportError.

- [ ] **Step 8: Document it on the iOS side**

In `ios/README.md`, update the `Speaker.swift` row of the layout table:

```
| `Speaker.swift` | Plays the Mini's neural voice; falls back to `AVSpeechSynthesizer` |
```

And add a section after "Before the first build":

```markdown
## The voice

Replies are spoken by Kokoro-82M running on the Mini, not by the phone. The
weights are ~310 MB and are not in the repo:

    VOICES="$HOME/Library/Application Support/jarvis/voices"
    mkdir -p "$VOICES"
    curl -L -o "$VOICES/kokoro-v1.0.onnx" \
      https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
    curl -L -o "$VOICES/voices-v1.0.bin" \
      https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

Without them the server answers 503 on `/speech` and the phone uses Apple's
synthesizer. That is a working configuration — it just sounds like a screen
reader, which is the thing this exists to fix. `GET /health` reports which
one you are getting.
```

If Task 1 Step 6 needed `brew install espeak-ng`, add that line above the `curl`s. If it did not, say nothing about espeak-ng.

- [ ] **Step 9: Commit**

```bash
git add ios/Jarvis/Speaker.swift ios/Jarvis/JarvisAPI.swift ios/Jarvis/TalkView.swift \
        ios/JarvisTests/SpeakerTests.swift ios/README.md speech/audition.py
git commit -m "feat(ios): play the Mini's voice, fall back to AVSpeechSynthesizer"
```

---

### Task 6: End to end on the Mini, and the measurement

The design's last open question is whether the gap between text and speech is annoying enough to justify streaming. Only a real device on a real network answers it.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Start the server with the weights present**

Run: `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`
Expected: starts immediately. The ONNX load happens in the warm-up thread, so the port is open before it finishes.

- [ ] **Step 2: Confirm health reports a loaded voice**

Run:
```bash
sleep 5 && curl -s localhost:8000/health -H "Authorization: Bearer $JARVIS_TOKEN" \
  | python -m json.tool
```
Expected: `"tts": {"available": true, "loaded": true, "voice": "...", "last_synth_ms": null}`

If `loaded` is false after five seconds, the warm-up thread threw. Run `synth.warm()` in a REPL to see the exception — a daemon thread's traceback goes nowhere useful.

- [ ] **Step 3: Measure a real synthesis**

```bash
curl -s -D /tmp/h -o /tmp/reply.wav -X POST localhost:8000/speech \
  -H "Authorization: Bearer $JARVIS_TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"You have three things tomorrow. Dentist at nine, standup at ten thirty, and dinner with Sam at seven."}'
grep -i x-synth-ms /tmp/h
afplay /tmp/reply.wav
```

Expected: audio plays in the chosen voice. **Record the `X-Synth-Ms` value** — it goes in the commit message and decides whether streaming is ever worth building.

- [ ] **Step 4: Confirm the fallback on a device**

On the phone, with the app enrolled: turn off Wi-Fi and cellular, tap the mic, say something. The reply request fails first, so nothing is spoken — that is expected and unchanged.

The case that matters is a reachable server with no voice: stop uvicorn, move the `voices` directory aside, restart, and say something. Expected: the reply text renders and Apple's synthesizer speaks it, within the 3-second budget rather than after a long silence.

Move the directory back afterwards.

- [ ] **Step 5: Record the facts worth not rediscovering**

Add to `CLAUDE.md`, as a new section after "Pantry":

```markdown
## Voice

Replies are synthesized on the Mini by Kokoro-82M over onnxruntime and played
by the phone; Apple's `AVSpeechSynthesizer` is the fallback. Two round trips —
`/say` returns text on its old budget, `/speech` returns audio separately —
because folding synthesis into `/say` would put half a second inside the
endpoint whose p95 is the system's headline number.

- **The filesystem is the switch.** No `TTS_ENABLED`. If the weights are not
  in `$JARVIS_DB`'s parent `voices/` directory, `/speech` answers 503 and the
  phone uses the Apple voice. A flag that can disagree with the filesystem is
  a flag that eventually will.
- **The fallback is unconditional and the timeout is 3s.** Every failure mode
  — unreachable, 503, a body that isn't a WAV — takes the same path, because
  they have the same remedy. Silence while a dead server is waited on is worse
  than the compact voice.
- **`speak()` holds a reentrant lock.** It calls `engine()`, which takes the
  same lock to build the session lazily. A plain `Lock` deadlocks on the first
  synthesis after a cold start, and only after a cold start — which is the
  worst kind of bug to ship.
- **`/speech` is a `def`, not an `async def`.** onnxruntime inference is
  CPU-bound; a coroutine would block the event loop and stall every other
  request. Starlette runs sync endpoints in a threadpool.
- **Voice and pace are `.env`, not code.** `TTS_VOICE` and `TTS_SPEED`. The
  four British males are `bm_george`, `bm_lewis`, `bm_daniel`, `bm_fable`;
  which one is right was decided by listening, and re-deciding costs one line.
- **Neither the weights nor synthesis appear in `/metrics`.** That block is
  per-utterance and costed against API spend; local synthesis has no token
  cost. `X-Synth-Ms` and `/health` carry the latency instead.
```

- [ ] **Step 6: Run everything one more time**

Run: `uv run pytest -q`
Expected: all pass.

Run:
```bash
xcodebuild test -project ios/Jarvis.xcodeproj -scheme Jarvis \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' 2>&1 | tail -20
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record how the local voice works and why

Synthesis measured at <X-Synth-Ms from Step 3>ms for a three-clause reply."
```

---

## Deliberately not built

Both were considered in the spec and rejected until there is evidence for them. If Task 6 Step 3 shows synthesis over ~1.5s, reopen the first.

- **Streaming.** Sentence-by-sentence synthesis, first audio ~300 ms out instead of ~700 ms, at the cost of chunked playback on iOS and a streaming response on the server.
- **Caching.** Templated confirmations repeat, so a hash-keyed WAV cache is easy. Premature until a measured synth time says the repeat is worth avoiding.
- **Diction.** "Certainly, sir" rather than "Added to the pantry." A separate change to the Python reply templates, and a separate judgement.
