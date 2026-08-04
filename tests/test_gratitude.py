"""Logging three things a day, and the streak behind them."""

import sqlite3

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "gratitude.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


def rows(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_gratitude_entries_is_a_writable_domain_table(db):
    """The write has to go through the mutations helper like every other, or
    /undo cannot reach it — and voice input being lossy, /undo is the whole
    reason the log exists."""
    from app import mutations
    from app.db import transaction

    with transaction() as conn:
        row_id = mutations.insert(
            conn,
            None,
            "gratitude_entries",
            {"body": "the sun", "entry_on": "2026-08-04", "created_at": "2026-08-05T02:12:00Z"},
        )

    assert rows(db, "SELECT body, entry_on FROM gratitude_entries") == [
        {"body": "the sun", "entry_on": "2026-08-04"}
    ]
    logged = rows(db, "SELECT table_name, op, row_id FROM mutations")
    assert logged == [{"table_name": "gratitude_entries", "op": "insert", "row_id": row_id}]
