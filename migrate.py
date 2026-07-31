#!/usr/bin/env python
"""Migration runner.

Plain numbered .sql files applied in order, recorded in schema_migrations.
Deliberately not Alembic — there is one developer and one database.

    uv run migrate.py          apply pending
    uv run migrate.py --status show what's applied
"""

import re
import sys

from app.config import DB_PATH, REPO_ROOT
from app.db import connect

MIGRATIONS_DIR = REPO_ROOT / "migrations"

_PRAGMA_LINE = re.compile(r"^\s*PRAGMA\b[^;]*;", re.IGNORECASE | re.MULTILINE)


def _split_pragmas(sql: str) -> tuple[list[str], str]:
    """Separate top-level PRAGMA statements from the rest of the migration."""
    pragmas = [m.group(0).strip() for m in _PRAGMA_LINE.finditer(sql)]
    return pragmas, _PRAGMA_LINE.sub("", sql)


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          filename   TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
        """
    )


def _applied(conn) -> set[str]:
    return {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}


def migrate() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        _ensure_table(conn)
        done = _applied(conn)
        pending = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.name not in done)

        if not pending:
            print(f"up to date ({len(done)} applied) — {DB_PATH}")
            return 0

        for path in pending:
            pragmas, body = _split_pragmas(path.read_text())

            # PRAGMAs must run outside a transaction (journal_mode=WAL errors
            # inside one) and are persistent or idempotent, so they run first
            # and are not rolled back.
            for pragma in pragmas:
                conn.execute(pragma)

            # BEGIN/COMMIT go *inside* the script text: executescript() issues
            # an implicit commit for any transaction open when it starts, so a
            # transaction opened out here would be closed under us.
            script = (
                "BEGIN;\n"
                f"{body}\n"
                "INSERT INTO schema_migrations(filename) VALUES "
                f"('{path.name}');\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except Exception as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                print(f"FAILED {path.name}: {exc}", file=sys.stderr)
                return 1
            print(f"applied {path.name}")

        print(f"done — {DB_PATH}")
        return 0
    finally:
        conn.close()


def status() -> int:
    conn = connect()
    try:
        _ensure_table(conn)
        done = _applied(conn)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            print(f"{'[x]' if path.name in done else '[ ]'} {path.name}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(status() if "--status" in sys.argv else migrate())
