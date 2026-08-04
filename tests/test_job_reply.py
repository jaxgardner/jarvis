"""Replying to a report that asked you something.

A reply re-queues the job it belongs to rather than starting a new one, so
there is one report per task no matter how many times you answer it.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "reply.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


def rows(db, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def make_job(db, **overrides) -> int:
    """A finished job with a session to resume, unless told otherwise."""
    fields = {
        "prompt": "Compare the three vendors",
        "status": "done",
        "result": "Vendor B looks best. Which do you want?",
        "session_id": "sess-abc",
        "attempts": 1,
    }
    fields.update(overrides)
    conn = sqlite3.connect(db)
    try:
        columns = ",".join(fields)
        marks = ",".join("?" * len(fields))
        cur = conn.execute(
            f"INSERT INTO jobs ({columns}) VALUES ({marks})", tuple(fields.values())
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_jobs_have_somewhere_to_put_a_reply(db):
    """pending_input holds the reply between sending it and the resumed run
    finishing. It cannot be folded into `prompt`: the worker passes `prompt`
    as -p, so reusing it would destroy the original ask the detail view
    shows under "Asked"."""
    job_id = make_job(db)
    assert rows(db, "SELECT pending_input FROM jobs WHERE id = ?", (job_id,)) == [
        {"pending_input": None}
    ]
