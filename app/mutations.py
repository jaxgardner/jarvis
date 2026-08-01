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


def undo_last(conn: sqlite3.Connection) -> dict | None:
    """Reverse the most recent mutation that hasn't been undone.

    The undo itself is deliberately NOT logged as a new mutation — it stamps
    undone_at instead. Logging it would make a second /undo re-apply the change,
    which is not what anyone means by "undo that".
    """
    row = conn.execute(
        """SELECT * FROM mutations
             WHERE undone_at IS NULL
             ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return None

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

    conn.execute(
        "UPDATE mutations SET undone_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " WHERE id = ?",
        (row["id"],),
    )
    return {"table": table, "row_id": row_id, "op": op, "before": before}
