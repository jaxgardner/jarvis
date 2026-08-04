"""Text to speech, on this machine.

The weights are 310 MB and live outside the repo, so a fresh checkout that has
never run the download simply cannot speak: `available()` is False, /speech
answers 503, and the phone falls back to Apple's synthesizer. That is the
switch. There is no TTS_ENABLED, because a flag that can disagree with the
filesystem is a flag that will.

A reply is synthesized in pieces and handed out as the pieces finish. Two
things follow from that, and they are the reason this module is more than a
wrapper around `Kokoro.create`:

  - **Synthesis is decoupled from the request.** `ensure()` starts a worker
    thread; readers only wait on its output. So /say can start synthesizing a
    reply the moment it has one, and the phone's /speech request — which
    arrives a beat later — attaches to work already in progress instead of
    starting it. Measured, that head start is about 140ms.
  - **Chunks are published as they are produced**, not collected and returned.
    A reader that attaches mid-synthesis gets everything so far and then
    blocks for the rest, so the phone can be playing chunk one while chunk two
    is still in the model.

One utterance is cached, not many. The only text ever prefetched is the reply
the phone is about to ask for, so a second slot would never be read.
"""

import threading
import time
from collections.abc import Iterator
from pathlib import Path

from app import config
from speech import clone, segment, wav

# Piper carries its phonemization language inside its own `.onnx.json`, as
# `espeak.voice`. There is deliberately no `lang_for` here any more: with
# Kokoro the language had to be derived from the voice name because nothing
# else knew it, and a mismatch was an American voice reading British phonemes
# — wrong in a way no log would show. Piper ships the answer beside the
# weights, so the two can no longer disagree at all, which is strictly better
# than deriving it correctly.

# Reentrant because `_synthesize` holds it across `engine()`, which takes it
# too. A plain Lock deadlocks on the first synthesis after a cold start.
_lock = threading.RLock()
_engine = None

# Guards the one-utterance slot. Deliberately a different lock from `_lock`:
# claiming the slot must not wait behind an inference that is already running,
# or /say's prefetch would block on the previous reply finishing.
_slot_lock = threading.Lock()
_current: "_Utterance | None" = None

# No chunk takes anywhere near this long — it is here so that a wedged worker
# costs one request rather than a threadpool slot forever.
_CHUNK_TIMEOUT = 30.0

# Read by /health. Process-local and deliberately not persisted: the question
# it answers is "is the voice working right now", not "what was p95 in June".
last_synth_ms: int | None = None

# The number that actually decides whether replies feel immediate. `speak()`
# is a whole reply; this is how long until there was something to play, which
# is what the phone waits for and what chunking exists to shrink.
last_first_chunk_ms: int | None = None


def paths() -> tuple[Path, Path]:
    """The Piper weights and the config that travels with them.

    Piper resolves the `.onnx.json` itself by appending to the model path, so
    these two are never configured separately and cannot drift apart.
    """
    model = config.TTS_MODEL_DIR / "piper" / f"{config.TTS_MODEL}.onnx"
    return model, model.with_suffix(".onnx.json")


def _files_present() -> bool:
    """Both stages, or neither.

    Piper without the converter would speak in the wrong voice rather than
    fail, and that is the one outcome worth ruling out here: a half-installed
    machine should take the Apple fallback, not quietly ship a different
    assistant.
    """
    return all(path.exists() for path in paths()) and clone.files_present()


def loaded() -> bool:
    """Both stages. Half a pipeline cannot produce a reply."""
    return _engine is not None and clone.loaded()


def available() -> bool:
    """Can this machine speak? True once the files are on disk.

    An already-installed engine counts, which is what lets tests exercise the
    endpoint without 310 MB of weights.
    """
    return loaded() or _files_present()


def engine():
    """The Piper voice. The converter is loaded separately, by `clone`.

    Two engines, built independently, because they fail independently: a
    missing reference clip is not a reason to have no synthesizer, and the
    error each produces should name the file that is actually absent.
    """
    global _engine
    with _lock:
        if _engine is None:
            from piper import PiperVoice

            model, _ = paths()
            _engine = PiperVoice.load(str(model))
        return _engine


def warm() -> None:
    """Build the session *and run one inference through it*, ahead of the
    first reply.

    Loading is a couple of seconds. Paying that on the first thing you say
    after a reboot is exactly the impression this feature exists to avoid.

    The throwaway synthesis is not redundant: onnxruntime defers a good deal
    of work to the first call — graph optimization, arena allocation — and
    measured on the Mini that first call cost an extra 850ms even with the
    session already built. Loading the model without ever using it moved the
    cost rather than removing it, and moved it onto the first thing you say.

    It matters more now than it did under Kokoro, not less. There are three
    models to build — Piper, Kanade and the vocoder — and the throwaway pass
    is the only thing that touches all of them, plus torch's own lazy kernel
    selection on the first convolution.

    Safe to call on a machine with no model files: it does nothing.
    """
    if loaded() or not _files_present():
        return
    engine()
    try:
        _synthesize("Ready.")
    except Exception:  # noqa: BLE001 — a warm-up is never worth a crash
        # A missing or unreadable reference clip lands here. /speech will
        # report it properly the first time it is asked; a background thread
        # at startup is not the place to raise it.
        pass


