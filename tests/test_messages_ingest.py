"""Texts and missed calls, imported read-only from the Mac's own databases."""

import base64
import sqlite3

import pytest

from ingest import messages
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A migrated database, opened directly *and* pointed at by app.db.

    Both halves are needed: most tests here drive SQL through `c`, but
    `sync()` opens its own connection through `app.db.transaction()`, and
    without the redirect it would find the unmigrated global test database.
    """
    path = tmp_path / "msg.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)

    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_tables_exist(conn):
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"messages", "calls"} <= names


def test_external_id_dedupes(conn):
    conn.execute(
        "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
        " VALUES ('m1','+15551234','in','hello','2026-08-04T10:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
            " VALUES ('m1','+15551234','in','hello again','2026-08-04T10:01:00Z')"
        )


def test_fts_finds_a_message(conn):
    conn.execute(
        "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
        " VALUES ('m2','+15551234','in','the landlord replied about the fence',"
        "'2026-08-04T10:00:00Z')"
    )
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'fence'"
    ).fetchall()
    assert len(hits) == 1


def test_hard_delete_leaves_the_index_clean(conn):
    """Unlike notes, messages are hard-deleted when they age out, so the FTS
    index needs no join-and-filter to stay honest."""
    conn.execute(
        "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
        " VALUES ('m3','+1','in','disposable','2026-08-04T10:00:00Z')"
    )
    conn.commit()
    conn.execute("DELETE FROM messages WHERE external_id = 'm3'")
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'disposable'"
    ).fetchall()
    assert hits == []


def test_apple_nanosecond_epoch_converts():
    """chat.db stores nanoseconds since 2001-01-01, not Unix seconds. Getting
    this wrong is off by 31 years and plausible enough to survive review."""
    # 2026-08-04T12:00:00Z == 807537600 seconds after the Apple epoch
    row = messages.to_row(
        {
            "external_id": 1,
            "handle": "+15551234",
            "is_from_me": 0,
            "text": "hello",
            "service": "iMessage",
            "apple_date": 807_537_600 * 1_000_000_000,
        }
    )
    assert row["sent_at"].startswith("2026-08-04T12:00:00")


def test_attributed_body_is_used_when_text_is_null():
    """The case that matters: modern rows leave `text` NULL."""
    blob = (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84"
        b"\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92"
        b"\x84\x84\x84\x08NSString\x01\x94\x84\x01\x2b\x05hello\x86"
    )
    row = messages.to_row(
        {
            "external_id": 2,
            "handle": "+15551234",
            "is_from_me": 0,
            "text": None,
            "attributed_body": base64.b64encode(blob).decode(),
            "apple_date": 807_537_600 * 1_000_000_000,
        }
    )
    assert row["body"] == "hello"


def test_row_with_no_recoverable_text_is_skipped():
    """An attachment-only message has neither. Skipped, not stored empty —
    an empty body is a row that pollutes search and answers nothing."""
    assert (
        messages.to_row(
            {
                "external_id": 3,
                "handle": "+1",
                "is_from_me": 0,
                "text": None,
                "attributed_body": None,
                "apple_date": 807_537_600 * 1_000_000_000,
            }
        )
        is None
    )


def test_store_is_idempotent(conn):
    row = {
        "external_id": "9",
        "handle": "+1",
        "direction": "in",
        "body": "twice",
        "service": "SMS",
        "sent_at": "2026-08-04T12:00:00+00:00",
    }
    messages.store(conn, row)
    messages.store(conn, row)
    conn.commit()
    count = conn.execute(
        "SELECT count(*) AS n FROM messages WHERE external_id = '9'"
    ).fetchone()["n"]
    assert count == 1


def test_missing_helper_is_not_fatal(conn, monkeypatch):
    """No binary, or no grant, marks the source stale and leaves every other
    ingester running. An importer that raises here takes the 15-minute tick
    down with it."""
    monkeypatch.setattr(messages, "HELPER", "/nonexistent/tccread")
    result = messages.sync()
    assert result["ok"] is False
    assert "tccread" in result["detail"]
