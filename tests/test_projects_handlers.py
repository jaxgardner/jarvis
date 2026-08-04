"""What happens once the router has decided. No model calls."""

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "handlers.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    connection = appdb.connect()
    try:
        yield connection
    finally:
        connection.close()


def utterance(conn) -> int:
    return int(
        conn.execute(
            "INSERT INTO utterances (raw_text) VALUES ('spoken') RETURNING id"
        ).fetchone()["id"]
    )


# ── start_project ─────────────────────────────────────────


def test_starting_a_project_with_no_research_just_creates_it(conn):
    from app import handlers

    project_id, job_id, reply = handlers.start_project(
        conn, utterance(conn), {"name": "hydroponic lettuce"}
    )

    assert job_id is None
    assert reply == "Started the hydroponic lettuce project."
    assert conn.execute(
        "SELECT name FROM projects WHERE id = ?", (project_id,)
    ).fetchone()["name"] == "hydroponic lettuce"


def test_a_research_ask_becomes_a_job_under_the_project(conn):
    from app import handlers

    utterance_id = utterance(conn)
    project_id, job_id, reply = handlers.start_project(
        conn,
        utterance_id,
        {"name": "hydroponic lettuce", "research_task": "what does it take to start"},
    )

    assert job_id is not None
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert job["project_id"] == project_id
    assert job["prompt"] == "what does it take to start"
    assert job["utterance_id"] == utterance_id
    assert "ping you" in reply


def test_starting_a_project_that_already_exists_does_not_make_a_second(conn):
    from app import handlers

    handlers.start_project(conn, utterance(conn), {"name": "lettuce"})
    project_id, job_id, reply = handlers.start_project(
        conn, utterance(conn), {"name": "Lettuce"}
    )

    assert conn.execute("SELECT count(*) AS n FROM projects").fetchone()["n"] == 1
    assert job_id is None
    assert "already" in reply.lower()


def test_saying_it_again_with_research_still_starts_the_research(conn):
    from app import handlers

    handlers.start_project(conn, utterance(conn), {"name": "lettuce"})
    _, job_id, reply = handlers.start_project(
        conn, utterance(conn), {"name": "lettuce", "research_task": "pumps"}
    )

    assert job_id is not None
    assert "ping you" in reply


def test_a_project_with_no_name_is_a_bug_not_a_row(conn):
    from app import handlers

    with pytest.raises(ValueError):
        handlers.start_project(conn, utterance(conn), {"name": "   "})


# ── filing under one ──────────────────────────────────────


def test_a_note_files_under_the_named_project_and_says_so(conn):
    from app import handlers
    from projects import store

    project_id = store.create(conn, None, "lettuce")
    reply = handlers.add_note(
        conn,
        utterance(conn),
        {"body": "deep water culture", "project_id": project_id},
        "America/Denver",
    )

    assert reply == "Noted, under lettuce."
    assert conn.execute("SELECT project_id FROM notes").fetchone()["project_id"] == project_id


def test_an_invented_project_id_files_the_note_nowhere(conn):
    """The router is told to use ids only from PROJECTS. This is what happens
    when it does it anyway — the note is kept, attached to nothing."""
    from app import handlers

    reply = handlers.add_note(
        conn, utterance(conn), {"body": "deep water culture", "project_id": 99}, "America/Denver"
    )

    assert reply == "Noted."
    assert conn.execute("SELECT project_id FROM notes").fetchone()["project_id"] is None


def test_events_and_reminders_file_under_a_project_too(conn):
    from app import handlers
    from projects import store

    project_id = store.create(conn, None, "lettuce")
    handlers.add_event(
        conn,
        utterance(conn),
        {"title": "permit office", "starts_at": "2026-08-10T15:00:00-06:00", "project_id": project_id},
        "America/Denver",
    )
    handlers.add_reminder(
        conn,
        utterance(conn),
        {"body": "order the pump", "fire_at": "2026-08-09T10:00:00-06:00", "project_id": project_id},
        "America/Denver",
    )

    assert conn.execute("SELECT project_id FROM events").fetchone()["project_id"] == project_id
    assert conn.execute("SELECT project_id FROM reminders").fetchone()["project_id"] == project_id


