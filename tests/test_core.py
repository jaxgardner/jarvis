"""Offline tests — no API key, no network. These must always pass."""

import sqlite3
from datetime import datetime, timezone as _tz

UTC = _tz.utc
from zoneinfo import ZoneInfo

import pytest

from app import mutations, timeutil
from app.config import REPO_ROOT


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real file-backed database, for tests that need `app.db.transaction()`
    across more than one connection."""
    from tests.helpers import apply_migrations

    path = tmp_path / "core.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


@pytest.fixture
def conn():
    """In-memory DB with the real schema, so tests exercise the real triggers
    and constraints rather than a hand-written approximation."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    sql = (REPO_ROOT / "migrations" / "001_init.sql").read_text()
    c.executescript("\n".join(l for l in sql.splitlines() if not l.strip().upper().startswith("PRAGMA")))
    yield c
    c.close()


# ── time ──────────────────────────────────────────────────


def test_parses_z_suffix():
    assert timeutil.parse("2026-07-30T21:00:00Z").tzinfo is not None


def test_rejects_naive_timestamp():
    with pytest.raises(ValueError):
        timeutil.parse("2026-07-30T15:00:00")


def test_normalizes_offset_to_utc():
    assert timeutil.to_utc_iso("2026-07-30T15:00:00-06:00") == "2026-07-30T21:00:00Z"


def test_utc_normalization_makes_string_ordering_correct():
    """The reason for normalizing at all.

    Mixed offsets in one column are not hypothetical — ingestion imports
    calendar rows in UTC while voice capture writes local time. Compared as raw strings
    (which is what SQLite BETWEEN does), the earlier instant sorts later.
    """
    earlier = "2026-07-30T21:00:00+00:00"  # 21:00Z — from the calendar importer
    later = "2026-07-30T18:00:00-04:00"  # 22:00Z — captured while travelling
    assert earlier > later  # raw strings: wrong order
    assert timeutil.to_utc_iso(earlier) < timeutil.to_utc_iso(later)  # normalized: right


@pytest.mark.parametrize(
    "local_iso",
    [
        "2026-11-01T01:30:00-06:00",  # US fall-back: 1:30 AM happens twice
        "2027-03-14T03:30:00-06:00",  # US spring-forward: 2:30 AM doesn't exist
    ],
)
def test_dst_boundaries_round_trip(local_iso):
    """DST is where the timezone bugs live. Round-tripping must preserve the
    instant.

    Note the comparison goes through UTC deliberately. Per PEP 495, an aware
    datetime sitting inside a DST fold does NOT compare equal to the same
    instant carrying a different tzinfo object — `==` returns False even when
    both sides have identical UTC offsets. Comparing instants is both the
    correct assertion and the only reliable one.
    """
    stored = timeutil.to_utc_iso(local_iso)
    back = timeutil.to_local(stored, "America/Denver")
    assert back.astimezone(UTC) == timeutil.parse(local_iso).astimezone(UTC)


def test_speak_datetime_is_tts_safe():
    tz = "America/Denver"
    dt = datetime.now(ZoneInfo(tz)).replace(hour=15, minute=0, second=0, microsecond=0)
    spoken = timeutil.speak_datetime(timeutil.to_utc_iso(dt), tz)
    assert "today at 3 PM" == spoken
    for banned in ("T", "Z", "-06:00", "*", "#"):
        assert banned not in spoken


# ── mutations ─────────────────────────────────────────────


def test_insert_logs_a_mutation(conn):
    row_id = mutations.insert(conn, None, "notes", {"body": "milk"})
    m = conn.execute("SELECT * FROM mutations").fetchone()
    assert (m["table_name"], m["row_id"], m["op"]) == ("notes", row_id, "insert")
    assert m["before_json"] is None and m["after_json"] is not None


def test_undo_removes_an_inserted_row(conn):
    mutations.insert(conn, None, "notes", {"body": "milk"})
    assert mutations.undo_last(conn)["op"] == "insert"
    assert conn.execute("SELECT count(*) c FROM notes").fetchone()["c"] == 0


def test_undo_restores_previous_values_on_update(conn):
    row_id = mutations.insert(conn, None, "events", {"title": "dentist", "starts_at": "2026-07-30T21:00:00Z"})
    mutations.update(conn, None, "events", row_id, {"starts_at": "2026-07-31T21:00:00Z"})
    mutations.undo_last(conn)
    assert conn.execute("SELECT starts_at FROM events WHERE id=?", (row_id,)).fetchone()[0] == "2026-07-30T21:00:00Z"


def test_undo_is_not_itself_undoable(conn):
    """A second undo must not re-apply the change — that isn't what anyone
    means by 'undo that'."""
    mutations.insert(conn, None, "notes", {"body": "milk"})
    mutations.undo_last(conn)
    assert mutations.undo_last(conn) is None
    assert conn.execute("SELECT count(*) c FROM notes").fetchone()["c"] == 0


def test_undo_restores_a_soft_deleted_row(conn):
    row_id = mutations.insert(conn, None, "notes", {"body": "milk"})
    mutations.soft_delete(conn, None, "notes", row_id)
    mutations.undo_last(conn)
    assert conn.execute("SELECT deleted_at FROM notes WHERE id=?", (row_id,)).fetchone()[0] is None


def test_rejects_writes_to_non_domain_tables(conn):
    with pytest.raises(ValueError):
        mutations.insert(conn, None, "mutations", {"op": "insert"})


