"""Project routing, run end-to-end against live Haiku.

Re-run after every change to the router prompt or tool definitions. Routing
decisions are the part of this system most likely to drift, and the failures
are quiet: a note that becomes a project, a project that becomes an escalation.

    uv run pytest tests/test_project_utterances.py -v

Costs a few cents per run. Skips automatically without an API key.
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


def intent_of(utterance_id: int) -> str:
    conn = connect()
    try:
        return conn.execute(
            "SELECT intent FROM utterances WHERE id = ?", (utterance_id,)
        ).fetchone()["intent"]
    finally:
        conn.close()


def test_a_project_from_first_word_to_last(client):
    """One conversation in order, each assertion naming the decision under
    test. This is the sequence the feature was designed around."""

    # 1. the sentence that started the whole feature
    r = say(client, "start a new project on hydroponic lettuce and research what it takes")
    assert intent_of(r["utterance_id"]) == "start_project"
    assert r["route"] == "deep", "a research ask must queue work, not just make a row"
    assert r["job_id"] is not None
    project_id = r["project_id"]

    conn = connect()
    try:
        job = conn.execute(
            "SELECT project_id FROM jobs WHERE id = ?", (r["job_id"],)
        ).fetchone()
        assert job["project_id"] == project_id, "the research is not filed under the project"
    finally:
        conn.close()

    # 2. train of thought, filed by naming the project
    r = say(client, "for the hydroponic lettuce project, I'm thinking about deep water culture")
    assert intent_of(r["utterance_id"]) == "add_note"
    conn = connect()
    try:
        note = conn.execute(
            "SELECT project_id FROM notes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert note["project_id"] == project_id
    finally:
        conn.close()

    # 3. a dated thing under the project is still an event
    r = say(client, "for the lettuce project, add a permit office visit next tuesday at 3")
    assert intent_of(r["utterance_id"]) == "add_event"

    # 4. asking where you are is a question, not new work
    r = say(client, "where am I on the hydroponic lettuce project")
    assert intent_of(r["utterance_id"]) == "query"
    assert "deep water" in r["reply"].lower() or "lettuce" in r["reply"].lower()

    # 5. starting it again is not a second project
    r = say(client, "start a project on hydroponic lettuce")
    assert intent_of(r["utterance_id"]) == "start_project"
    conn = connect()
    try:
        count = conn.execute(
            "SELECT count(*) AS n FROM projects WHERE lower(name) LIKE '%lettuce%'"
        ).fetchone()["n"]
        assert count == 1, "a repeated ask made a near-duplicate project"
    finally:
        conn.close()


def test_a_project_that_does_not_exist_is_not_invented(client):
    """The failure this whole naming design exists to prevent."""
    r = say(client, "note that the greenhouse thermostat is broken")
    assert intent_of(r["utterance_id"]) == "add_note"

    conn = connect()
    try:
        note = conn.execute(
            "SELECT project_id FROM notes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert note["project_id"] is None, "a note invented a project out of its content"
    finally:
        conn.close()


def test_plain_research_with_no_project_still_escalates(client):
    """start_project must not swallow every research ask."""
    r = say(client, "look into whether a heat pump would work in this house")
    assert intent_of(r["utterance_id"]) == "escalate"
