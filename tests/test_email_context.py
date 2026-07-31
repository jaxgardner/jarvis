"""Email as assistant context.

This is the half of Gmail ingestion that docs/phase-6-ingestion.md §4 did not
specify. §4 routes mail exclusively into `proposals`, which is right for
writes — nothing a model extracted should reach `events` unattended — but it
leaves the assistant unable to answer "did the landlord email me back?".

These tests pin the two properties that make that safe: mail is searchable, and
mail is never mistaken for something the user said.
"""

import sqlite3

import pytest

from app import handlers
from ingest import gmail
from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"
TZ = "America/Denver"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "context.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


@pytest.fixture
def conn(db):
    from app.db import connect

    c = connect()
    yield c
    c.close()


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {SHARED}"
    return c


def store_mail(
    subject: str = "Lease renewal",
    sender: str = "Dana <dana@example.com>",
    snippet: str = "Attaching the renewal for your signature.",
    message_id: str = "m1",
    labels: list[str] | None = None,
) -> None:
    from app.db import transaction

    with transaction() as c:
        gmail.store(
            c,
            gmail.to_row(
                {
                    "id": message_id,
                    "threadId": f"t-{message_id}",
                    "labelIds": labels if labels is not None else ["INBOX"],
                    "snippet": snippet,
                    "internalDate": "1785000000000",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": sender},
                            {"name": "Subject", "value": subject},
                        ]
                    },
                }
            ),
        )


def add_note(body: str) -> None:
    from app import mutations
    from app.db import transaction

    with transaction() as c:
        mutations.insert(c, None, "notes", {"body": body})


# ── search ────────────────────────────────────────────────


def test_email_is_searchable_by_subject(conn):
    store_mail(subject="Lease renewal")
    assert len(handlers.search_email(conn, "lease")) == 1


def test_email_is_searchable_by_sender(conn):
    store_mail(sender="Dana <dana@example.com>")
    assert len(handlers.search_email(conn, "Dana")) == 1


def test_email_is_searchable_by_snippet(conn):
    store_mail(snippet="The plumber is coming Thursday at nine")
    assert len(handlers.search_email(conn, "plumber")) == 1


def test_search_ignores_short_noise_words(conn):
    store_mail()
    assert handlers.search_email(conn, "in it a") == []


def test_search_survives_fts_hostile_input(conn):
    """Questions arrive as dictated speech, so they carry quotes, brackets and
    stray operators — all of which are FTS5 syntax. Terms are extracted before
    the index sees them, so the punctuation never reaches the parser and the
    question is still answered rather than becoming a 500."""
    store_mail(subject="Lease renewal")
    assert len(handlers.search_email(conn, '"lease" OR (renewal')) == 1
    assert len(handlers.search_email(conn, "lease renewal")) == 1


def test_search_returns_nothing_before_migration_006(tmp_path, monkeypatch):
    """Every caller has to keep working on a database that predates the email
    table — /health, /say and the MCP server all reach this code."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    try:
        assert handlers.search_email(conn, "anything") == []
    finally:
        conn.close()


# ── not mistaken for a note ───────────────────────────────


def test_a_note_alone_still_answers_from_the_template(conn):
    """No mail involved, so the fast templated path stands — no model hop."""
    add_note("Sarah prefers mornings")
    answer = handlers._answer_recall(conn, {"subject": "Sarah"}, TZ)
    assert answer is not None and "Sarah prefers mornings" in answer


def test_matching_mail_defers_the_answer_to_the_model(conn):
    """"Did Sarah email me?" asked by someone who ALSO has a note mentioning
    Sarah must not come back as "You noted: …". When both sources match, hand
    both to the model rather than templating the wrong one."""
    add_note("Sarah prefers mornings")
    store_mail(sender="Sarah <sarah@example.com>", subject="Re: the quote")

    assert handlers._answer_recall(conn, {"subject": "Sarah"}, TZ) is None


def test_no_note_and_no_mail_defers_too(conn):
    assert handlers._answer_recall(conn, {"subject": "nobody"}, TZ) is None


# ── /inbox ────────────────────────────────────────────────


def test_inbox_lists_recent_mail(client):
    store_mail(subject="Lease renewal")
    body = client.get("/inbox").json()
    assert [m["subject"] for m in body["messages"]] == ["Lease renewal"]


def test_inbox_never_exposes_a_body_column(client):
    """format=metadata means there is no body to expose, and the response
    shape should make that obvious to anyone reading it."""
    store_mail()
    message = client.get("/inbox").json()["messages"][0]
    assert set(message) == {
        "sender",
        "subject",
        "snippet",
        "received_at",
        "is_unread",
        "thread_id",
    }


def test_inbox_can_filter_to_unread(client):
    store_mail(message_id="read", subject="Read one", labels=["INBOX"])
    store_mail(message_id="new", subject="Unread one", labels=["INBOX", "UNREAD"])
    body = client.get("/inbox?unread_only=true").json()
    assert [m["subject"] for m in body["messages"]] == ["Unread one"]


def test_inbox_can_search(client):
    store_mail(message_id="a", subject="Lease renewal")
    store_mail(message_id="b", subject="Dinner Friday")
    body = client.get("/inbox?q=lease").json()
    assert [m["subject"] for m in body["messages"]] == ["Lease renewal"]


def test_inbox_requires_a_token(db):
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).get("/inbox").status_code == 401
