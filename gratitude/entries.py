"""Three things a day: the writes, the reads, and the streak.

The scheduler imports this through `gratitude.nudge`, so nothing here may
reach an LLM — design principle 3 says the evening prompt keeps arriving on
nights when the agent is broken.
"""

from datetime import date, datetime, timedelta

from app import config, mutations, timeutil

# Three is the feature, not a setting. A two-item day would make the page's
# three slots a lie, and a ten-item day is not what the prompt is asking for.
TARGET = 3


def day_for(local: datetime, day_start: int | None = None) -> str:
    """Which gratitude day an aware LOCAL datetime belongs to.

    The day runs to `GRATITUDE_DAY_START` rather than to midnight. Pure date
    arithmetic on the local wall clock, so it is indifferent to DST: both
    halves of an ambiguous 01:30 are before the cutoff and land on the same
    date.

    The single owner of this rule. Every `entry_on` in the database and every
    day-comparison in this package comes through here, so there is no second
    place for the boundary to be decided differently.
    """
    if day_start is None:
        day_start = config.GRATITUDE_DAY_START
    if local.hour < day_start:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def add(
    conn,
    utterance_id: int | None,
    items: list[str],
    tz_name: str,
) -> tuple[int, int]:
    """Append what was just said to today. Returns (added, day total).

    Every item is its own row and its own mutation, all sharing one
    `utterance_id` — which is what makes `undo_last` take the whole turn back
    instead of the last thing you said in it.
    """
    local = timeutil.now(tz_name)
    on = day_for(local)
    created_at = timeutil.to_utc_iso(local)

    cleaned = [" ".join(str(item).split()) for item in items or []]
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        raise ValueError("log_gratitude needs at least one thing")

    for body in cleaned:
        mutations.insert(
            conn,
            utterance_id,
            "gratitude_entries",
            {"body": body, "entry_on": on, "created_at": created_at},
        )
    return len(cleaned), count_for(conn, on)


def count_for(conn, on: str) -> int:
    return int(
        conn.execute(
            "SELECT count(*) AS n FROM gratitude_entries WHERE entry_on = ?", (on,)
        ).fetchone()["n"]
    )


def for_day(conn, on: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """SELECT id, body, created_at AS at FROM gratitude_entries
                 WHERE entry_on = ? ORDER BY created_at, id""",
            (on,),
        ).fetchall()
    ]


def recent(conn, tz_name: str, days: int) -> list[dict]:
    """Past days with entries, newest first. Today is excluded — it is served
    separately, because the top card and the history render differently and
    merging them would make the view re-derive which group is 'now'."""
    today = day_for(timeutil.now(tz_name))
    floor = (date.fromisoformat(today) - timedelta(days=days)).isoformat()

    grouped: list[dict] = []
    for row in conn.execute(
        """SELECT id, body, entry_on, created_at AS at FROM gratitude_entries
             WHERE entry_on < ? AND entry_on >= ?
             ORDER BY entry_on DESC, created_at, id""",
        (today, floor),
    ).fetchall():
        item = dict(row)
        on = item.pop("entry_on")
        if not grouped or grouped[-1]["on"] != on:
            grouped.append({"on": on, "entries": []})
        grouped[-1]["entries"].append(item)
    return grouped
