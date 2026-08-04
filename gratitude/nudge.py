"""The evening prompt: three things, if the day is unfinished.

Deliberately NOT built on `reminders` rows, for the same reasons the pantry
expiry sweep isn't:

  * A reminder shows up in /agenda. A gratitude prompt is not an appointment.
  * A reminder is scheduled ahead of time, so logging your three at eight
    would leave one still due at ten. This sweep reads `gratitude_entries`
    live and is therefore correct by construction: a finished day cannot
    notify.

No LLM dependency — the scheduler imports this, and design principle 3 says
the scheduler keeps working when the agent does not.
"""

from app import config, notify, timeutil
from gratitude import entries


def _local_day_and_hour(tz_name: str) -> tuple[str, int]:
    """Seam for tests. The day carries the 4am cutoff; the hour is real clock
    time, because when to interrupt someone is a wall-clock question."""
    local = timeutil.now(tz_name)
    return entries.day_for(local), local.hour


def message(count: int) -> str:
    """The push body. Templated, not generated — the scheduler has no model."""
    if count <= 0:
        return "Three things you're grateful for?"
    left = entries.TARGET - count
    return f"{'One' if left == 1 else 'Two'} more — what else are you grateful for?"


def _already_pushed(conn, on: str) -> bool:
    row = conn.execute(
        "SELECT detail FROM heartbeats WHERE name = 'gratitude'"
    ).fetchone()
    return row is not None and row["detail"] == on


def _stamp(conn, on: str) -> None:
    """One push a day, tracked in `heartbeats` rather than a table of its own.

    `_selfcheck` already uses that table for exactly this shape of thing — a
    daily event that must not repeat — and one string does not earn a schema.
    """
    conn.execute(
        """INSERT INTO heartbeats (name, last_run_at, detail) VALUES ('gratitude',?,?)
             ON CONFLICT(name) DO UPDATE SET last_run_at = excluded.last_run_at,
                                             detail = excluded.detail""",
        (timeutil.to_utc_iso(timeutil.now("UTC")), on),
    )


def sweep(tz_name: str | None = None) -> dict:
    """One prompt an evening, and only when there is something to prompt for.

    Nothing is stamped unless the push actually landed: `notify.push` returning
    False means it went nowhere, and recording it would claim a delivery that
    did not happen. The pantry sweep makes the same promise.

    The window is GRATITUDE_HOUR to midnight. A Mini that was asleep all
    evening produces no push and no catch-up, unlike `reminders`, which
    deliver up to six hours late — a gratitude prompt at 8am is about a day
    that is already gone.
    """
    from app.db import transaction

    tz_name = tz_name or config.DEFAULT_TZ
    on, hour = _local_day_and_hour(tz_name)

    if hour < config.GRATITUDE_HOUR:
        return {"logged": 0, "pushed": False}

    with transaction() as conn:
        if _already_pushed(conn, on):
            return {"logged": entries.count_for(conn, on), "pushed": False}
        count = entries.count_for(conn, on)

    if count >= entries.TARGET:
        return {"logged": count, "pushed": False}

    ok = notify.push(
        message(count),
        title="Gratitude",
        tags="sparkles",
        priority="default",
        category="GRATITUDE",
        data={"kind": "gratitude"},
        collapse_id=f"gratitude-{on}",
    )
    if not ok:
        return {"logged": count, "pushed": False}

    with transaction() as conn:
        _stamp(conn, on)
    return {"logged": count, "pushed": True}
