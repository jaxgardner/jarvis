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


# ── the helper ────────────────────────────────────────────


def test_replying_requeues_the_job_in_place(db):
    from app import handlers
    from app.db import transaction

    job_id = make_job(db)
    with transaction() as conn:
        assert handlers.reply_to_job(conn, job_id, "Go with B") == "ok"

    job = rows(db, "SELECT * FROM jobs WHERE id = ?", (job_id,))[0]
    assert job["status"] == "queued"
    assert job["pending_input"] == "Go with B"
    assert job["session_id"] == "sess-abc"
    assert rows(db, "SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 1


def test_replying_preserves_the_original_ask(db):
    """`prompt` is what the detail view shows under "Asked". Overwriting it
    with the reply would leave no record of what the report is for."""
    from app import handlers
    from app.db import transaction

    job_id = make_job(db)
    with transaction() as conn:
        handlers.reply_to_job(conn, job_id, "Go with B")

    assert (
        rows(db, "SELECT prompt FROM jobs WHERE id = ?", (job_id,))[0]["prompt"]
        == "Compare the three vendors"
    )


def test_replying_resets_the_attempt_count(db):
    """MAX_ATTEMPTS is 2 and counts across the life of the row. A job that
    already failed once and recovered sits at 2 — without the reset your
    reply gets no retries at all, and one transient failure buries it."""
    from app import handlers
    from app.db import transaction

    job_id = make_job(db, attempts=2, error="exit 1: transient")
    with transaction() as conn:
        handlers.reply_to_job(conn, job_id, "Go with B")

    job = rows(db, "SELECT attempts, error FROM jobs WHERE id = ?", (job_id,))[0]
    assert job["attempts"] == 0
    assert job["error"] is None


def test_replying_keeps_the_old_report_readable_until_it_is_replaced(db):
    """You reply, then keep reading. Blanking `result` would empty the screen
    for the minutes the rerun takes, for no gain — the worker overwrites it
    on finish anyway."""
    from app import handlers
    from app.db import transaction

    job_id = make_job(db)
    with transaction() as conn:
        handlers.reply_to_job(conn, job_id, "Go with B")

    job = rows(db, "SELECT result, finished_at FROM jobs WHERE id = ?", (job_id,))[0]
    assert job["result"] == "Vendor B looks best. Which do you want?"
    assert job["finished_at"] is None


def test_replying_to_a_live_job_is_refused(db):
    """You cannot resume a session mid-run, and a reply that vanished into a
    job already working is the worst possible failure for this feature."""
    from app import handlers
    from app.db import transaction

    for status in ("queued", "running"):
        job_id = make_job(db, status=status)
        with transaction() as conn:
            assert handlers.reply_to_job(conn, job_id, "Go with B") == "live"
        assert (
            rows(db, "SELECT pending_input FROM jobs WHERE id = ?", (job_id,))[0][
                "pending_input"
            ]
            is None
        )


def test_replying_to_a_failed_job_is_allowed(db):
    """A run that failed is over, and answering it is a reasonable way to
    steer the retry."""
    from app import handlers
    from app.db import transaction

    job_id = make_job(db, status="failed", result=None, error="timed out")
    with transaction() as conn:
        assert handlers.reply_to_job(conn, job_id, "Skip the third vendor") == "ok"


def test_replying_to_a_job_with_no_session_is_allowed(db):
    """A job that failed before its subprocess started has no session. The
    reply simply starts a fresh one with the wrapped text, which is a
    reasonable reading of what you meant — and refusing would leave a dead
    report with no way forward."""
    from app import handlers
    from app.db import transaction

    job_id = make_job(
        db, status="failed", session_id=None, error="claude CLI not found"
    )
    with transaction() as conn:
        assert handlers.reply_to_job(conn, job_id, "try again") == "ok"


def test_replying_to_a_job_that_does_not_exist(db):
    from app import handlers
    from app.db import transaction

    with transaction() as conn:
        assert handlers.reply_to_job(conn, 999, "Go with B") == "missing"


def test_resume_latest_picks_the_most_recent_finished_job(db):
    from app import handlers
    from app.db import transaction

    make_job(db, prompt="older", session_id="sess-old")
    newest = make_job(db, prompt="newer", session_id="sess-new")

    with transaction() as conn:
        assert handlers.resume_latest_job(conn, "what about the second one") == newest

    assert (
        rows(db, "SELECT pending_input FROM jobs WHERE id = ?", (newest,))[0][
            "pending_input"
        ]
        == "what about the second one"
    )


def test_resume_latest_skips_a_job_with_no_session(db):
    """Resuming is the whole point of a follow-up; a job with no session id
    would start cold with no idea what "the second one" was."""
    from app import handlers
    from app.db import transaction

    make_job(db, session_id=None)

    with transaction() as conn:
        assert handlers.resume_latest_job(conn, "go deeper") is None


def test_resume_latest_returns_none_when_there_is_nothing_to_resume(db):
    from app import handlers
    from app.db import transaction

    with transaction() as conn:
        assert handlers.resume_latest_job(conn, "go deeper") is None


# ── the endpoint ──────────────────────────────────────────


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {SHARED}"})
    return c


def test_reply_endpoint_requeues_and_returns_the_job(client, db):
    job_id = make_job(db)

    response = client.post(f"/jobs/{job_id}/reply", json={"text": "Go with B"})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == job_id
    assert body["status"] == "queued"
    assert body["pending_input"] == "Go with B"
    assert body["prompt"] == "Compare the three vendors"


def test_reply_endpoint_409s_on_a_live_job(client, db):
    job_id = make_job(db, status="running")
    assert (
        client.post(f"/jobs/{job_id}/reply", json={"text": "Go with B"}).status_code
        == 409
    )


def test_reply_endpoint_404s_on_an_unknown_job(client):
    assert client.post("/jobs/999/reply", json={"text": "Go with B"}).status_code == 404


def test_reply_endpoint_rejects_an_empty_reply(client, db):
    job_id = make_job(db)
    assert client.post(f"/jobs/{job_id}/reply", json={"text": "  "}).status_code == 422


def test_reply_endpoint_requires_a_token(db):
    from fastapi.testclient import TestClient

    from app.main import app

    job_id = make_job(db)
    anonymous = TestClient(app)
    assert anonymous.post(f"/jobs/{job_id}/reply", json={"text": "x"}).status_code == 401
