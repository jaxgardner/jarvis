"""Texts and missed calls, imported read-only from the Mac's own databases."""

import sqlite3

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "msg.db"
    apply_migrations(path)
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
