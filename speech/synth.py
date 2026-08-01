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
