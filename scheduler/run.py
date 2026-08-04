"""Reminder scheduler — a separate process, on purpose.

Principle 3 from the design doc: reminders must fire even when the agent is
broken. Nothing in this file imports the router, touches Anthropic, or makes
any network call except the ntfy push. If the API server is wedged, crashed,
or mid-deploy, reminders still go out.

Runs every 60s via launchd StartInterval. One tick, then exit — no internal
loop, so a hung tick can't wedge the schedule permanently.

    uv run python -m scheduler.run
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import config, notify, timeutil
from app.db import transaction
from gratitude import nudge
from pantry import expiry

# A reminder that came due while the machine was down is worth delivering late
# — but only up to a point. Past this, waking someone for a 6-hour-old prompt
# is worse than staying quiet.
CATCHUP_LIMIT = timedelta(hours=6)

SELFCHECK_INTERVAL = timedelta(hours=24)


# ── recurrence ────────────────────────────────────────────

_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def next_occurrence(fire_at: str, recurrence: str, tz_name: str) -> str | None:
    """Next fire time after `fire_at`, or None if the rule isn't understood.

    Computed in LOCAL wall-clock time, not by adding a timedelta to the UTC
    instant. A daily 9 AM reminder must stay at 9 AM across a DST boundary;
    UTC arithmetic would silently slide it to 8 or 10.
    """
    rule = (recurrence or "").strip().lower()
    if not rule:
        return None

    zone = ZoneInfo(tz_name)
    local = timeutil.parse(fire_at).astimezone(zone)
    now_local = timeutil.now(tz_name)

    def relocalize(naive: datetime) -> datetime:
        return naive.replace(tzinfo=zone)

    if rule == "daily":
        step = 1
        allowed: set[int] | None = None
    elif rule.startswith("weekly:"):
        codes = [c.strip().upper()[:2] for c in rule.split(":", 1)[1].split(",") if c.strip()]
        allowed = {_WEEKDAYS[c] for c in codes if c in _WEEKDAYS}
        if not allowed:
            return None
        step = 1
    elif rule == "weekly":
        step = 7
        allowed = None
    else:
        return None  # RRULE and anything else: not supported yet

    naive = local.replace(tzinfo=None)
    for _ in range(400):  # bounded so a pathological rule can't spin forever
        naive = naive + timedelta(days=step)
        candidate = relocalize(naive)
        if candidate <= now_local:
            continue
        if allowed is None or candidate.weekday() in allowed:
            return timeutil.to_utc_iso(candidate)
    return None


# ── the tick ──────────────────────────────────────────────


def _beat(conn, name: str, detail: str | None = None) -> None:
    conn.execute(
        """INSERT INTO heartbeats (name, last_run_at, detail) VALUES (?,?,?)
             ON CONFLICT(name) DO UPDATE SET last_run_at = excluded.last_run_at,
                                             detail = excluded.detail""",
        (name, timeutil.to_utc_iso(timeutil.now("UTC")), detail),
    )


def tick(tz_name: str | None = None) -> dict:
    tz_name = tz_name or config.DEFAULT_TZ
    now = timeutil.now("UTC")
    now_iso = timeutil.to_utc_iso(now)
    fired: list[int] = []
    missed: list[str] = []
    failed: list[int] = []

    # Atomic claim. UPDATE ... RETURNING hands each due row to exactly one
    # caller, so two overlapping ticks — which launchd will happily produce if
    # one run is slow — cannot double-send.
    with transaction() as conn:
        claimed = conn.execute(
            """UPDATE reminders SET status = 'firing'
                 WHERE status = 'pending' AND fire_at <= ?
                 RETURNING *""",
            (now_iso,),
        ).fetchall()
        due = [dict(r) for r in claimed]

    for row in due:
        overdue = now - timeutil.parse(row["fire_at"])

        if overdue > CATCHUP_LIMIT:
            with transaction() as conn:
                conn.execute(
                    "UPDATE reminders SET status = 'missed' WHERE id = ?", (row["id"],)
                )
            missed.append(row["body"])
            _schedule_next(row, tz_name)
            continue

        late = " (delayed)" if overdue > timedelta(minutes=5) else ""
        # category + reminder_id are what make the notification actionable on
        # iOS: the app registers a 'REMINDER' category with Snooze / Done /
        # Undo buttons, and the id tells those buttons what they are acting
        # on. Both are ignored by the ntfy backend, so this stays correct
        # during the dual-send window.
        ok = notify.push(
            row["body"],
            title=f"Reminder{late}",
            tags="alarm_clock",
            priority="default",
            category="REMINDER",
            data={"reminder_id": row["id"], "kind": "reminder"},
            collapse_id=f"reminder-{row['id']}",
        )

        with transaction() as conn:
            if ok:
                conn.execute(
                    "UPDATE reminders SET status='fired', fired_at=? WHERE id = ?",
                    (now_iso, row["id"]),
                )
            else:
                # Back to pending so the next tick retries. Leaving it 'firing'
                # would strand it forever — nothing ever re-claims that state.
                conn.execute(
                    "UPDATE reminders SET status='pending' WHERE id = ?", (row["id"],)
                )
        (fired if ok else failed).append(row["id"])
        if ok:
            _schedule_next(row, tz_name)

    if missed:
        notify.push(
            f"{len(missed)} reminder(s) came due while I was offline and were "
            "too old to deliver: " + "; ".join(missed[:5]),
            title="Missed while offline",
            tags="warning",
        )

    with transaction() as conn:
        _beat(conn, "scheduler", f"due={len(due)} fired={len(fired)} missed={len(missed)}")

    # Pantry expiry. Wrapped because a tick must never take out the schedule:
    # design principle 3 is that reminders fire even when everything else is
    # broken, and the pantry is very much everything else.
    try:
        expiry.sweep(tz_name)
    except Exception as exc:  # noqa: BLE001
        print(f"pantry expiry sweep failed: {exc}", file=sys.stderr)

    # Same guard, same reason: a gratitude bug must not cost you a reminder.
    try:
        nudge.sweep(tz_name)
    except Exception as exc:  # noqa: BLE001
        print(f"gratitude sweep failed: {exc}", file=sys.stderr)

    _selfcheck(tz_name)
    return {"due": len(due), "fired": fired, "missed": missed, "failed": failed}


def _schedule_next(row: dict, tz_name: str) -> None:
    """Insert the next occurrence of a recurring reminder.

    Chained off the scheduled fire_at rather than now(), so a late delivery
    doesn't drag every future occurrence later with it.
    """
    if not row.get("recurrence"):
        return
    upcoming = next_occurrence(row["fire_at"], row["recurrence"], tz_name)
    if upcoming is None:
        return
    with transaction() as conn:
        conn.execute(
            """INSERT INTO reminders (body, fire_at, recurrence, event_id)
                 VALUES (?,?,?,?)""",
            (row["body"], upcoming, row["recurrence"], row.get("event_id")),
        )


def _selfcheck(tz_name: str) -> None:
    """Daily 'still alive' push.

    The failure this catches is the quiet one: the scheduler stops running and
    nothing happens — no error, no alert, just reminders that never arrive.
    A heartbeat you'd notice missing is the only way to detect that.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT last_run_at FROM heartbeats WHERE name = 'selfcheck'"
        ).fetchone()
        now = timeutil.now("UTC")
        if row and now - timeutil.parse(row["last_run_at"]) < SELFCHECK_INTERVAL:
            return
        pending = conn.execute(
            "SELECT count(*) AS n FROM reminders WHERE status = 'pending'"
        ).fetchone()["n"]
        _beat(conn, "selfcheck", f"pending={pending}")

        # Piggy-backs on the daily selfcheck rather than running every 60s.
        # A photo's only use is re-reading a bad extraction, so it ages out 30
        # days after the receipt is confirmed.
        #
        # Guarded: housekeeping must never cost the heartbeat. This runs inside
        # the transaction that records the beat, so letting it raise would roll
        # that back and re-push the daily check on every tick — and on a
        # database predating migration 008 the table isn't there at all.
        from pantry import images

        try:
            images.prune(conn)
        except sqlite3.Error as exc:
            print(f"receipt image prune failed: {exc}", file=sys.stderr)

    notify.push(
        f"Jarvis is running. {pending} reminder(s) pending.",
        title="Daily check",
        tags="white_check_mark",
        priority="low",
    )


def main() -> int:
    try:
        result = tick()
    except Exception as exc:  # noqa: BLE001 — a tick must never take out the schedule
        print(f"scheduler tick failed: {exc}", file=sys.stderr)
        return 1
    if result["due"]:
        print(
            f"due={result['due']} fired={len(result['fired'])} "
            f"missed={len(result['missed'])} failed={len(result['failed'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
