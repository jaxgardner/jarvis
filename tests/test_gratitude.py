"""Logging three things a day, and the streak behind them."""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers import apply_migrations

DENVER = ZoneInfo("America/Denver")


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


def test_late_evening_belongs_to_today():
    from gratitude import entries

    assert entries.day_for(datetime(2026, 8, 4, 23, 50, tzinfo=DENVER)) == "2026-08-04"


def test_after_midnight_belongs_to_the_day_before():
    """The prompt lands at 10pm and is sometimes answered at half past twelve.
    That entry belongs to the day you were thinking about, not to the one that
    started twenty minutes ago."""
    from gratitude import entries

    assert entries.day_for(datetime(2026, 8, 5, 0, 30, tzinfo=DENVER)) == "2026-08-04"


def test_morning_belongs_to_itself():
    from gratitude import entries

    assert entries.day_for(datetime(2026, 8, 5, 7, 0, tzinfo=DENVER)) == "2026-08-05"


def test_four_am_is_the_boundary():
    from gratitude import entries

    assert entries.day_for(datetime(2026, 8, 5, 3, 59, tzinfo=DENVER)) == "2026-08-04"
    assert entries.day_for(datetime(2026, 8, 5, 4, 0, tzinfo=DENVER)) == "2026-08-05"


def test_both_halves_of_a_fall_back_night_land_on_the_same_day():
    """01:30 happens twice on 2026-11-01 in Denver. Both are before the
    cutoff, so both belong to Halloween — the ambiguous hour must not split
    one evening across two days."""
    from gratitude import entries

    first = datetime(2026, 11, 1, 1, 30, tzinfo=DENVER, fold=0)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=DENVER, fold=1)
    assert entries.day_for(first) == "2026-10-31"
    assert entries.day_for(second) == "2026-10-31"
