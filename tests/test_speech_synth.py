"""Loading, availability, and the fact that text arrives unaltered.

The weights run to hundreds of megabytes across two models and CI does not
have them, so every test here runs against doubles: one at `synth._engine`
for Piper, and stubs over `speech.clone` for the conversion. That also keeps
torch out of the suite, which matters — it is a real dependency now, and a
test run should not be loading a codec to check that text is not rewritten.

What is actually under test is the availability logic, the parameters the
voice is driven with, and the promise that nothing rewrites the server's text
on its way to the engine.
"""

import io
import wave

import pytest

from speech import clone, synth


class FakeChunk:
    def __init__(self, pcm, rate):
        self.audio_int16_bytes = pcm
        self.sample_rate = rate


class FakePiperConfig:
    # Real jarvis-high ships 1.15, and `_synthesize` divides by TTS_SPEED.
    length_scale = 1.15


class FakePiper:
    """A tenth of a second of silence, at Piper's real output rate."""

    def __init__(self):
        self.calls = []
        self.config = FakePiperConfig()

    def synthesize(self, text, syn_config=None):
        self.calls.append({"text": text, "syn_config": syn_config})
        yield FakeChunk(b"\x00\x00" * 2_400, 22_050)


@pytest.fixture
def engine(monkeypatch):
    """Piper doubled, conversion stubbed to a pass-through.

    The stubs are deliberately identity functions rather than mocks: the
    conversion's job is to change how audio sounds, which no assertion here
    could check, so the useful thing is that bytes survive the round trip and
    the frame count is preserved.
    """
    fake = FakePiper()
    monkeypatch.setattr(synth, "_engine", fake)
    monkeypatch.setattr(synth, "last_synth_ms", None)
    # The prefetch slot is module state and outlives a test.
    monkeypatch.setattr(synth, "_current", None)

    monkeypatch.setattr(clone, "from_pcm16", lambda pcm, rate: pcm)
    monkeypatch.setattr(clone, "convert", lambda samples: samples)
    monkeypatch.setattr(clone, "to_pcm16", lambda samples: samples)
    monkeypatch.setattr(clone, "rate", lambda: 24_000)
    monkeypatch.setattr(clone, "loaded", lambda: True)
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


def test_chunks_are_not_individually_levelled(engine):
    """`normalize_audio` off, or the seams become steps in loudness.

    Piper defaults it on and levels every *call* to full scale. A reply is
    synthesized one clause per call, so leaving it on makes a quiet clause and
    a loud one arrive equally loud — audible at exactly the joins the chunking
    was designed to hide.
    """
    synth.speak("Got it.")

    assert engine.calls[-1]["syn_config"].normalize_audio is False


def test_speed_maps_onto_pipers_inverse_scale(engine, monkeypatch):
    """TTS_SPEED is a multiplier; `length_scale` is its reciprocal.

    Larger is *slower* in Piper, so asking for 2.0 and getting audio at twice
    the length would be the exact opposite of the request. The model's own
    default is the 1.0 case, so the voice's intended pace survives.
    """
    monkeypatch.setattr(synth.config, "TTS_SPEED", 1.0)
    synth.speak("Got it.")
    assert engine.calls[-1]["syn_config"].length_scale == pytest.approx(1.15)

    monkeypatch.setattr(synth.config, "TTS_SPEED", 2.0)
    synth.speak("Got it.")
    assert engine.calls[-1]["syn_config"].length_scale == pytest.approx(0.575)


def test_output_carries_the_converters_rate_not_pipers(engine):
    """Piper speaks at 22.05kHz; what reaches the phone is the vocoder's 24kHz.

    Labelling the WAV with the source rate would play every reply about 9%
    slow and a semitone flat — wrong in a way that sounds like a bad voice
    rather than like a bug.
    """
    payload = synth.speak("Got it.")

    with wave.open(io.BytesIO(payload), "rb") as handle:
        assert handle.getframerate() == 24_000


def test_half_an_engine_is_not_available(monkeypatch, tmp_path):
    """Piper present, reference clip absent.

    Speaking anyway would produce Piper's own voice rather than the
    assistant's — a different voice, confidently delivered. The Apple fallback
    is the better answer, so availability requires both stages.
    """
    monkeypatch.setattr(synth, "_engine", None)
    monkeypatch.setattr(synth.config, "TTS_MODEL_DIR", tmp_path)
    piper = tmp_path / "piper"
    piper.mkdir()
    (piper / f"{synth.config.TTS_MODEL}.onnx").write_bytes(b"x")
    (piper / f"{synth.config.TTS_MODEL}.onnx.json").write_bytes(b"{}")

    assert clone.files_present() is False
    assert synth.available() is False


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


def test_warm_runs_an_inference_rather_than_only_loading(monkeypatch):
    """Building the session is not the expensive half.

    onnxruntime defers graph optimization and arena allocation to the first
    call — 850ms of it, measured on the Mini. A warm-up that only loads the
    model moves that cost onto the first thing you say after a reboot, which
    is the one thing warming is for.
    """
    fake = FakePiper()
    monkeypatch.setattr(synth, "_engine", None)
    monkeypatch.setattr(synth, "_files_present", lambda: True)
    monkeypatch.setattr(synth, "engine", lambda: fake)
    monkeypatch.setattr(clone, "loaded", lambda: False)
    monkeypatch.setattr(clone, "from_pcm16", lambda pcm, rate: pcm)
    monkeypatch.setattr(clone, "convert", lambda samples: samples)
    monkeypatch.setattr(clone, "to_pcm16", lambda samples: samples)
    monkeypatch.setattr(clone, "rate", lambda: 24_000)

    synth.warm()

    assert fake.calls, "warm() loaded the model without ever running it"


def test_a_warm_up_never_takes_the_process_down(monkeypatch):
    """It runs on a background thread at startup.

    An unreadable reference clip is a real misconfiguration — but /speech is
    where it should surface, as a 503 the phone turns into the Apple voice,
    not as an exception on a daemon thread nobody is watching.
    """
    fake = FakePiper()
    monkeypatch.setattr(synth, "_engine", None)
    monkeypatch.setattr(synth, "_files_present", lambda: True)
    monkeypatch.setattr(synth, "engine", lambda: fake)
    monkeypatch.setattr(clone, "loaded", lambda: False)

    def unreadable(pcm, rate):
        raise RuntimeError("no such reference clip")

    monkeypatch.setattr(clone, "from_pcm16", unreadable)

    synth.warm()  # must not raise


def test_a_prefetched_reply_is_not_synthesized_a_second_time(engine):
    """The point of the cache: /speech attaches to /say's work.

    One `create` call per chunk. Two would mean the phone's request started
    over instead of joining what was already running.
    """
    reply = "Got it — I'll remind you to call the dentist tomorrow at nine."
    synth.prefetch(reply)

    chunks = list(synth.stream(reply))

    assert len(chunks) == 2
    assert len(engine.calls) == 2


def test_chunks_arrive_before_the_whole_reply_is_finished(engine):
    """A reader attaching mid-synthesis gets what exists so far.

    This is what lets the phone play the first clause while the second is
    still in the model — if `stream` collected everything before yielding,
    chunking would buy nothing.
    """
    entry = synth._Utterance("two chunks")
    entry.publish(b"first")
    reader = entry.read()

    assert next(reader) == b"first"  # returns without waiting for the rest

    entry.publish(b"second")
    entry.close()
    assert list(reader) == [b"second"]
