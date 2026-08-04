"""Pre-retrieval, end to end against live Haiku.

Costs a few cents per run. Skips automatically without an API key, like
tests/test_utterances.py, whose fixture shape this follows.
"""

import pytest

from app import config
from app.db import connect

pytestmark = pytest.mark.skipif(
    not config.configured()["anthropic_api_key"],
    reason="needs ANTHROPIC_API_KEY (live router calls)",
)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    import migrate
    from app.main import app

    assert migrate.migrate() == 0, "migrations failed"
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {config.jarvis_token()}"
        yield c


def say(client, text: str) -> dict:
    response = client.post("/say", json={"text": text, "client": "test"})
    assert response.status_code == 200, response.text
    return response.json()


def row_for(utterance_id: int):
    conn = connect()
    try:
        return conn.execute(
            "SELECT model_calls, intent FROM utterances WHERE id = ?", (utterance_id,)
        ).fetchone()
    finally:
        conn.close()


def test_context_answers_in_one_call(client):
    """The whole point: a question whose answer is in a note should cost one
    model call, not two.

    Asserted over five attempts, not one, because this is a live model's
    choice and the rate is what the feature actually is. Measured at the time
    of writing: 7 of 8 for this phrasing, and 0 of 8 for "what did I say
    about Sarah" — a person-subject recall stays on `query`, which has a
    `kind='recall'` path tuned for exactly that shape and wins against
    CONTEXT every time. So the win is real and partial, and a single-shot
    assertion here was passing on luck: it failed on the second full-suite
    run after being written.

    One `answer` is enough to prove the path works end to end. What the rate
    is worth is a separate question, and `/metrics` answers it from real
    traffic rather than from three cents of test calls.
    """
    say(client, "note that the fence posts are rotten on the left side")

    intents = []
    for _ in range(5):
        said = say(client, "what did I say about the fence")
        row = row_for(said["utterance_id"])
        intents.append(row["intent"])
        if row["intent"] == "answer":
            assert row["model_calls"] == 1, "answered, but still paid for two calls"
            return

    raise AssertionError(f"never answered from CONTEXT in five tries: {intents}")


def test_query_still_reachable(client):
    """THE SAFETY PROPERTY. Do not delete this test.

    CONTEXT is question-derived, which TODAY deliberately is not. What makes
    that safe is that a miss falls through to `query` rather than producing a
    confident wrong answer out of a block that never held the answer."""
    said = say(client, "what did I say about the mortgage refinancing paperwork")
    assert row_for(said["utterance_id"])["intent"] == "query"
