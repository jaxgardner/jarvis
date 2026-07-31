"""Scheduler tests — offline. Pushes are stubbed; nothing hits the network."""

import sqlite3
from datetime import timedelta

import pytest

from app import timeutil
from app.config import REPO_ROOT
from scheduler import run as scheduler


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Real schema in a real file, with app.db pointed at it, so the tests
    exercise the actual UPDATE...RETURNING claim rather than a mock."""
    path = tmp_path / "sched.db"
    conn = sqlite3.connect(path)
    for name in ("001_init.sql", "002_scheduler.sql"):
        sql = (REPO_ROOT / "migrations" / name).read_text()
        conn.executescript(
            "\n".join(
                l for l in sql.splitlines() if not l.strip().upper().startswith("PRAGMA")
            )
        )
    conn.commit()
    conn.close()

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


@pytest.fixture
def sent(monkeypatch):
    """Capture pushes instead of sending them."""
    calls: list[dict] = []

    def fake_push(message, **kwargs):
        calls.append({"message": message, **kwargs})
        return True

    monkeypatch.setattr(scheduler.notify, "push", fake_push)
    return calls


def add_reminder(db, body: str, offset: timedelta, recurrence: str | None = None) -> int:
    fire_at = timeutil.to_utc_iso(timeutil.now("UTC") + offset)
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO reminders (body, fire_at, recurrence) VALUES (?,?,?)",
        (body, fire_at, recurrence),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return int(row_id)


def status_of(db, row_id: int) -> str:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT status FROM reminders WHERE id = ?", (row_id,)
        ).fetchone()[0]
    finally:
        conn.close()


# ── firing ────────────────────────────────────────────────


def test_fires_a_due_reminder(db, sent):
    row_id = add_reminder(db, "call the dentist", timedelta(minutes=-1))
    result = scheduler.tick()
    assert result["fired"] == [row_id]
    assert status_of(db, row_id) == "fired"
    assert sent[0]["message"] == "call the dentist"


def test_leaves_future_reminders_alone(db, sent):
    row_id = add_reminder(db, "later", timedelta(hours=2))
    assert scheduler.tick()["due"] == 0
    assert status_of(db, row_id) == "pending"
    # The daily self-check legitimately fires on a fresh database, so assert
    # no *reminder* was delivered rather than that nothing was.
    assert [c for c in sent if c.get("title") != "Daily check"] == []


def test_claim_is_atomic_so_two_ticks_cannot_double_send(db, sent):
    """The reason for UPDATE...RETURNING: launchd will overlap ticks whenever
    one run is slow, and a reminder delivered twice is worse than one late."""
    add_reminder(db, "once only", timedelta(minutes=-1))
    scheduler.tick()
    scheduler.tick()
    assert len([c for c in sent if c["message"] == "once only"]) == 1


def test_failed_push_returns_to_pending_for_retry(db, monkeypatch):
    """A failed push must not strand the row in 'firing' — nothing re-claims
    that state, so the reminder would never fire again."""
    row_id = add_reminder(db, "flaky", timedelta(minutes=-1))
    monkeypatch.setattr(scheduler.notify, "push", lambda *a, **k: False)
    result = scheduler.tick()
    assert result["failed"] == [row_id]
    assert status_of(db, row_id) == "pending"


# ── catch-up ──────────────────────────────────────────────


def test_delivers_recently_overdue_reminders(db, sent):
    """Machine was down an hour — still worth delivering."""
    row_id = add_reminder(db, "recent", timedelta(hours=-1))
    scheduler.tick()
    assert status_of(db, row_id) == "fired"
    assert "(delayed)" in sent[0]["title"]


def test_skips_stale_reminders_but_reports_them(db, sent):
    """Overdue past the 6h limit: don't wake someone for it, but don't hide it
    either."""
    row_id = add_reminder(db, "ancient", timedelta(hours=-9))
    result = scheduler.tick()
    assert result["missed"] == ["ancient"]
    assert status_of(db, row_id) == "missed"
    summary = [c for c in sent if c.get("title") == "Missed while offline"]
    assert len(summary) == 1 and "ancient" in summary[0]["message"]


def test_missed_is_distinct_from_fired(db, sent):
    """'missed' must not be recorded as 'fired' — that would claim a
    notification was delivered when none was."""
    row_id = add_reminder(db, "stale", timedelta(hours=-9))
    scheduler.tick()
    assert status_of(db, row_id) != "fired"


# ── recurrence ────────────────────────────────────────────


def test_recurring_reminder_schedules_its_successor(db, sent):
    add_reminder(db, "vitamins", timedelta(minutes=-1), recurrence="daily")
    scheduler.tick()
    conn = sqlite3.connect(db)
    try:
        pending = conn.execute(
            "SELECT fire_at FROM reminders WHERE status='pending' AND body='vitamins'"
        ).fetchall()
    finally:
        conn.close()
    assert len(pending) == 1
    assert timeutil.parse(pending[0][0]) > timeutil.now("UTC")


def test_one_off_reminder_does_not_recur(db, sent):
    add_reminder(db, "once", timedelta(minutes=-1))
    scheduler.tick()
    conn = sqlite3.connect(db)
    try:
        assert (
            conn.execute(
                "SELECT count(*) FROM reminders WHERE status='pending'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_daily_recurrence_holds_wall_clock_time_across_dst():
    """The DST trap: a 9 AM daily reminder must stay 9 AM. Adding 24h to the
    UTC instant would slide it an hour when the offset changes."""
    tz = "America/Denver"
    before_fallback = timeutil.to_utc_iso("2026-10-31T09:00:00-06:00")
    nxt = scheduler.next_occurrence(before_fallback, "daily", tz)
    assert timeutil.to_local(nxt, tz).strftime("%H:%M") == "09:00"


def test_weekly_recurrence_lands_on_listed_days():
    tz = "America/Denver"
    # 2026-08-03 is a Monday.
    monday = timeutil.to_utc_iso("2026-08-03T09:00:00-06:00")
    nxt = scheduler.next_occurrence(monday, "weekly:MO,WE", tz)
    assert timeutil.to_local(nxt, tz).strftime("%A") in {"Monday", "Wednesday"}


def test_unknown_recurrence_rule_is_ignored_not_guessed():
    assert scheduler.next_occurrence(
        timeutil.to_utc_iso(timeutil.now("UTC")), "FREQ=MONTHLY;BYDAY=1FR", "America/Denver"
    ) is None


# ── heartbeat ─────────────────────────────────────────────


def test_tick_records_a_heartbeat(db, sent):
    scheduler.tick()
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT last_run_at FROM heartbeats WHERE name='scheduler'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_selfcheck_pushes_once_per_day(db, sent):
    scheduler.tick()
    scheduler.tick()
    checks = [c for c in sent if c.get("title") == "Daily check"]
    assert len(checks) == 1, "self-check should not fire on every tick"
