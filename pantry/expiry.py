"""What is about to spoil, and telling you the day before.

Deliberately NOT built on `reminders` rows. Two reasons, both learned from
what reminders are for:

  * A reminder shows up in /agenda. Groceries do not belong among your
    appointments.
  * A reminder is scheduled ahead of time, so finishing the milk early would
    strand one that still fires. This sweep reads `pantry_items` directly and
    is therefore correct by construction: an item that is gone cannot notify.

No LLM dependency — the scheduler imports this, and design principle 3 says
the scheduler keeps working when the agent does not.
"""

import sqlite3
from datetime import date, timedelta

from app import config, notify, timeutil
from pantry import inventory


def _local_date_and_hour(tz_name: str) -> tuple[str, int]:
    """Seam for tests. Local, because 'expires tomorrow' is a wall-clock
    question — nobody thinks about their spinach in UTC."""
    local = timeutil.now(tz_name)
    return local.date().isoformat(), local.hour


def due_tomorrow(conn: sqlite3.Connection, today: str) -> list[dict]:
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    return [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM pantry_items
                 WHERE status = 'active'
                   AND expires_on = ?
                   AND notified_on IS NULL
                 ORDER BY name""",
            (tomorrow,),
        ).fetchall()
    ]


def message(items: list[dict]) -> str:
    """The push body. Templated, not generated — the scheduler has no model,
    and this is exactly the kind of sentence Python formats well."""
    names = [item["name"] for item in items]
    if len(names) == 1:
        return f"{names[0]} expires tomorrow."
    listed = ", ".join(names[:-1]) + f" and {names[-1]}"
    return f"{len(names)} things expire tomorrow: {listed}."


def mark_notified(conn: sqlite3.Connection, item_ids: list[int], today: str) -> None:
    marks = ",".join("?" for _ in item_ids)
    conn.execute(
        f"UPDATE pantry_items SET notified_on = ? WHERE id IN ({marks})",  # noqa: S608
        (today, *item_ids),
    )


def sweep(tz_name: str | None = None) -> dict:
    """One batched push per day for everything expiring tomorrow.

    Batched on purpose: a notification per item is how a useful feature
    becomes one you mute.

    Nothing is stamped and nothing is listed unless the push actually landed.
    `notify.push` returning False means it went nowhere — with no registered
    device that is the normal case — and recording it anyway would claim a
    delivery that did not happen.
    """
    from app.db import transaction

    tz_name = tz_name or config.DEFAULT_TZ
    today, hour = _local_date_and_hour(tz_name)

    if hour < config.PANTRY_EXPIRY_HOUR:
        # Late enough to still cook or shop, early enough to act on.
        return {"due": 0, "pushed": False}

    with transaction() as conn:
        items = due_tomorrow(conn, today)

    if not items:
        return {"due": 0, "pushed": False}

    ok = notify.push(
        message(items),
        title="Expiring tomorrow",
        tags="warning",
        priority="default",
        category="PANTRY",
        data={"kind": "expiry"},
        collapse_id=f"expiry-{today}",
    )
    if not ok:
        return {"due": len(items), "pushed": False}

    with transaction() as conn:
        mark_notified(conn, [item["id"] for item in items], today)
        for item in items:
            inventory.add_to_list(conn, None, item["name"], "expiring", item["id"])

    return {"due": len(items), "pushed": True}
