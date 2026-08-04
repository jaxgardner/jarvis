"""The day-before expiry push.

This runs inside the scheduler, which exists so reminders fire when the agent
is broken. Nothing in the import graph below may reach an LLM.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "expiry.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


@pytest.fixture
def pushes(monkeypatch):
    """Capture pushes. Returns the list of (body, kwargs) sent."""
    from app import notify

    sent = []

    def fake_push(body, **kwargs):
        sent.append((body, kwargs))
        return True

    monkeypatch.setattr(notify, "push", fake_push)
    return sent


def stock(db, name, expires_on, status="active", notified_on=None):
    from app.db import transaction

    with transaction() as conn:
        return int(
            conn.execute(
                """INSERT INTO pantry_items (name, expires_on, status, notified_on)
                     VALUES (?,?,?,?)""",
                (name, expires_on, status, notified_on),
            ).lastrowid
        )


def rows(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def run_sweep(monkeypatch, today="2026-07-31", hour=18):
    """Run the sweep at a fixed local date and hour."""
    from pantry import expiry

    monkeypatch.setattr(expiry, "_local_date_and_hour", lambda tz: (today, hour))
    return expiry.sweep("America/Denver")


def test_items_expiring_tomorrow_are_pushed(db, pushes, monkeypatch):
    stock(db, "spinach", "2026-08-01")
    result = run_sweep(monkeypatch)

    assert result["due"] == 1
    assert len(pushes) == 1
    assert "spinach" in pushes[0][0].lower()


def test_several_items_batch_into_one_push(db, pushes, monkeypatch):
    """Per-item pushes are how a useful feature becomes a muted one."""
    stock(db, "spinach", "2026-08-01")
    stock(db, "whole milk", "2026-08-01")
    stock(db, "chicken", "2026-08-01")

    run_sweep(monkeypatch)

    assert len(pushes) == 1
    body = pushes[0][0].lower()
    assert "spinach" in body and "milk" in body and "chicken" in body
    assert "3" in pushes[0][0]


def test_nothing_is_pushed_before_the_configured_hour(db, pushes, monkeypatch):
    stock(db, "spinach", "2026-08-01")
    result = run_sweep(monkeypatch, hour=9)

    assert result["due"] == 0
    assert pushes == []


def test_the_same_item_is_not_pushed_twice(db, pushes, monkeypatch):
    stock(db, "spinach", "2026-08-01")
    run_sweep(monkeypatch)
    run_sweep(monkeypatch)

    assert len(pushes) == 1


def test_a_consumed_item_never_notifies(db, pushes, monkeypatch):
    """The sweep reads pantry_items directly rather than creating reminder
    rows, so finishing the milk early cannot strand a notification."""
    stock(db, "spinach", "2026-08-01", status="consumed")
    result = run_sweep(monkeypatch)

    assert result["due"] == 0
    assert pushes == []


def test_a_pending_item_never_notifies(db, pushes, monkeypatch):
    stock(db, "spinach", "2026-08-01", status="pending")
    assert run_sweep(monkeypatch)["due"] == 0


def test_items_expiring_today_or_later_are_not_tomorrow(db, pushes, monkeypatch):
    stock(db, "today's yogurt", "2026-07-31")
    stock(db, "next week's milk", "2026-08-07")
    assert run_sweep(monkeypatch)["due"] == 0


def test_expiring_items_land_on_the_shopping_list(db, pushes, monkeypatch):
    stock(db, "spinach", "2026-08-01")
    run_sweep(monkeypatch)

    listed = rows(db, "SELECT name, reason FROM shopping_list")
    assert listed == [{"name": "spinach", "reason": "expiring"}]


def test_a_failed_push_is_retried_next_tick(db, monkeypatch):
    """notify.push returning False means nothing was delivered. Stamping
    notified_on anyway would claim a delivery that did not happen — the exact
    failure this subsystem exists to make loud."""
    from app import notify

    stock(db, "spinach", "2026-08-01")

    monkeypatch.setattr(notify, "push", lambda body, **kwargs: False)
    first = run_sweep(monkeypatch)

    assert first["pushed"] is False
    assert rows(db, "SELECT notified_on FROM pantry_items")[0]["notified_on"] is None

    sent = []
    monkeypatch.setattr(notify, "push", lambda body, **kwargs: sent.append(body) or True)
    second = run_sweep(monkeypatch)

    assert second["pushed"] is True
    assert len(sent) == 1
    assert rows(db, "SELECT notified_on FROM pantry_items")[0]["notified_on"] == "2026-07-31"


def test_a_failed_push_does_not_list_the_items_either(db, monkeypatch):
    """Otherwise the list fills up with things you were never told about."""
    from app import notify

    stock(db, "spinach", "2026-08-01")
    monkeypatch.setattr(notify, "push", lambda body, **kwargs: False)
    run_sweep(monkeypatch)

    assert rows(db, "SELECT * FROM shopping_list") == []


def test_the_message_is_safe_to_speak_and_names_the_food(db):
    from pantry import expiry

    body = expiry.message([{"name": "spinach"}, {"name": "whole milk"}])
    assert "\n" not in body
    assert not any(ch in body for ch in "*_#`[]")
    assert "spinach" in body and "whole milk" in body


def test_the_scheduler_tick_runs_the_sweep(db, pushes, monkeypatch):
    from pantry import expiry
    from scheduler import run

    called = []
    monkeypatch.setattr(expiry, "sweep", lambda tz_name=None: called.append(tz_name) or {"due": 0, "pushed": False})
    run.tick("America/Denver")

    assert called == ["America/Denver"]


def test_a_broken_sweep_does_not_take_out_the_reminder_tick(db, pushes, monkeypatch):
    """Design principle 3. Reminders must fire even when everything else is
    broken, and the pantry is very much everything else."""
    from pantry import expiry
    from scheduler import run

    def boom(tz_name=None):
        raise RuntimeError("pantry is on fire")

    monkeypatch.setattr(expiry, "sweep", boom)

    from app.db import transaction

    with transaction() as conn:
        conn.execute(
            """INSERT INTO reminders (body, fire_at)
                 VALUES ('take the bins out', strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 minute'))"""
        )

    result = run.tick("America/Denver")

    assert len(result["fired"]) == 1
    assert any("bins" in body for body, _ in pushes)


def test_the_scheduler_imports_no_model_code():
    """A guard, not a nicety: the moment scheduler/ can reach anthropic, the
    'reminders fire when the agent is broken' guarantee is gone."""
    import ast
    from pathlib import Path

    from app.config import REPO_ROOT

    forbidden = {"anthropic", "app.router", "pantry.extract"}
    for path in [REPO_ROOT / "scheduler/run.py", REPO_ROOT / "pantry/expiry.py",
                 REPO_ROOT / "pantry/inventory.py",
                 REPO_ROOT / "gratitude/nudge.py",
                 REPO_ROOT / "gratitude/entries.py"]:
        tree = ast.parse(Path(path).read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert name not in forbidden, f"{path.name} imports {name}"
