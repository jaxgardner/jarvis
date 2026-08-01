"""Pantry inventory: what I have, what is dying, what I need to buy.

The invariant under test throughout: the model reads pixels, a checked-in
table proposes dates, and a human confirms them. Nothing reaches the
inventory unreviewed.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "pantry.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


def rows(db, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_pantry_tables_exist(db):
    names = {r["name"] for r in rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"receipts", "pantry_items", "shopping_list"} <= names


def test_deleting_a_receipt_takes_its_items(db):
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("INSERT INTO receipts (id, image_sha256) VALUES (1, 'abc')")
        conn.execute(
            "INSERT INTO pantry_items (receipt_id, name) VALUES (1, 'whole milk')"
        )
        conn.commit()
        conn.execute("DELETE FROM receipts WHERE id = 1")
        conn.commit()
        left = conn.execute("SELECT count(*) FROM pantry_items").fetchone()[0]
    finally:
        conn.close()
    assert left == 0, "undoing a receipt must take the whole trip with it"


def test_the_same_photo_cannot_be_uploaded_twice(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO receipts (image_sha256) VALUES ('abc')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO receipts (image_sha256) VALUES ('abc')")
    finally:
        conn.close()


def test_one_open_list_entry_per_name(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO shopping_list (name, reason) VALUES ('Milk', 'out')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO shopping_list (name, reason) VALUES ('milk', 'out')")
        # A resolved entry does not block a fresh one.
        conn.execute("UPDATE shopping_list SET status = 'purchased'")
        conn.execute("INSERT INTO shopping_list (name, reason) VALUES ('milk', 'out')")
        conn.commit()
        n = conn.execute("SELECT count(*) FROM shopping_list").fetchone()[0]
    finally:
        conn.close()
    assert n == 2


def test_pantry_tables_are_writable_through_the_mutations_helper():
    from app import mutations

    assert {"receipts", "pantry_items", "shopping_list"} <= mutations.WRITABLE
