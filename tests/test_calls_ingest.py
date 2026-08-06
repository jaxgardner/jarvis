"""Call history. ZDATE is Core Data epoch — seconds, not nanoseconds."""

import sqlite3

import pytest

from ingest import calls
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "calls.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)

    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_core_data_epoch_is_seconds_not_nanoseconds():
    """The classic off-by-31-years. CallHistory counts SECONDS from
    2001-01-01; chat.db counts nanoseconds. Same epoch, different units."""
    row = calls.to_row(
        {
            "external_id": 1,
            "handle": "+15551234",
            "originated": 0,
            "answered": 0,
            "duration": 0,
            "apple_date": 807_537_600,
        }
    )
    assert row["occurred_at"].startswith("2026-08-04T12:00:00")


def test_missed_call_is_recorded_as_unanswered():
    row = calls.to_row(
        {
            "external_id": 2,
            "handle": "+1",
            "originated": 0,
            "answered": 0,
            "duration": 0,
            "apple_date": 807_537_600,
        }
    )
    assert row["direction"] == "in"
    assert row["answered"] == 0


def test_outgoing_call_direction():
    row = calls.to_row(
        {
            "external_id": 3,
            "handle": "+1",
            "originated": 1,
            "answered": 1,
            "duration": 42,
            "apple_date": 807_537_600,
        }
    )
    assert row["direction"] == "out"
    assert row["duration_s"] == 42


def test_store_is_idempotent(conn):
    row = {
        "external_id": "7",
        "handle": "+1",
        "direction": "in",
        "answered": 0,
        "duration_s": 0,
        "occurred_at": "2026-08-04T12:00:00+00:00",
    }
    calls.store(conn, row)
    calls.store(conn, row)
    conn.commit()
    n = conn.execute("SELECT count(*) AS n FROM calls").fetchone()["n"]
    assert n == 1


def test_missing_helper_is_not_fatal(conn, monkeypatch):
    """Same posture as the messages importer: no binary and no grant leave
    every other ingester on the tick running."""
    monkeypatch.setattr(calls, "HELPER", "/nonexistent/tccread")
    result = calls.sync()
    assert result["ok"] is False
    assert "tccread" in result["detail"]
