"""The turn is the number the user feels; latency_ms is not it.

Offline for the schema and endpoint tests. There is no shared conftest
fixture in this repo — conftest.py only redirects JARVIS_DB — so each test
file builds its own client, following tests/test_utterances.py.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def migrated(tmp_path):
    """Every migration, in order. The `conn` fixture other files use applies
    001_init.sql alone and would not see column 016."""
    path = tmp_path / "turns.db"
    apply_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_columns_exist(migrated):
    cols = {r["name"] for r in migrated.execute("PRAGMA table_info(utterances)")}
    assert "turn_ms" in cols
    assert "timings" in cols


def test_columns_are_nullable(migrated):
    """A Shortcut client has no microphone and reports no turn. That is a
    client without a mic, not a missing measurement."""
    migrated.execute("INSERT INTO utterances (raw_text, client) VALUES ('hi','shortcut')")
    migrated.commit()
    row = migrated.execute(
        "SELECT turn_ms, timings FROM utterances ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["turn_ms"] is None
    assert row["timings"] is None