def speak(text: str) -> bytes:
    """Synthesize `text` as one WAV. Blocking, CPU-bound, and unchunked.

    The whole-utterance path: no seams, no streaming, and the reply is not
    heard until all of it exists. /speech uses `stream()` instead; this stays
    because it is the honest answer to "what does this voice sound like with
    nothing clever in the way", and it is what `TTS_STREAM_CHUNKS=0` falls
    back to.

    `text` is passed through untouched. The server guarantees replies are
    plain and speakable; anything "corrected" here would hide a bug that
    belongs upstream.
    """
    global last_synth_ms
    started = time.monotonic()
    payload = _synthesize(text)
    last_synth_ms = int((time.monotonic() - started) * 1000)
    return payload


def _synthesize(text: str) -> bytes:
    """One utterance, spoken and then re-voiced. Both models, in order.

    Piper is fast enough that the conversion dominates — about 40ms against
    385ms for a clause. That ratio is why the two stages are not worth
    parallelizing or interleaving: there is nothing to hide behind.
    """
    from piper import SynthesisConfig

    voice = engine()

    # `normalize_audio` defaults on and levels each call to full scale, which
    # across a chunked reply means every clause arrives equally loud and the
    # seams become steps in level. `length_scale` is Piper's inverse of
    # TTS_SPEED; dividing the model's own default keeps the voice's intended
    # pace as the 1.0 case.
    syn = SynthesisConfig(
        normalize_audio=False,
        length_scale=voice.config.length_scale / config.TTS_SPEED,
    )

    with _lock:
        chunks = list(voice.synthesize(text, syn_config=syn))
        spoken = b"".join(chunk.audio_int16_bytes for chunk in chunks)
        source_rate = chunks[0].sample_rate

    converted = clone.convert(clone.from_pcm16(spoken, source_rate))
    return wav.encode_pcm16(clone.to_pcm16(converted), clone.rate())


def _pieces(text: str) -> list[str]:
    if not config.TTS_STREAM_CHUNKS:
        return [text]
    return segment.segments(text) or [text]


class _Utterance:
    """One reply being synthesized, readable while it still is.

    A single producer appends chunks; any number of readers walk the list from
    the beginning and block at the end until there is more or the producer is
    done. The producer's exception is re-raised in every reader, so a failure
    reaches /speech as a failure rather than as a truncated reply.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self._chunks: list[bytes] = []
        self._done = False
        self._error: BaseException | None = None
        self._ready = threading.Condition()

    def publish(self, chunk: bytes) -> None:
        with self._ready:
            self._chunks.append(chunk)
            self._ready.notify_all()

    def close(self, error: BaseException | None = None) -> None:
        with self._ready:
            self._error = error
            self._done = True
            self._ready.notify_all()

    def complete(self) -> bool:
        with self._ready:
            return self._done

    def read(self) -> Iterator[bytes]:
        index = 0
        while True:
            with self._ready:
                while index >= len(self._chunks) and not self._done:
                    if not self._ready.wait(timeout=_CHUNK_TIMEOUT):
                        raise TimeoutError("synthesis stalled")
                if index >= len(self._chunks):
                    if self._error is not None:
                        raise self._error
                    return
                chunk = self._chunks[index]
                index += 1
            # Outside the lock: a slow reader must not stall the producer.
            yield chunk


def _fill(entry: _Utterance) -> None:
    global last_synth_ms, last_first_chunk_ms
    started = time.monotonic()
    first_ms: int | None = None
    error: BaseException | None = None
    try:
        for piece in _pieces(entry.text):
            entry.publish(_synthesize(piece))
            if first_ms is None:
                # Published here rather than with `last_synth_ms` at the end,
                # because /speech reads it while writing its headers — which
                # happens as soon as the first chunk exists, long before the
                # last one does. Setting it late would put the *previous*
                # reply's number on this reply's response.
                first_ms = int((time.monotonic() - started) * 1000)
                last_first_chunk_ms = first_ms
    except BaseException as exc:  # noqa: BLE001 — re-raised in every reader
        error = exc
    finally:
        # In `finally` so a reader can never be left waiting on a worker that
        # died: every exit from this function releases them.
        entry.close(error)

    if error is None:
        last_synth_ms = int((time.monotonic() - started) * 1000)


def ensure(text: str) -> _Utterance:
    """The utterance for `text`, starting synthesis if nothing else has.

    Returns immediately — the work happens on a worker thread. Calling this
    twice with the same text is what makes the prefetch worthwhile: the second
    caller attaches to the first caller's work instead of repeating it.
    """
    global _current
    with _slot_lock:
        current = _current
        if current is not None and current.text == text:
            return current
        entry = _Utterance(text)
        _current = entry
    threading.Thread(target=_fill, args=(entry,), daemon=True).start()
    return entry


def prefetch(text: str) -> None:
    """Start synthesizing a reply before the phone asks for it.

    Called from /say, which must not grow a synthesis-shaped failure mode: a
    machine with no weights, an unspeakable voice, or a dead worker all have
    to leave /say exactly as it was. Hence the bare except — there is no
    failure here worth failing a reply over, because /speech will report it
    honestly a moment later and the phone will use the Apple voice.
    """
    if not text or not available():
        return
    try:
        ensure(text)
    except Exception:  # noqa: BLE001 — /say never fails over speech
        pass


def stream(text: str) -> Iterator[bytes]:
    """Chunks of `text` as WAVs, in order, as they finish.

    The first chunk is the one that matters: everything after it is produced
    faster than the phone can play what came before.
    """
    return ensure(text).read()
