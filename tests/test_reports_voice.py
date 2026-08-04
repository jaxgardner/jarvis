"""Reports the assistant can name and talk about.

The router sees the last ten in its system prompt, so "answer the vendor one"
and "what did it say about pricing" both resolve to an id rather than a guess.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "reports.db"
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
    """A finished report with a session to resume, unless told otherwise."""
    fields = {
        "prompt": "Compare the three vendors",
        "status": "done",
        "result": "Vendor B is cheapest at $4,200/yr.",
        "summary": "Compared three vendors; B is cheapest at $4,200/yr.",
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


def test_a_report_can_carry_a_summary(db):
    """The summary is what voice reads. `result` runs to tens of kilobytes —
    a vendor comparison with three tables would dominate the context window
    and the latency budget for a question one sentence answers."""
    job_id = make_job(db)
    assert (
        rows(db, "SELECT summary FROM jobs WHERE id = ?", (job_id,))[0]["summary"]
        == "Compared three vendors; B is cheapest at $4,200/yr."
    )
