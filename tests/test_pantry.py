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


def stock(db, name, *, category=None, location="fridge", expires_on=None, status="active"):
    """Put one item in the fridge, bypassing the review flow."""
    from app.db import transaction

    with transaction() as conn:
        return int(
            conn.execute(
                """INSERT INTO pantry_items
                     (name, category, location, expires_on, status)
                   VALUES (?,?,?,?,?)""",
                (name, category, location, expires_on, status),
            ).lastrowid
        )


def test_active_lists_soonest_to_expire_first(db):
    from app.db import connect
    from pantry import inventory

    stock(db, "pasta", expires_on=None, location="pantry")
    stock(db, "milk", expires_on="2026-08-07")
    stock(db, "spinach", expires_on="2026-08-02")

    conn = connect()
    try:
        names = [item["name"] for item in inventory.active(conn)]
    finally:
        conn.close()
    assert names == ["spinach", "milk", "pasta"]


def test_active_reports_days_left(db, monkeypatch):
    from app.db import connect
    from pantry import inventory

    monkeypatch.setattr(inventory, "_today", lambda: "2026-07-31")
    stock(db, "spinach", expires_on="2026-08-02")

    conn = connect()
    try:
        item = inventory.active(conn)[0]
    finally:
        conn.close()
    assert item["days_left"] == 2


def test_active_excludes_pending_and_consumed(db):
    from app.db import connect
    from pantry import inventory

    stock(db, "unreviewed", status="pending")
    stock(db, "eaten", status="consumed")
    stock(db, "real")

    conn = connect()
    try:
        assert [i["name"] for i in inventory.active(conn)] == ["real"]
    finally:
        conn.close()


def test_find_matches_a_substring_of_the_name(db):
    from app.db import connect
    from pantry import inventory

    stock(db, "whole milk")

    conn = connect()
    try:
        assert inventory.find(conn, "milk")["name"] == "whole milk"
        assert inventory.find(conn, "MILK")["name"] == "whole milk"
        assert inventory.find(conn, "orange juice") is None
    finally:
        conn.close()


def test_find_prefers_the_item_dying_soonest(db):
    """Two cartons open: 'we're out of milk' means the one you were drinking."""
    from app.db import connect
    from pantry import inventory

    stock(db, "whole milk", expires_on="2026-08-20")
    stock(db, "whole milk", expires_on="2026-08-02")

    conn = connect()
    try:
        assert inventory.find(conn, "milk")["expires_on"] == "2026-08-02"
    finally:
        conn.close()


def test_consume_marks_the_item_and_adds_it_to_the_list(db):
    from app.db import connect, transaction
    from pantry import inventory

    item_id = stock(db, "whole milk")

    with transaction() as conn:
        utterance_id = int(
            conn.execute(
                "INSERT INTO utterances (raw_text, client) VALUES ('out of milk','test')"
            ).lastrowid
        )
        item = inventory.find(conn, "milk")
        inventory.consume(conn, utterance_id, item, partial=False)

    assert rows(db, "SELECT status FROM pantry_items")[0]["status"] == "consumed"
    listed = rows(db, "SELECT name, reason FROM shopping_list")
    assert listed == [{"name": "whole milk", "reason": "out"}]


def test_a_partial_consume_keeps_the_item_and_adds_nothing(db):
    """'used half the chicken' is not 'buy more chicken'."""
    from app.db import transaction
    from pantry import inventory

    stock(db, "chicken breast", location="fridge")

    with transaction() as conn:
        item = inventory.find(conn, "chicken")
        inventory.consume(conn, None, item, partial=True)

    assert rows(db, "SELECT status FROM pantry_items")[0]["status"] == "active"
    assert rows(db, "SELECT * FROM shopping_list") == []


def test_undo_after_consume_restores_both_halves(db):
    """The reason Task 2 exists. Undoing half of this is worse than not
    undoing at all."""
    from app import mutations
    from app.db import transaction
    from pantry import inventory

    stock(db, "whole milk")

    with transaction() as conn:
        utterance_id = int(
            conn.execute(
                "INSERT INTO utterances (raw_text, client) VALUES ('out of milk','test')"
            ).lastrowid
        )
        inventory.consume(conn, utterance_id, inventory.find(conn, "milk"), partial=False)

    with transaction() as conn:
        mutations.undo_last(conn)

    assert rows(db, "SELECT status FROM pantry_items")[0]["status"] == "active"
    assert rows(db, "SELECT * FROM shopping_list") == []


def test_adding_something_already_on_the_list_is_a_no_op(db):
    from app.db import transaction
    from pantry import inventory

    with transaction() as conn:
        first = inventory.add_to_list(conn, None, "paper towels", "manual")
        second = inventory.add_to_list(conn, None, "Paper Towels", "manual")

    assert first is not None
    assert second is None, "the partial unique index is the contract; honour it"
    assert len(rows(db, "SELECT * FROM shopping_list")) == 1


def test_a_purchased_entry_can_be_added_again(db):
    from app.db import transaction
    from pantry import inventory

    with transaction() as conn:
        entry_id = inventory.add_to_list(conn, None, "milk", "out")
        inventory.resolve_list_entry(conn, None, entry_id, "purchased")
        again = inventory.add_to_list(conn, None, "milk", "out")

    assert again is not None
    assert len(rows(db, "SELECT * FROM shopping_list WHERE status='open'")) == 1
