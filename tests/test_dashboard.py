"""The reads the dashboard is built on — offline.

`/activity` is the one that carries a real invariant: only the mutation /undo
would actually reverse may be marked undoable.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "dash.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {SHARED}"
    return c


def capture(db, text: str, intent: str = "add_note") -> int:
    """One utterance that inserted one note, through the real mutations helper."""
    from app.db import transaction
    from app import mutations

    with transaction() as conn:
        utterance_id = int(
            conn.execute(
                """INSERT INTO utterances (raw_text, response_text, route, intent,
                                           latency_ms, input_tokens, output_tokens, model_calls)
                     VALUES (?,?,'fast',?,?,?,?,?)""",
                (text, "Noted.", intent, 500, 2557, 90, 1),
            ).lastrowid
        )
        mutations.insert(conn, utterance_id, "notes", {"body": text})
    return utterance_id


# ── /activity ─────────────────────────────────────────────


def test_activity_pairs_utterances_with_what_they_changed(client, db):
    utterance_id = capture(db, "milk")
    body = client.get("/activity").json()

    entry = next(u for u in body["utterances"] if u["id"] == utterance_id)
    assert entry["raw_text"] == "milk"
    assert entry["input_tokens"] == 2557
    assert len(entry["mutations"]) == 1
    assert entry["mutations"][0]["table"] == "notes"
    assert entry["mutations"][0]["op"] == "insert"


def test_only_the_newest_mutation_is_undoable(client, db):
    """/undo reverses the most recent non-undone mutation and nothing else.
    Marking an older row undoable would offer a swipe that silently reverses
    something the user wasn't looking at."""
    capture(db, "first")
    capture(db, "second")

    flags = [
        (u["raw_text"], m["undoable"])
        for u in client.get("/activity").json()["utterances"]
        for m in u["mutations"]
    ]
    assert sorted(flags) == [("first", False), ("second", True)]


def test_undo_moves_the_undoable_flag_back(client, db):
    """After undoing the newest, the one before it becomes the next target."""
    capture(db, "first")
    capture(db, "second")
    assert client.post("/undo").json()["undone"] is True

    entries = {
        u["raw_text"]: u["mutations"][0] for u in client.get("/activity").json()["utterances"]
    }
    assert entries["second"]["undone_at"] is not None
    assert entries["second"]["undoable"] is False
    assert entries["first"]["undoable"] is True


def test_activity_is_newest_first(client, db):
    capture(db, "older")
    capture(db, "newer")
    texts = [u["raw_text"] for u in client.get("/activity").json()["utterances"]]
    assert texts[:2] == ["newer", "older"]


def test_activity_with_nothing_stored_is_an_empty_list(client):
    assert client.get("/activity").json() == {"utterances": []}


def test_an_utterance_that_changed_nothing_still_appears(client, db):
    """A question mutates nothing, but it is still the bulk of what you said
    and belongs in the history."""
    from app.db import transaction

    with transaction() as conn:
        conn.execute(
            """INSERT INTO utterances (raw_text, response_text, route, intent, latency_ms)
                 VALUES ('what is on tomorrow','Clear.','fast','query',480)"""
        )
    entry = client.get("/activity").json()["utterances"][0]
    assert entry["intent"] == "query" and entry["mutations"] == []


def test_activity_limit_is_bounded(client, db):
    for n in range(5):
        capture(db, f"note {n}")
    assert len(client.get("/activity?limit=2").json()["utterances"]) == 2
    # Absurd values clamp instead of erroring — a dashboard bug shouldn't be
    # able to ask for the entire table.
    assert client.get("/activity?limit=99999").status_code == 200


# ── /jobs ─────────────────────────────────────────────────


def add_job(db, prompt: str, status: str = "done", result: str | None = None) -> int:
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "INSERT INTO jobs (prompt, status, result) VALUES (?,?,?)",
            (prompt, status, result),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_jobs_lists_newest_first(client, db):
    add_job(db, "older")
    add_job(db, "newer")
    assert [j["prompt"] for j in client.get("/jobs").json()["jobs"]] == ["newer", "older"]


def test_job_results_are_truncated_in_the_list(client, db):
    add_job(db, "long one", result="x" * 1000)
    job = client.get("/jobs").json()["jobs"][0]
    assert len(job["result_preview"]) == 280
    assert job["result_truncated"] == 1


def test_a_short_result_is_not_marked_truncated(client, db):
    add_job(db, "short one", result="done")
    job = client.get("/jobs").json()["jobs"][0]
    assert job["result_preview"] == "done" and job["result_truncated"] == 0


def test_the_full_result_is_still_on_the_detail_endpoint(client, db):
    job_id = add_job(db, "long one", result="x" * 1000)
    assert len(client.get(f"/jobs/{job_id}").json()["result"]) == 1000


def test_dashboard_reads_require_auth(client):
    client.headers.pop("Authorization")
    assert client.get("/activity").status_code == 401
    assert client.get("/jobs").status_code == 401


# ── pantry health ─────────────────────────────────────────


def test_health_reports_a_stuck_extraction(client, db):
    """A receipt frozen in 'extracting' means the background task died. That
    is the quiet failure — the user sees a spinner and assumes it is slow."""
    from app.db import transaction

    with transaction() as conn:
        conn.execute(
            """INSERT INTO receipts (image_sha256, status, created_at)
                 VALUES ('a', 'extracting',
                         strftime('%Y-%m-%dT%H:%M:%SZ','now','-10 minutes'))"""
        )

    pantry = client.get("/health").json()["pantry"]
    assert pantry["stuck_receipts"] == 1


def test_a_recent_extraction_is_not_yet_stuck(client, db):
    from app.db import transaction

    with transaction() as conn:
        conn.execute(
            "INSERT INTO receipts (image_sha256, status) VALUES ('a', 'extracting')"
        )

    assert client.get("/health").json()["pantry"]["stuck_receipts"] == 0


def test_health_counts_unreviewed_receipts_and_overdue_food(client, db):
    from app.db import transaction

    with transaction() as conn:
        conn.execute(
            "INSERT INTO receipts (image_sha256, status) VALUES ('b', 'pending')"
        )
        conn.execute(
            """INSERT INTO pantry_items (name, expires_on, status)
                 VALUES ('old milk', date('now','-3 days'), 'active')"""
        )

    pantry = client.get("/health").json()["pantry"]
    assert pantry["pending_receipts"] == 1
    assert pantry["overdue_items"] == 1


def test_health_still_answers_with_an_empty_pantry(client, db):
    """/health is liveness for launchd. It must never depend on there being
    data."""
    pantry = client.get("/health").json()["pantry"]
    assert pantry == {"stuck_receipts": 0, "pending_receipts": 0, "overdue_items": 0}
