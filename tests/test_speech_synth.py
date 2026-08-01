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
