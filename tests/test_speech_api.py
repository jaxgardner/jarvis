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
