import sqlite3

import pytest

from app import handlers
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ctx.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _message(conn, body, handle="+15551234", sent_at="2026-08-04T12:00:00+00:00"):
    conn.execute(
        "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
        " VALUES (?,?,?,?,?)",
        (body[:8] + sent_at, handle, "in", body, sent_at),
    )
    conn.commit()


def test_search_messages_finds_one(conn):
    _message(conn, "the landlord wrote back about the fence")
    hits = handlers.search_messages(conn, "what did the landlord say about the fence")
    assert hits
    assert "landlord" in hits[0]["body"]


def test_context_block_includes_texts(conn):
    _message(conn, "the landlord wrote back about the fence")
    block = handlers.context_block(conn, "did the landlord write back")
    assert "TEXT:" in block


def test_missed_calls_appear_in_today(conn):
    conn.execute(
        "INSERT INTO calls (external_id, handle, direction, answered, occurred_at)"
        " VALUES ('c1','+15551234','in',0,?)",
        (handlers.timeutil.to_utc_iso(handlers.timeutil.now("America/Denver")),),
    )
    conn.commit()
    block = handlers.today_block(conn, "America/Denver")
    assert "missed call" in block.lower()


def test_answered_call_is_not_a_missed_call(conn):
    conn.execute(
        "INSERT INTO calls (external_id, handle, direction, answered, occurred_at)"
        " VALUES ('c2','+15551234','in',1,?)",
        (handlers.timeutil.to_utc_iso(handlers.timeutil.now("America/Denver")),),
    )
    conn.commit()
    assert "missed call" not in handlers.today_block(conn, "America/Denver").lower()


@pytest.fixture
def seen(monkeypatch):
    """Capture the context `query` hands the answering model, without making
    the call. What is in that block is the whole question here."""
    captured = {}

    def fake_answer(question, context, tz_name):
        captured["context"] = context
        return "spoken"

    monkeypatch.setattr(handlers.router, "answer", fake_answer)
    return captured


def test_a_message_question_puts_the_text_in_context(conn, seen):
    _message(conn, "running late, be there at six")
    handlers.query(
        conn, 1, {"question": "did she say she was running late", "kind": "message"}, "UTC"
    )
    assert "TEXT: from +15551234" in seen["context"]
    assert "running late" in seen["context"]


def test_a_call_question_lists_recent_calls(conn, seen):
    """Calls are not searchable — a handle is a phone number and none of the
    question's words are in the row — so the kind lists them instead."""
    conn.execute(
        "INSERT INTO calls (external_id, handle, direction, answered, occurred_at)"
        " VALUES ('c9','+15559876','in',0,?)",
        (handlers.timeutil.to_utc_iso(handlers.timeutil.now("UTC")),),
    )
    conn.commit()
    handlers.query(conn, 1, {"question": "did I miss a call", "kind": "call"}, "UTC")
    assert "MISSED CALL: from +15559876" in seen["context"]


def test_a_call_older_than_the_window_is_not_listed(conn, seen):
    conn.execute(
        "INSERT INTO calls (external_id, handle, direction, answered, occurred_at)"
        " VALUES ('c10','+15559876','in',0,?)",
        (
            handlers.timeutil.to_utc_iso(
                handlers.timeutil.now("UTC") - handlers.timedelta(days=30)
            ),
        ),
    )
    conn.commit()
    handlers.query(conn, 1, {"question": "did I miss a call", "kind": "call"}, "UTC")
    assert "+15559876" not in seen["context"]


def test_yesterdays_missed_call_is_not_todays(conn):
    """The line is about *today*. A call missed last week is not a fact about
    this morning, and one that never ages out is a line that stops being read."""
    stale = handlers.timeutil.to_utc_iso(
        handlers.timeutil.now("America/Denver") - handlers.timedelta(days=2)
    )
    conn.execute(
        "INSERT INTO calls (external_id, handle, direction, answered, occurred_at)"
        " VALUES ('c3','+15551234','in',0,?)",
        (stale,),
    )
    conn.commit()
    assert "missed call" not in handlers.today_block(conn, "America/Denver").lower()
