"""The review queue — the only path from email extraction to the agenda.

The invariant under test throughout: extraction proposes, a human disposes.
Acceptance is a user action and therefore goes through the mutations helper,
which is what makes it undoable on the same terms as anything spoken.
"""

import json
import sqlite3

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "proposals.db"
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


def rows(db, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def propose(db, **overrides) -> int:
    payload = {
        "title": "Flight UA 412",
        "starts_at": "2026-08-10T13:30:00Z",
        "location": "DEN",
        **overrides.pop("payload", {}),
    }
    fields = {
        "source": "gmail",
        "external_id": "m1",
        "kind": "event",
        "summary": "Flight UA 412 — Monday at 7:30 AM",
        "confidence": 0.9,
        **overrides,
    }
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            """INSERT INTO proposals
                 (source, external_id, kind, payload_json, summary, confidence)
               VALUES (?,?,?,?,?,?)""",
            (
                fields["source"],
                fields["external_id"],
                fields["kind"],
                json.dumps(payload),
                fields["summary"],
                fields["confidence"],
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# ── listing ───────────────────────────────────────────────


def test_pending_proposals_are_listed(client, db):
    propose(db)
    body = client.get("/proposals").json()
    assert len(body["proposals"]) == 1
    assert body["proposals"][0]["summary"].startswith("Flight UA 412")


def test_decided_proposals_leave_the_queue(client, db):
    proposal_id = propose(db)
    client.post(f"/proposals/{proposal_id}/reject")
    assert client.get("/proposals").json()["proposals"] == []


def test_proposals_require_a_token(db):
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).get("/proposals").status_code == 401


# ── accepting ─────────────────────────────────────────────


def test_accept_creates_the_event(client, db):
    proposal_id = propose(db)
    reply = client.post(f"/proposals/{proposal_id}/accept").json()["reply"]
    assert "Flight UA 412" in reply

    events = rows(db, "SELECT * FROM events")
    assert len(events) == 1
    assert events[0]["starts_at"] == "2026-08-10T13:30:00Z"
    assert events[0]["location"] == "DEN"


def test_an_accepted_event_is_sourced_email_not_calendar(client, db):
    """Labelling it 'calendar' would make the calendar ingester's dedupe treat
    it as a row it owns and is entitled to overwrite on the next sync."""
    client.post(f"/proposals/{propose(db)}/accept")
    assert rows(db, "SELECT source FROM events")[0]["source"] == "email"


def test_accept_is_logged_and_undoable(client, db):
    """The one ingestion write that DOES go through the mutations helper. Not
    because of where the data came from — because a human pressed Accept, and
    user actions are what /undo exists to reverse."""
    client.post(f"/proposals/{propose(db)}/accept")

    logged = rows(db, "SELECT * FROM mutations")
    assert len(logged) == 1
    assert logged[0]["table_name"] == "events" and logged[0]["op"] == "insert"

    assert client.post("/undo").json()["undone"] is True
    assert rows(db, "SELECT * FROM events") == []


def test_undoing_an_accept_does_not_trip_the_foreign_key(client, db):
    """proposals.event_id points at the row /undo hard-deletes. Declared
    RESTRICT (the default), the delete fails outright — so the one ingestion
    write deliberately made undoable would have been the only one that could
    not be undone. Migration 007 makes it ON DELETE SET NULL."""
    proposal_id = propose(db)
    client.post(f"/proposals/{proposal_id}/accept")
    client.post("/undo")

    row = rows(db, "SELECT status, event_id FROM proposals")[0]
    assert row["event_id"] is None
    # Still 'accepted', deliberately: re-offering something you just removed
    # is a loop, not a feature.
    assert row["status"] == "accepted"


def test_accept_records_the_event_it_created(client, db):
    proposal_id = propose(db)
    client.post(f"/proposals/{proposal_id}/accept")
    row = rows(db, "SELECT status, event_id, decided_at FROM proposals")[0]
    assert row["status"] == "accepted"
    assert row["event_id"] is not None
    assert row["decided_at"] is not None


def test_accepting_twice_does_not_create_two_events(client, db):
    """A double-tap on a phone is not a request for two calendar entries."""
    proposal_id = propose(db)
    client.post(f"/proposals/{proposal_id}/accept")
    second = client.post(f"/proposals/{proposal_id}/accept").json()
    assert "already" in second["reply"].lower()
    assert len(rows(db, "SELECT * FROM events")) == 1


def test_accepting_an_unknown_proposal_is_a_404(client):
    assert client.post("/proposals/9999/accept").status_code == 404


def test_a_proposal_with_no_usable_time_answers_rather_than_500s(client, db):
    """The extractor drops these before they become proposals, so this is a
    hand-written row or a schema change. Either way the caller is a button on a
    phone and deserves a sentence, not a stack trace."""
    proposal_id = propose(db, payload={"starts_at": None})
    body = client.post(f"/proposals/{proposal_id}/accept").json()
    assert "can't put it on the calendar" in body["reply"]
    assert rows(db, "SELECT * FROM events") == []


def test_an_all_day_proposal_keeps_its_flag(client, db):
    proposal_id = propose(db, payload={"all_day": True})
    client.post(f"/proposals/{proposal_id}/accept")
    assert rows(db, "SELECT all_day FROM events")[0]["all_day"] == 1


# ── rejecting ─────────────────────────────────────────────


def test_reject_writes_nothing_to_events(client, db):
    client.post(f"/proposals/{propose(db)}/reject")
    assert rows(db, "SELECT * FROM events") == []
    assert rows(db, "SELECT * FROM mutations") == []


def test_reject_is_recorded_so_it_is_never_re_proposed(client, db):
    """ingest.gmail.candidates() skips any message with a proposals row of any
    status, which is what makes this stick."""
    proposal_id = propose(db)
    client.post(f"/proposals/{proposal_id}/reject")
    row = rows(db, "SELECT status, decided_at FROM proposals")[0]
    assert row["status"] == "rejected" and row["decided_at"] is not None


def test_rejecting_a_decided_proposal_says_so(client, db):
    proposal_id = propose(db)
    client.post(f"/proposals/{proposal_id}/accept")
    assert "already" in client.post(f"/proposals/{proposal_id}/reject").json()[
        "reply"
    ].lower()


# ── health ────────────────────────────────────────────────


def test_health_reports_no_ingestion_before_the_first_sync(client):
    assert client.get("/health").json()["ingest"]["sources"] == []


def test_health_flags_a_stale_source(client, db):
    """The failure this phase is built against is the silent one — the agenda
    just quietly goes stale."""
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO sync_state (source, last_run_at, last_ok_at)
             VALUES ('calendar:me@x','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')"""
    )
    conn.commit()
    conn.close()

    ingest = client.get("/health").json()["ingest"]
    assert ingest["stale"] == ["calendar:me@x"]


def test_health_does_not_flag_a_fresh_source(client, db):
    from app import timeutil

    now = timeutil.to_utc_iso(timeutil.now("UTC"))
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO sync_state (source, last_run_at, last_ok_at) VALUES (?,?,?)",
        ("gmail", now, now),
    )
    conn.commit()
    conn.close()

    assert client.get("/health").json()["ingest"]["stale"] == []


def test_a_source_that_has_never_succeeded_is_stale(client, db):
    """last_run_at without last_ok_at means it is running and failing — a
    different problem from not running at all, and both need to show."""
    from app import timeutil

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO sync_state (source, last_run_at) VALUES ('gmail', ?)",
        (timeutil.to_utc_iso(timeutil.now("UTC")),),
    )
    conn.commit()
    conn.close()

    assert client.get("/health").json()["ingest"]["stale"] == ["gmail"]
