"""Reading and writing `sync_state`.

The table exists so that "the importer stopped working" is a *detectable*
condition rather than something noticed a week later when the agenda has
quietly gone stale — the same reasoning as `heartbeats` in migration 002.

`last_run_at` and `last_ok_at` are deliberately distinct. A run that errored
still stamps the former, so the gap between the two is what says "broken
since". A single timestamp updated only on success cannot tell "erroring every
15 minutes" apart from "not running at all", and those need different fixes.
"""

import sqlite3

from app import timeutil

# Per-calendar rows are keyed 'calendar:<calendarId>', because a syncToken is
# per-calendar and one shared cursor across several would be meaningless.
# Gmail has one mailbox and therefore one row, 'gmail'.
CALENDAR_PREFIX = "calendar:"
GMAIL = "gmail"


def _now() -> str:
    return timeutil.to_utc_iso(timeutil.now("UTC"))


def token(conn: sqlite3.Connection, source: str) -> str | None:
    row = conn.execute(
        "SELECT token FROM sync_state WHERE source = ?", (source,)
    ).fetchone()
    return row["token"] if row else None


def start(conn: sqlite3.Connection, source: str) -> None:
    """Stamp last_run_at. Called before the work, so a run that dies partway
    still leaves evidence that it began."""
    conn.execute(
        """INSERT INTO sync_state (source, last_run_at) VALUES (?,?)
             ON CONFLICT(source) DO UPDATE SET last_run_at = excluded.last_run_at""",
        (source, _now()),
    )


def succeeded(
    conn: sqlite3.Connection, source: str, new_token: str | None, detail: str
) -> None:
    conn.execute(
        """INSERT INTO sync_state (source, token, last_run_at, last_ok_at, detail)
             VALUES (?,?,?,?,?)
             ON CONFLICT(source) DO UPDATE SET
               token       = excluded.token,
               last_run_at = excluded.last_run_at,
               last_ok_at  = excluded.last_ok_at,
               detail      = excluded.detail""",
        (source, new_token, _now(), _now(), detail),
    )


def failed(conn: sqlite3.Connection, source: str, detail: str) -> None:
    """Record the failure without touching `token` or `last_ok_at`.

    Clearing the cursor on a failure would silently promote every transient
    network error into a full refetch.
    """
    conn.execute(
        """INSERT INTO sync_state (source, last_run_at, detail) VALUES (?,?,?)
             ON CONFLICT(source) DO UPDATE SET
               last_run_at = excluded.last_run_at,
               detail      = excluded.detail""",
        (source, _now(), detail[:500]),
    )


def clear_token(conn: sqlite3.Connection, source: str) -> None:
    """Drop the cursor so the next pass is a full fetch. Google expires sync
    cursors on its own schedule; this is the normal response, not a repair."""
    conn.execute("UPDATE sync_state SET token = NULL WHERE source = ?", (source,))


def all_rows(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT source, last_run_at, last_ok_at, detail FROM sync_state"
            " ORDER BY source"
        ).fetchall()
    ]