# ── FTS ───────────────────────────────────────────────────


def test_fts_index_follows_inserts(conn):
    mutations.insert(conn, None, "notes", {"body": "Sarah's kid is named Theo"})
    hit = conn.execute("SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'Theo'").fetchone()
    assert hit is not None


def test_soft_deleted_notes_stay_in_fts_index(conn):
    """Documents the trap: soft delete fires the UPDATE trigger, not the DELETE
    trigger, so the row remains indexed. Search must join notes and filter."""
    row_id = mutations.insert(conn, None, "notes", {"body": "Theo"})
    mutations.soft_delete(conn, None, "notes", row_id)
    assert conn.execute("SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'Theo'").fetchone() is not None
    survived = conn.execute(
        """SELECT n.id FROM notes_fts f JOIN notes n ON n.id = f.rowid
             WHERE notes_fts MATCH 'Theo' AND n.deleted_at IS NULL"""
    ).fetchone()
    assert survived is None


# ── router calendar table ─────────────────────────────────


def test_calendar_table_includes_today_as_its_own_weekday():
    """Regression: the table started at tomorrow, so "Friday at 6" said on a
    Friday morning resolved to *next* Friday — there was no row telling the
    model that Friday could mean today."""
    from app import router

    friday = datetime(2026, 7, 31, 8, 0, tzinfo=ZoneInfo("America/Denver"))
    table = router.calendar_table(friday)
    line = next(l for l in table.splitlines() if l.strip().startswith("Friday"))
    assert "2026-07-31" in line, f"today's weekday missing from table: {line}"
    assert "2026-08-07" in line, "the 'next' column should be a week out"


def test_calendar_table_covers_all_seven_weekdays():
    from app import router

    table = router.calendar_table(datetime(2026, 7, 31, 8, 0, tzinfo=ZoneInfo("America/Denver")))
    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        assert any(l.strip().startswith(day) for l in table.splitlines()), day


# ── undo groups by utterance ──────────────────────────────


def test_undo_reverses_every_write_from_one_utterance(db):
    """A handler that writes twice must undo twice.

    `add_event` with a new person inserts into `people` and `events` under one
    utterance_id. Reversing only the newest leaves an orphan.
    """
    from app import mutations
    from app.db import transaction

    with transaction() as conn:
        utterance_id = int(
            conn.execute(
                "INSERT INTO utterances (raw_text, client) VALUES ('lunch with Sarah','test')"
            ).lastrowid
        )
        person_id = mutations.insert(conn, utterance_id, "people", {"name": "Sarah"})
        mutations.insert(
            conn,
            utterance_id,
            "events",
            {"title": "lunch", "starts_at": "2026-08-01T18:00:00Z", "source": "voice"},
        )

    with transaction() as conn:
        undone = mutations.undo_last(conn)

    assert undone["table"] == "events", "reports the newest reversed row"

    with transaction() as conn:
        events = conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
        people = conn.execute(
            "SELECT count(*) AS n FROM people WHERE id = ?", (person_id,)
        ).fetchone()["n"]
        open_rows = conn.execute(
            "SELECT count(*) AS n FROM mutations WHERE undone_at IS NULL"
        ).fetchone()["n"]

    assert events == 0
    assert people == 0, "the person inserted by the same utterance must go too"
    assert open_rows == 0, "every mutation in the group is stamped undone"


def test_undo_does_not_reach_across_utterances(db):
    from app import mutations
    from app.db import transaction

    with transaction() as conn:
        first = int(
            conn.execute(
                "INSERT INTO utterances (raw_text, client) VALUES ('one','test')"
            ).lastrowid
        )
        mutations.insert(conn, first, "notes", {"body": "keep me"})
        second = int(
            conn.execute(
                "INSERT INTO utterances (raw_text, client) VALUES ('two','test')"
            ).lastrowid
        )
        mutations.insert(conn, second, "notes", {"body": "drop me"})

    with transaction() as conn:
        mutations.undo_last(conn)
        bodies = [r["body"] for r in conn.execute("SELECT body FROM notes")]

    assert bodies == ["keep me"]


def test_undo_of_an_unattributed_mutation_reverses_only_itself(db):
    """utterance_id is NULL for writes with no utterance behind them.

    Grouping on NULL would sweep up every unattributed mutation ever made.
    """
    from app import mutations
    from app.db import transaction

    with transaction() as conn:
        mutations.insert(conn, None, "notes", {"body": "first"})
        mutations.insert(conn, None, "notes", {"body": "second"})

    with transaction() as conn:
        mutations.undo_last(conn)
        bodies = [r["body"] for r in conn.execute("SELECT body FROM notes ORDER BY id")]

    assert bodies == ["first"]


def test_log_insert_makes_an_existing_row_undoable_as_an_insert(db):
    from app import mutations
    from app.db import transaction

    with transaction() as conn:
        row_id = int(
            conn.execute(
                "INSERT INTO notes (body) VALUES ('drafted earlier')"
            ).lastrowid
        )
        mutations.log_insert(conn, None, "notes", row_id)

    with transaction() as conn:
        undone = mutations.undo_last(conn)
        left = conn.execute("SELECT count(*) AS n FROM notes").fetchone()["n"]

    assert undone["op"] == "insert"
    assert left == 0, "undoing an adopted insert deletes the row"
