"""Snooze / Done — the notification buttons. Offline.

These are the first things that ever write reminders.status = 'acked', a state
declared in 001 and described in 002 with nothing to set it until now.
"""

import sqlite3
from datetime import timedelta

import pytest

from app import timeutil
from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "actions.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {SHARED}"
    return c


def add_fired_reminder(db, body="take the bins out", recurrence=None) -> int:
    fire_at = timeutil.to_utc_iso(timeutil.now("UTC") - timedelta(minutes=1))
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            """INSERT INTO reminders (body, fire_at, recurrence, status, fired_at)
                 VALUES (?,?,?,'fired',?)""",
            (body, fire_at, recurrence, fire_at),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def row(db, reminder_id: int) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone())
    finally:
        conn.close()


# ── ack ───────────────────────────────────────────────────


def test_ack_marks_the_reminder_dealt_with(client, db):
    reminder_id = add_fired_reminder(db)
    response = client.post(f"/reminders/{reminder_id}/ack")
    assert response.status_code == 200
    assert row(db, reminder_id)["status"] == "acked"


def test_ack_is_idempotent(client, db):
    """A double-tap on a lock screen is not an error."""
    reminder_id = add_fired_reminder(db)
    client.post(f"/reminders/{reminder_id}/ack")
    second = client.post(f"/reminders/{reminder_id}/ack")
    assert second.status_code == 200
    assert row(db, reminder_id)["status"] == "acked"


def test_ack_of_an_unknown_reminder_is_a_404(client):
    assert client.post("/reminders/9999/ack").status_code == 404


# ── snooze ────────────────────────────────────────────────


def test_snooze_requeues_the_reminder(client, db):
    reminder_id = add_fired_reminder(db)
    response = client.post(f"/reminders/{reminder_id}/snooze", json={"minutes": 10})
    assert response.status_code == 200

    after = row(db, reminder_id)
    assert after["status"] == "pending", "a snoozed reminder must be claimable again"
    assert after["fired_at"] is None
    fire_at = timeutil.parse(after["fire_at"])
    assert timedelta(minutes=9) < fire_at - timeutil.now("UTC") < timedelta(minutes=11)


def test_snooze_defaults_to_ten_minutes(client, db):
    """Matches the notification's 'Snooze 10m' button, which sends no body."""
    reminder_id = add_fired_reminder(db)
    client.post(f"/reminders/{reminder_id}/snooze", json={})
    delta = timeutil.parse(row(db, reminder_id)["fire_at"]) - timeutil.now("UTC")
    assert timedelta(minutes=9) < delta < timedelta(minutes=11)


def test_snoozing_a_recurring_reminder_does_not_clone_the_series(client, db):
    """The trap: the scheduler already inserted the next occurrence when this
    one fired. If the snoozed row kept its recurrence rule it would insert a
    *second* one on re-fire, and a daily reminder would quietly become two."""
    reminder_id = add_fired_reminder(db, recurrence="daily")
    client.post(f"/reminders/{reminder_id}/snooze", json={"minutes": 10})
    assert row(db, reminder_id)["recurrence"] is None


def test_snooze_is_undoable(client, db):
    """Every mutation is reversible — including one made by a fat thumb on a
    lock screen."""
    reminder_id = add_fired_reminder(db)
    before = row(db, reminder_id)
    client.post(f"/reminders/{reminder_id}/snooze", json={"minutes": 30})
    assert client.post("/undo").json()["undone"] is True

    restored = row(db, reminder_id)
    assert restored["status"] == before["status"] == "fired"
    assert restored["fire_at"] == before["fire_at"]


def test_ack_is_undoable(client, db):
    reminder_id = add_fired_reminder(db)
    client.post(f"/reminders/{reminder_id}/ack")
    client.post("/undo")
    assert row(db, reminder_id)["status"] == "fired"


def test_snooze_rejects_an_absurd_window(client, db):
    reminder_id = add_fired_reminder(db)
    assert client.post(f"/reminders/{reminder_id}/snooze", json={"minutes": 0}).status_code == 422
    assert (
        client.post(f"/reminders/{reminder_id}/snooze", json={"minutes": 99999}).status_code == 422
    )


def test_snoozed_reminder_actually_fires_again(client, db, monkeypatch):
    """End to end: the scheduler must re-claim it. Status 'pending' is only
    meaningful if the thing that reads that column agrees."""
    from scheduler import run as scheduler

    sent: list[str] = []
    monkeypatch.setattr(
        scheduler.notify, "push", lambda message, **kw: (sent.append(message), True)[1]
    )

    reminder_id = add_fired_reminder(db, body="bins")
    client.post(f"/reminders/{reminder_id}/snooze", json={"minutes": 1})

    # Reach into the future by moving the reminder's fire_at into the past —
    # cheaper and more honest than freezing the clock.
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE reminders SET fire_at = ? WHERE id = ?",
        (timeutil.to_utc_iso(timeutil.now("UTC") - timedelta(seconds=30)), reminder_id),
    )
    conn.commit()
    conn.close()

    assert scheduler.tick()["fired"] == [reminder_id]
    assert "bins" in sent
