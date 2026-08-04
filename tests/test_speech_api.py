"""The endpoint the phone calls after the reply text has already landed.

Two round trips is the design: /say keeps its latency and its contract, and
audio is fetched separately. So the interesting cases here are the failure
ones — an unavailable model must be a clean 503, because that is the signal
the phone turns into "use the Apple voice" rather than "say nothing".

The body is a sequence of length-prefixed WAVs rather than one WAV, so that
the phone can start playing the first clause while the rest is still in the
model. That makes *when* a failure is detectable the thing worth pinning down:
a streaming response has already committed to 200 by the time its second chunk
is produced, so everything that can fail has to fail before the headers.
"""

import io
import types
import wave

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


class FakePiper:
    def __init__(self):
        self.calls = []
        self.config = types.SimpleNamespace(length_scale=1.15)

    def synthesize(self, text, syn_config=None):
        self.calls.append({"text": text, "syn_config": syn_config})
        yield types.SimpleNamespace(
            audio_int16_bytes=b"\x00\x00" * 2_400, sample_rate=22_050
        )


def unframe(body: bytes) -> list[bytes]:
    """The wire format, read back: 4-byte big-endian length, then that many
    bytes of WAV, repeated to the end."""
    chunks, offset = [], 0
    while offset < len(body):
        size = int.from_bytes(body[offset : offset + 4], "big")
        offset += 4
        chunks.append(body[offset : offset + size])
        offset += size
    return chunks


def frames(chunk: bytes) -> int:
    with wave.open(io.BytesIO(chunk), "rb") as audio:
        assert audio.getframerate() == 24_000
        assert audio.getnchannels() == 1
        return audio.getnframes()


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
    from speech import clone, synth

    fake = FakePiper()
    monkeypatch.setattr(synth, "_engine", fake)
    # The prefetch slot is module state and outlives a test. Left alone, a
    # second test asking for the same text would be served audio the previous
    # test's engine produced.
    monkeypatch.setattr(synth, "_current", None)
    stub_conversion(monkeypatch)

    handle = TestClient(app)
    handle.headers["Authorization"] = f"Bearer {SHARED}"
    return handle, fake


def stub_conversion(monkeypatch):
    """Pass audio through the conversion stage untouched.

    The real one loads a codec, a vocoder and torch. None of that changes what
    these tests assert — framing, chunk boundaries, status codes — and all of
    it would make the suite depend on half a gigabyte of weights.
    """
    from speech import clone

    monkeypatch.setattr(clone, "from_pcm16", lambda pcm, rate: pcm)
    monkeypatch.setattr(clone, "convert", lambda samples: samples)
    monkeypatch.setattr(clone, "to_pcm16", lambda samples: samples)
    monkeypatch.setattr(clone, "rate", lambda: 24_000)
    monkeypatch.setattr(clone, "loaded", lambda: True)


def test_speech_returns_playable_wav_chunks(client):
    handle, _ = client

    response = handle.post("/speech", json={"text": "Reminder set for five."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/x-jarvis-chunked-wav"
    chunks = unframe(response.content)
    assert chunks
    assert all(frames(chunk) == 2_400 for chunk in chunks)


def test_a_reply_is_cut_at_its_pause_so_playback_can_start_early(client):
    """The point of the whole arrangement: the first chunk is short.

    "Got it —" is its own utterance, so the phone has something to play after
    one short synthesis instead of after the whole sentence.
    """
    handle, fake = client

    response = handle.post(
        "/speech",
        json={"text": "Got it — I'll remind you to call the dentist tomorrow at nine."},
    )

    assert len(unframe(response.content)) == 2
    assert fake.calls[0]["text"] == "Got it —"
    assert fake.calls[1]["text"] == "I'll remind you to call the dentist tomorrow at nine."


def test_the_reply_text_is_not_rewritten_on_its_way_to_the_voice(client):
    """Cut, never edited. Rejoining the pieces gives back the words.

    5:30 is the case worth naming: a colon is a place this is allowed to cut,
    but not inside a time, and nothing may quietly become "5 30".
    """
    handle, fake = client
    text = "You're out of milk — and it's 5:30."

    handle.post("/speech", json={"text": text})

    spoken = " ".join(call["text"] for call in fake.calls)
    assert spoken == text


def test_unchunked_mode_speaks_the_reply_whole(client, monkeypatch):
    """TTS_STREAM_CHUNKS=0 is the escape hatch if the seams ever sound wrong."""
    handle, fake = client
    monkeypatch.setattr("app.config.TTS_STREAM_CHUNKS", False)
    text = "Got it — I'll remind you to call the dentist tomorrow at nine."

    response = handle.post("/speech", json={"text": text})

    assert len(unframe(response.content)) == 1
    assert [call["text"] for call in fake.calls] == [text]


def test_time_to_first_audio_is_reported_per_hop(client):
    handle, _ = client

    response = handle.post("/speech", json={"text": "Got it."})

    assert int(response.headers["X-Synth-First-Ms"]) >= 0


def test_a_voice_that_cannot_be_spoken_is_a_503_not_a_broken_stream(client, monkeypatch):
    """The failure has to land before the headers do.

    An unreadable reference clip raises inside the conversion stage, which
    runs after Piper has already produced audio. If that surfaced mid-stream
    the phone would already hold a 200 and some audio, and could not honestly
    fall back — so /speech pulls the first chunk before it answers at all.
    """
    from speech import clone

    handle, _ = client

    def unreadable(samples):
        raise RuntimeError("reference clip is not audio")

    monkeypatch.setattr(clone, "convert", unreadable)

    response = handle.post("/speech", json={"text": "Got it."})

    assert response.status_code == 503
    assert "reference clip" in response.json()["detail"]


def test_say_starts_the_voice_before_the_phone_asks(db, monkeypatch):
    """/say hands the reply to the synthesizer on its way out.

    Worth about 140ms — the round trip the phone spends deciding to ask for
    audio is time the model can already be working.
    """
    from fastapi.testclient import TestClient

    from app import router
    from app.main import app
    from speech import synth

    fake = FakePiper()
    monkeypatch.setattr(synth, "_engine", fake)
    monkeypatch.setattr(synth, "_current", None)
    stub_conversion(monkeypatch)
    monkeypatch.setattr(
        router,
        "route",
        lambda text, tz, reports=(), projects=(), today="", context="": (
            "add_note",
            {"body": "buy stamps"},
        ),
    )

    handle = TestClient(app)
    handle.headers["Authorization"] = f"Bearer {SHARED}"

    reply = handle.post("/say", json={"text": "note to buy stamps"}).json()["reply"]

    entry = synth._current
    assert entry is not None and entry.text == reply


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
    # Both halves of the voice: the model speaks, the reference gives it its
    # timbre. Either being wrong sounds the same from the phone.
    assert tts["model"]
    assert tts["reference"]
