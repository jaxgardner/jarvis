"""What is in the fridge, and what needs buying.

No LLM dependency, by design — `scheduler/` imports this, and design
principle 3 says the scheduler keeps working when the agent does not.

Unlike `receipts.confirm`, every write here goes through the mutations helper.
These are voice actions, and voice is exactly what /undo exists for: you will
mis-hear "we're out of milk" for something else eventually.
"""

import sqlite3
from datetime import date

from app import mutations, timeutil


def _today() -> str:
    """Seam for tests. Local date, because 'expires tomorrow' is a wall-clock
    question — nobody thinks about their spinach in UTC."""
    from app import config

    return timeutil.now(config.DEFAULT_TZ).date().isoformat()


def _days_left(expires_on: str | None, today: str) -> int | None:
    if not expires_on:
        return None
    try:
        return (date.fromisoformat(expires_on) - date.fromisoformat(today)).days
    except (TypeError, ValueError):
        return None


def active(conn: sqlite3.Connection, location: str | None = None) -> list[dict]:
    """Everything currently in the house, soonest to expire first.

    Undated items sort last rather than first: a jar of salt is never the
    answer to "what should I use up?".
    """
    sql = """SELECT * FROM pantry_items
               WHERE status = 'active'"""
    args: tuple = ()
    if location:
        sql += " AND location = ?"
        args = (location,)
    sql += " ORDER BY expires_on IS NULL, expires_on, name"

    today = _today()
    return [
        {**dict(row), "days_left": _days_left(row["expires_on"], today)}
        for row in conn.execute(sql, args).fetchall()
    ]


def find(conn: sqlite3.Connection, name: str) -> dict | None:
    """The active item the user means by `name`.

    Substring, case-insensitive, tie-broken by soonest expiry: with two
    cartons open, "we're out of milk" means the one you were drinking.
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None
    row = conn.execute(
        """SELECT * FROM pantry_items
             WHERE status = 'active'
               AND (lower(name) LIKE '%' || ? || '%'
                    OR lower(COALESCE(raw_text,'')) LIKE '%' || ? || '%')
             ORDER BY expires_on IS NULL, expires_on
             LIMIT 1""",
        (needle, needle),
    ).fetchone()
    return dict(row) if row else None


def consume(
    conn: sqlite3.Connection, utterance_id: int | None, item: dict, partial: bool
) -> None:
    """Mark an item used.

    Two outcomes only. All of it: the item is consumed and lands on the
    shopping list. Some of it: the item stays active and **nothing** is
    listed, because "used half the chicken" is not "buy more chicken".

    There is no fractional model beyond that. Tracking 0.4 of a chicken is
    precision this system cannot honestly maintain, and the only decision that
    would depend on it is one you make at the fridge door anyway.
    """
    if partial:
        return

    mutations.update(
        conn,
        utterance_id,
        "pantry_items",
        item["id"],
        {
            "status": "consumed",
            "consumed_at": timeutil.to_utc_iso(timeutil.now("UTC")),
        },
    )
    add_to_list(conn, utterance_id, item["name"], "out", source_item_id=item["id"])


def add_item(
    conn: sqlite3.Connection,
    utterance_id: int | None,
    name: str,
    *,
    category: str | None = None,
    location: str | None = None,
    expires_on: str | None = None,
    quantity: float | None = None,
    unit: str | None = None,
) -> int:
    """Put something in the house immediately, no review step.

    The review screen exists because a *model* read a receipt and might have
    read it wrong. Here the user is the source — this is the same act as
    confirming, so making them confirm their own sentence would be ceremony.

    Logged through the mutations helper like every other voice action, so it
    undoes normally.
    """
    return mutations.insert(
        conn,
        utterance_id,
        "pantry_items",
        {
            "name": name,
            "category": category,
            "location": location or "pantry",
            "expires_on": expires_on,
            "expiry_source": "default" if expires_on else None,
            "quantity": quantity,
            "unit": unit,
            "status": "active",
        },
    )


def add_to_list(
    conn: sqlite3.Connection,
    utterance_id: int | None,
    name: str,
    reason: str,
    source_item_id: int | None = None,
) -> int | None:
    """Add to the shopping list, or do nothing if it is already open.

    Returns None when it was already there. `idx_shopping_open` would raise
    otherwise, and saying "we're out of milk" twice is not an error worth
    surfacing — it is a person being human about groceries.
    """
    name = (name or "").strip()
    if not name:
        return None
    existing = conn.execute(
        "SELECT id FROM shopping_list WHERE lower(name) = lower(?) AND status = 'open'",
        (name,),
    ).fetchone()
    if existing:
        return None
    return mutations.insert(
        conn,
        utterance_id,
        "shopping_list",
        {"name": name, "reason": reason, "source_item_id": source_item_id},
    )


def open_list(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM shopping_list WHERE status = 'open' ORDER BY created_at, id"
        ).fetchall()
    ]


def resolve_list_entry(
    conn: sqlite3.Connection, utterance_id: int | None, entry_id: int, status: str
) -> bool:
    """Close a list entry. `status` is 'purchased' or 'removed'."""
    if status not in ("purchased", "removed"):
        raise ValueError(f"not a resolution: {status!r}")
    return mutations.update(
        conn,
        utterance_id,
        "shopping_list",
        entry_id,
        {"status": status, "resolved_at": timeutil.to_utc_iso(timeutil.now("UTC"))},
    )
