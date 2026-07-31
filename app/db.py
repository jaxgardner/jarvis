"""SQLite access.

Two invariants every connection must hold:
  - foreign_keys is per-connection, not stored in the file, so it must be set
    on every connect (journal_mode=WAL, by contrast, is persistent).
  - row_factory gives dict-like rows so handlers can json-serialize directly.
"""

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from app.config import DB_PATH


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """One unit of work. The domain write and its `mutations` row go in here
    together — that pairing is what makes /undo trustworthy."""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
