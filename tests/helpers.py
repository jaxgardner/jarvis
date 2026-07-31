"""Shared test helpers."""

import sqlite3
from pathlib import Path

from app.config import REPO_ROOT


def apply_migrations(path: Path) -> None:
    """Build a database from every migration, in order.

    Globbed rather than listed: a hardcoded list keeps passing after a new
    migration lands while silently testing the old schema, which is the
    failure you notice in production instead of here.
    """
    conn = sqlite3.connect(path)
    try:
        for sql_file in sorted((REPO_ROOT / "migrations").glob("*.sql")):
            conn.executescript(
                "\n".join(
                    line
                    for line in sql_file.read_text().splitlines()
                    if not line.strip().upper().startswith("PRAGMA")
                )
            )
        conn.commit()
    finally:
        conn.close()
