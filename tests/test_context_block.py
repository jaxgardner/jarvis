"""Pre-retrieval: search before the router call, not after it.

query costs 2675ms against 1410ms for one call, and the whole difference is
that it searches *after* the model has decided to search. The search itself
is ~3ms. So we do it first and hand the result in.
"""

import sqlite3

import pytest

from app import handlers
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    """Every migration, so email_fts exists — the `conn` fixture in
    test_core.py applies 001_init.sql alone and search_email would fall
    through its OperationalError guard and silently return []."""
    path = tmp_path / "context.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def _note(conn, body):
    conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
    conn.commit()


def test_matching_note_appears(conn):
    _note(conn, "the back garden fence needs a new post on the left side")
    block = handlers.context_block(conn, "what did I say about the fence")
    assert "NOTE:" in block
    assert "fence" in block


def test_no_match_is_empty_string(conn):
    """Empty, not a bare heading. A CONTEXT: label with nothing under it
    invites the model to answer from a block that contains nothing."""
    _note(conn, "buy milk")
    block = handlers.context_block(conn, "what did I say about kubernetes")
    assert block == ""


def test_soft_deleted_note_is_excluded(conn):
    conn.execute("INSERT INTO notes (body) VALUES ('the fence is finished')")
    conn.execute("UPDATE notes SET deleted_at = '2026-08-04T00:00:00Z'")
    conn.commit()
    assert handlers.context_block(conn, "what about the fence") == ""


def test_limit_is_respected(conn):
    for i in range(12):
        _note(conn, f"the fence note number {i}")
    block = handlers.context_block(conn, "fence", limit=5)
    assert len([l for l in block.splitlines() if l.startswith("NOTE:")]) <= 5


def test_lines_are_single_line_each(conn):
    """A newline inside a context line would misalign the block the model
    reads, the same reason /say squeezes its reply to one line."""
    _note(conn, "fence plan:\nreplace posts\nthen paint")
    block = handlers.context_block(conn, "fence")
    assert block
    for line in block.splitlines():
        assert line.startswith(("NOTE:", "EMAIL:"))
