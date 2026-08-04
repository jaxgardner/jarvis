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


def log(conn, items, tz_name="America/Denver", utterance_id=None):
    from gratitude import entries

    return entries.add(conn, utterance_id, items, tz_name)


def test_one_turn_can_carry_three_things(db):
    from app.db import transaction

    with transaction() as conn:
        added, total = log(conn, ["the sun", "Emma calling", "the deadline moving"])

    assert (added, total) == (3, 3)
    assert [r["body"] for r in rows(db, "SELECT body FROM gratitude_entries ORDER BY id")] == [
        "the sun",
        "Emma calling",
        "the deadline moving",
    ]


def test_items_accumulate_across_turns(db):
    from app.db import transaction

    with transaction() as conn:
        assert log(conn, ["the sun"]) == (1, 1)
    with transaction() as conn:
        assert log(conn, ["Emma calling", "the rain"]) == (2, 3)


def test_blank_and_whitespace_items_are_dropped(db):
    from app.db import transaction

    with transaction() as conn:
        added, total = log(conn, ["  the sun  ", "", "   ", "the\n rain"])

    assert (added, total) == (2, 2)
    bodies = [r["body"] for r in rows(db, "SELECT body FROM gratitude_entries ORDER BY id")]
    assert bodies == ["the sun", "the rain"]


def test_nothing_at_all_is_a_router_error(db):
    """Raised as ValueError so /say answers 422 — malformed tool arguments are
    a router problem, not a user problem."""
    from app.db import transaction

    with transaction() as conn, pytest.raises(ValueError):
        log(conn, ["", "  "])


def test_created_at_is_stored_as_utc(db):
    from app.db import transaction

    with transaction() as conn:
        log(conn, ["the sun"])

    stored = rows(db, "SELECT created_at FROM gratitude_entries")[0]["created_at"]
    assert stored.endswith("Z")


def test_for_day_returns_entries_in_the_order_they_were_said(db):
    from app import timeutil
    from app.db import connect, transaction
    from gratitude import entries

    with transaction() as conn:
        log(conn, ["first", "second"])
    with transaction() as conn:
        log(conn, ["third"])

    conn = connect()
    try:
        got = entries.for_day(conn, entries.day_for(timeutil.now("America/Denver")))
    finally:
        conn.close()

    assert [e["body"] for e in got] == ["first", "second", "third"]
    assert set(got[0]) == {"id", "body", "at"}


def seed(db, on, bodies):
    """Write entries directly for a past day. Bypasses the mutations helper on
    purpose — these are fixtures, not user actions."""
    from app.db import transaction

    with transaction() as conn:
        for index, body in enumerate(bodies):
            conn.execute(
                """INSERT INTO gratitude_entries (body, entry_on, created_at)
                     VALUES (?,?,?)""",
                (body, on, f"{on}T{20 + index:02d}:00:00Z"),
            )


def test_recent_groups_by_day_newest_first_and_excludes_today(db, monkeypatch):
    from app.db import connect
    from gratitude import entries

    monkeypatch.setattr(entries, "day_for", lambda local, day_start=None: "2026-08-04")
    seed(db, "2026-08-02", ["a", "b"])
    seed(db, "2026-08-03", ["c"])
    seed(db, "2026-08-04", ["today's"])

    conn = connect()
    try:
        got = entries.recent(conn, "America/Denver", 30)
    finally:
        conn.close()

    assert [day["on"] for day in got] == ["2026-08-03", "2026-08-02"]
    assert [e["body"] for e in got[1]["entries"]] == ["a", "b"]


def test_recent_honours_its_window(db, monkeypatch):
    from app.db import connect
    from gratitude import entries

    monkeypatch.setattr(entries, "day_for", lambda local, day_start=None: "2026-08-04")
    seed(db, "2026-08-03", ["inside"])
    seed(db, "2026-07-01", ["outside"])

    conn = connect()
    try:
        got = entries.recent(conn, "America/Denver", 7)
    finally:
        conn.close()

    assert [day["on"] for day in got] == ["2026-08-03"]


def test_streak_counts_consecutive_complete_days(db, monkeypatch):
    from app.db import connect
    from gratitude import entries

    monkeypatch.setattr(entries, "day_for", lambda local, day_start=None: "2026-08-04")
    for on in ("2026-08-02", "2026-08-03", "2026-08-04"):
        seed(db, on, ["a", "b", "c"])

    conn = connect()
    try:
        assert entries.streak(conn, "America/Denver") == 3
    finally:
        conn.close()


