"""The 10pm prompt.

Runs inside the scheduler, which exists so reminders fire when the agent is
broken. Nothing in the import graph below may reach an LLM.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "nudge.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


@pytest.fixture
def pushes(monkeypatch):
    from app import notify

    sent = []

    def fake_push(body, **kwargs):
        sent.append((body, kwargs))
        return True

    monkeypatch.setattr(notify, "push", fake_push)
    return sent


def rows(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def logged(db, on, count):
    from app.db import transaction

    with transaction() as conn:
        for index in range(count):
            conn.execute(
                """INSERT INTO gratitude_entries (body, entry_on, created_at)
                     VALUES (?,?,?)""",
                (f"thing {index}", on, f"{on}T20:00:00Z"),
            )


def run_sweep(monkeypatch, on="2026-08-04", hour=22):
    from gratitude import nudge

    monkeypatch.setattr(nudge, "_local_day_and_hour", lambda tz: (on, hour))
    return nudge.sweep("America/Denver")


def test_an_empty_day_is_prompted(db, pushes, monkeypatch):
    result = run_sweep(monkeypatch)

    assert result == {"logged": 0, "pushed": True}
    assert len(pushes) == 1
    assert "grateful" in pushes[0][0].lower()


def test_the_push_carries_what_the_phone_needs_to_route_it(db, pushes, monkeypatch):
    run_sweep(monkeypatch)

    _, kwargs = pushes[0]
    assert kwargs["category"] == "GRATITUDE"
    assert kwargs["data"] == {"kind": "gratitude"}
    assert kwargs["collapse_id"] == "gratitude-2026-08-04"


def test_a_partial_day_is_told_how_many_are_left(db, pushes, monkeypatch):
    logged(db, "2026-08-04", 2)
    result = run_sweep(monkeypatch)

    assert result["pushed"] is True
    assert "one more" in pushes[0][0].lower()


def test_a_finished_day_is_never_prompted(db, pushes, monkeypatch):
    """The sweep reads the entries live rather than scheduling a reminder
    ahead of time, so logging your three at eight cannot strand a push."""
    logged(db, "2026-08-04", 3)
    result = run_sweep(monkeypatch)

    assert result == {"logged": 3, "pushed": False}
    assert pushes == []


def test_a_fourth_entry_still_counts_as_finished(db, pushes, monkeypatch):
    logged(db, "2026-08-04", 4)
    assert run_sweep(monkeypatch)["pushed"] is False
    assert pushes == []


def test_nothing_is_pushed_before_the_hour(db, pushes, monkeypatch):
    result = run_sweep(monkeypatch, hour=21)

    assert result == {"logged": 0, "pushed": False}
    assert pushes == []


def test_after_midnight_is_before_the_hour(db, pushes, monkeypatch):
    """00:30 is hour 0, which is under 22 — so the prompt does not fire in the
    small hours even though the gratitude day is still open."""
    assert run_sweep(monkeypatch, hour=0)["pushed"] is False
    assert pushes == []


def test_the_prompt_goes_out_once_a_day(db, pushes, monkeypatch):
    run_sweep(monkeypatch)
    run_sweep(monkeypatch)
    run_sweep(monkeypatch)

    assert len(pushes) == 1


def test_the_next_evening_is_prompted_again(db, pushes, monkeypatch):
    run_sweep(monkeypatch, on="2026-08-04")
    run_sweep(monkeypatch, on="2026-08-05")

    assert len(pushes) == 2


def test_a_failed_push_is_retried_next_tick(db, monkeypatch):
    """push returning False means it went nowhere — with no registered device
    that is the normal case. Stamping anyway would claim a delivery that did
    not happen."""
    from app import notify

    monkeypatch.setattr(notify, "push", lambda body, **kwargs: False)
    assert run_sweep(monkeypatch)["pushed"] is False
    assert rows(db, "SELECT * FROM heartbeats WHERE name = 'gratitude'") == []

    sent = []
    monkeypatch.setattr(notify, "push", lambda body, **kwargs: sent.append(body) or True)
    assert run_sweep(monkeypatch)["pushed"] is True
    assert len(sent) == 1
    stamped = rows(db, "SELECT detail FROM heartbeats WHERE name = 'gratitude'")
    assert stamped == [{"detail": "2026-08-04"}]


def test_the_message_is_safe_to_speak(db):
    from gratitude import nudge

    for count in (0, 1, 2):
        body = nudge.message(count)
        assert "\n" not in body
        assert not any(ch in body for ch in "*_#`[]")
