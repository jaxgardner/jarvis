"""Every write goes through here.

The whole point: the domain write and its `mutations` row land in the same
transaction, so the log can never disagree with the data. That is what makes
/undo trustworthy — and voice input being lossy, /undo is load-bearing.

Do not INSERT/UPDATE a domain table anywhere else.
"""

import json
import sqlite3
from typing import Any

# Whitelist — table names are interpolated into SQL below, so they must never
# come from anywhere but this module's callers.
WRITABLE = {
    "events",
    "reminders",
    "people",
    "projects",
    "notes",
    "receipts",
    "pantry_items",
    "shopping_list",
}
SOFT_DELETE = {"events", "notes"}  # have a deleted_at column


def _check(table: str) -> None:
    if table not in WRITABLE:
        raise ValueError(f"{table!r} is not a writable domain table")


def _row(conn: sqlite3.Connection, table: str, row_id: int) -> dict | None:
    cur = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))  # noqa: S608
    found = cur.fetchone()
    return dict(found) if found else None


def _log(
    conn: sqlite3.Connection,
    utterance_id: int | None,
    table: str,
    row_id: int,
    op: str,
    before: dict | None,
    after: dict | None,
) -> None:
    conn.execute(
        """INSERT INTO mutations
             (utterance_id, table_name, row_id, op, before_json, after_json)
           VALUES (?,?,?,?,?,?)""",
        (
            utterance_id,
            table,
            row_id,
            op,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
        ),
    )


def insert(
    conn: sqlite3.Connection,
    utterance_id: int | None,
    table: str,
    values: dict[str, Any],
) -> int:
    _check(table)
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks})",  # noqa: S608
        tuple(values.values()),
    )
    row_id = int(cur.lastrowid)
    _log(conn, utterance_id, table, row_id, "insert", None, _row(conn, table, row_id))
    return row_id


def update(
    conn: sqlite3.Connection,
    utterance_id: int | None,
    table: str,
    row_id: int,
    values: dict[str, Any],
) -> bool:
    _check(table)
    before = _row(conn, table, row_id)
    if before is None:
        return False
    assignments = ", ".join(f"{col} = ?" for col in values)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE id = ?",  # noqa: S608
        (*values.values(), row_id),
    )
    _log(conn, utterance_id, table, row_id, "update", before, _row(conn, table, row_id))
    return True


def soft_delete(
    conn: sqlite3.Connection, utterance_id: int | None, table: str, row_id: int
) -> bool:
    """Mark deleted rather than removing. Recorded as op='delete' so /undo
    reverses it by clearing deleted_at."""
    _check(table)
    if table not in SOFT_DELETE:
        raise ValueError(f"{table!r} has no deleted_at column")
    before = _row(conn, table, row_id)
    if before is None or before.get("deleted_at"):
        return False
    conn.execute(
        f"UPDATE {table} SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"  # noqa: S608
        " WHERE id = ?",
        (row_id,),
    )
    _log(conn, utterance_id, table, row_id, "delete", before, None)
    return True


# ── undo ──────────────────────────────────────────────────


def _reverse(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Undo one logged mutation. Does not stamp undone_at — the caller does."""
    table, row_id, op = row["table_name"], row["row_id"], row["op"]
    before = json.loads(row["before_json"]) if row["before_json"] else None

    if op == "insert":
        # Hard-delete: the row only ever existed because of the utterance
        # we're reversing.
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))  # noqa: S608
    elif op == "update":
        if before:
            cols = [c for c in before if c != "id"]
            assignments = ", ".join(f"{c} = ?" for c in cols)
            conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",  # noqa: S608
                (*[before[c] for c in cols], row_id),
            )
    elif op == "delete":
        conn.execute(
            f"UPDATE {table} SET deleted_at = NULL WHERE id = ?", (row_id,)  # noqa: S608
        )


def undo_last(conn: sqlite3.Connection) -> dict | None:
    """Reverse the most recent utterance's writes.

    One utterance can write more than one row — `add_event` with a new person
    inserts into `people` and `events`, and consuming an item both updates it
    and adds to the shopping list. Reversing a single row would leave the
    other half standing, which is not what anyone means by "undo that".

    Reversed newest-first, which is also the correct order for foreign keys:
    the referencing row was written after the row it references.

    The undo itself is deliberately NOT logged as a new mutation — it stamps
    undone_at instead. Logging it would make a second /undo re-apply the
    change.
    """
    newest = conn.execute(
        """SELECT * FROM mutations
             WHERE undone_at IS NULL
             ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if newest is None:
        return None

    if newest["utterance_id"] is None:
        # NULL is "no utterance behind this", not a group key. Grouping on it
        # would sweep up every unattributed mutation ever made.
        group = [newest]
    else:
        group = conn.execute(
            """SELECT * FROM mutations
                 WHERE utterance_id = ? AND undone_at IS NULL
                 ORDER BY id DESC""",
            (newest["utterance_id"],),
        ).fetchall()

    for row in group:
        _reverse(conn, row)

    # %-formatting is not an option here: the SQL itself contains %Y/%m/%d.
    marks = ",".join("?" for _ in group)
    conn.execute(
        "UPDATE mutations SET undone_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        f" WHERE id IN ({marks})",
        tuple(row["id"] for row in group),
    )

    before = json.loads(newest["before_json"]) if newest["before_json"] else None
    return {
        "table": newest["table_name"],
        "row_id": newest["row_id"],
        "op": newest["op"],
        "before": before,
    }