def test_an_incomplete_today_does_not_break_the_streak(db, monkeypatch):
    """Today at one is a day in progress, not a failure. A number that turns
    into a reproach at 6pm is a number that gets muted."""
    from app.db import connect
    from gratitude import entries

    monkeypatch.setattr(entries, "day_for", lambda local, day_start=None: "2026-08-04")
    seed(db, "2026-08-02", ["a", "b", "c"])
    seed(db, "2026-08-03", ["a", "b", "c"])
    seed(db, "2026-08-04", ["just the one"])

    conn = connect()
    try:
        assert entries.streak(conn, "America/Denver") == 2
    finally:
        conn.close()


def test_an_empty_today_does_not_break_the_streak_either(db, monkeypatch):
    from app.db import connect
    from gratitude import entries

    monkeypatch.setattr(entries, "day_for", lambda local, day_start=None: "2026-08-04")
    seed(db, "2026-08-03", ["a", "b", "c"])

    conn = connect()
    try:
        assert entries.streak(conn, "America/Denver") == 1
    finally:
        conn.close()


def test_a_missed_day_ends_the_streak(db, monkeypatch):
    from app.db import connect
    from gratitude import entries

    monkeypatch.setattr(entries, "day_for", lambda local, day_start=None: "2026-08-04")
    seed(db, "2026-08-01", ["a", "b", "c"])
    seed(db, "2026-08-03", ["a", "b", "c"])
    seed(db, "2026-08-04", ["a", "b", "c"])

    conn = connect()
    try:
        assert entries.streak(conn, "America/Denver") == 2
    finally:
        conn.close()


def test_a_day_short_of_three_does_not_count(db, monkeypatch):
    from app.db import connect
    from gratitude import entries

    monkeypatch.setattr(entries, "day_for", lambda local, day_start=None: "2026-08-04")
    seed(db, "2026-08-03", ["a", "b"])
    seed(db, "2026-08-04", ["a", "b", "c"])

    conn = connect()
    try:
        assert entries.streak(conn, "America/Denver") == 1
    finally:
        conn.close()


def test_no_history_is_a_zero_streak(db, monkeypatch):
    from app.db import connect
    from gratitude import entries

    monkeypatch.setattr(entries, "day_for", lambda local, day_start=None: "2026-08-04")

    conn = connect()
    try:
        assert entries.streak(conn, "America/Denver") == 0
    finally:
        conn.close()


def reply_for(db, items, utterance_id=None):
    from app import handlers
    from app.db import transaction

    with transaction() as conn:
        return handlers.log_gratitude(
            conn, utterance_id, {"items": items}, "America/Denver"
        )


def test_the_first_thing_asks_for_two_more(db):
    assert reply_for(db, ["the sun"]) == "That's one down. Two more when you're ready."


def test_the_second_thing_asks_for_one_more(db):
    reply_for(db, ["the sun"])
    assert reply_for(db, ["Emma calling"]) == "That's two down. One more when you're ready."


def test_the_third_thing_finishes_the_day(db):
    assert reply_for(db, ["a", "b", "c"]) == "Three for today. Done."


def test_a_fourth_thing_is_accepted(db):
    """Refusing gratitude because a counter is full would be the app being
    pedantic about its own bookkeeping."""
    reply_for(db, ["a", "b", "c"])
    assert reply_for(db, ["the car started"]) == "That's a fourth — logged."
    assert len(rows(db, "SELECT id FROM gratitude_entries")) == 4


def test_a_seventh_thing_stops_counting_out_loud(db):
    reply_for(db, ["a", "b", "c"])
    for _ in range(3):
        reply_for(db, ["another"])
    assert reply_for(db, ["and another"]) == "That's another one — logged."


def test_the_reply_is_safe_to_speak(db):
    reply = reply_for(db, ["the sun"])
    assert "\n" not in reply
    assert not any(ch in reply for ch in "*_#`[]")


def test_the_handler_is_registered_for_the_router(db):
    from app import handlers

    assert handlers.FAST_HANDLERS["log_gratitude"] is handlers.log_gratitude


def test_undo_reverses_a_whole_gratitude_turn(db):
    """Two items in one turn write two mutation rows sharing one utterance_id.
    Reversing one of them would leave half a sentence standing."""
    from app import mutations
    from app.db import transaction

    with transaction() as conn:
        utterance_id = int(
            conn.execute(
                "INSERT INTO utterances (raw_text, client) VALUES ('grateful for x and y','test')"
            ).lastrowid
        )

    reply_for(db, ["the sun", "Emma calling"], utterance_id=utterance_id)
    assert len(rows(db, "SELECT id FROM gratitude_entries")) == 2

    with transaction() as conn:
        mutations.undo_last(conn)

    assert rows(db, "SELECT id FROM gratitude_entries") == []