def test_a_note_with_no_project_is_unchanged(conn):
    from app import handlers

    reply = handlers.add_note(
        conn, utterance(conn), {"body": "the wifi password is hunter2"}, "America/Denver"
    )
    assert reply == "Noted."


# ── asking where you are ──────────────────────────────────


def test_a_project_question_puts_the_project_in_front_of_the_model(conn, monkeypatch):
    """The answer is generated, but what it is generated FROM is not — and
    that is the part worth testing."""
    from app import handlers, router
    from projects import store

    project_id = store.create(conn, None, "lettuce", "salad in the garage")
    conn.execute(
        "INSERT INTO notes (body, project_id) VALUES ('deep water culture', ?)",
        (project_id,),
    )

    seen = {}

    def fake_answer(question, context, tz_name):
        seen["context"] = context
        return "You are looking at deep water culture."

    monkeypatch.setattr(router, "answer", fake_answer)

    reply = handlers.query(
        conn,
        None,
        {"question": "where am I on the lettuce project", "kind": "project", "project_id": project_id},
        "America/Denver",
    )

    assert reply == "You are looking at deep water culture."
    assert "PROJECT: lettuce — salad in the garage" in seen["context"]
    assert "NOTE: deep water culture" in seen["context"]


def capture(monkeypatch, seen: dict) -> None:
    """Stand in for the answering hop and keep what it was given."""
    from app import router

    def fake_answer(question, context, tz_name):
        seen["context"] = context
        return "Answered."

    monkeypatch.setattr(router, "answer", fake_answer)


def test_asking_what_you_are_working_on_lists_the_active_projects(conn, monkeypatch):
    from app import handlers
    from projects import store

    store.create(conn, None, "lettuce")
    store.create(conn, None, "kitchen remodel")

    seen: dict = {}
    capture(monkeypatch, seen)

    handlers.query(
        conn, None, {"question": "what am I working on", "kind": "project"}, "America/Denver"
    )

    assert "PROJECT: lettuce" in seen["context"]
    assert "PROJECT: kitchen remodel" in seen["context"]


def test_a_project_question_is_not_drowned_out_by_unrelated_notes(conn, monkeypatch):
    """Found on the real database: asked "what am I working on" with a project
    open, it answered about an unrelated note on the GitHub CLI. The generic
    note search matches the question's own words — "working" hits any note
    containing it — and those lines outcompeted the single PROJECT line."""
    from app import handlers
    from projects import store

    store.create(conn, None, "back garden fence")
    conn.execute(
        "INSERT INTO notes (body) VALUES ('still working on fixing the GitHub CLI')"
    )

    seen: dict = {}
    capture(monkeypatch, seen)

    handlers.query(
        conn, None, {"question": "what am I working on", "kind": "project"}, "America/Denver"
    )

    assert "PROJECT: back garden fence" in seen["context"]
    assert "GitHub CLI" not in seen["context"], seen["context"]


def test_no_projects_at_all_says_so_rather_than_guessing(conn, monkeypatch):
    from app import handlers

    conn.execute("INSERT INTO notes (body) VALUES ('still working on the GitHub CLI')")
    seen: dict = {}
    capture(monkeypatch, seen)

    reply = handlers.query(
        conn, None, {"question": "what am I working on", "kind": "project"}, "America/Denver"
    )

    assert reply == "You don't have any projects yet."
    assert "context" not in seen, "should not have spent a model call"


def test_an_invented_project_id_does_not_invent_context(conn, monkeypatch):
    from app import handlers
    from projects import store

    store.create(conn, None, "lettuce")
    seen: dict = {}
    capture(monkeypatch, seen)

    handlers.query(
        conn,
        None,
        {"question": "where am I on the greenhouse", "kind": "project", "project_id": 99},
        "America/Denver",
    )

    # Falls back to the active list rather than answering about nothing.
    assert "PROJECT: lettuce" in seen["context"]
