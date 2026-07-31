"""Offline tests — no API key, no network. These must always pass."""

import sqlite3
from datetime import datetime, timezone as _tz

UTC = _tz.utc
from zoneinfo import ZoneInfo

import pytest

from app import mutations, timeutil
from app.config import REPO_ROOT


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

    Mixed offsets in one column are not hypothetical — Phase 6 imports calendar
    rows in UTC while voice capture writes local time. Compared as raw strings
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
