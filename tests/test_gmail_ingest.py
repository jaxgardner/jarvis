"""Gmail ingestion — offline, no network, no model calls.

Two passes with different risk profiles, and the tests split the same way.
The context pass must not lose a message; the proposal pass must not invent
an appointment and must not spend without a ceiling.
"""

import json
import sqlite3

import pytest

from ingest import gmail
from ingest.client import ApiError
from tests.helpers import apply_migrations

TZ = "America/Denver"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "gmail.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


def rows(path, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def message(
    message_id: str = "m1",
    *,
    sender: str = "Dana <dana@example.com>",
    subject: str = "Lease renewal",
    snippet: str = "Attaching the renewal for your signature.",
    labels: list[str] | None = None,
    internal_date: str = "1785000000000",
) -> dict:
    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
        "snippet": snippet,
        "internalDate": internal_date,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
            ]
        },
    }


# ── field conversion ──────────────────────────────────────


def test_row_uses_internal_date_not_the_date_header():
    """internalDate is Gmail's own arrival time. The Date header is set by the
    sender and is routinely wrong or missing."""
    row = gmail.to_row(message(internal_date="1785000000000"))
    assert row["received_at"].endswith("Z")
    assert row["received_at"].startswith("2026-")


def test_unread_is_derived_from_labels():
    assert gmail.to_row(message(labels=["INBOX", "UNREAD"]))["is_unread"] == 1
    assert gmail.to_row(message(labels=["INBOX"]))["is_unread"] == 0


def test_missing_headers_do_not_raise():
    bare = {"id": "m9", "internalDate": "1785000000000", "payload": {}}
    row = gmail.to_row(bare)
    assert row["subject"] is None and row["sender"] is None


def test_preheader_padding_is_stripped_from_the_snippet():
    """Marketing mail pads its preheader with zero-width characters so the
    inbox preview shows nothing after the hook. Gmail returns them inside
    `snippet`, and they are invisible but *not* cheap: each is its own
    codepoint and tokenizes to two or three tokens. Measured on one real
    morning, 611 of them were 1248 tokens — 66% of the whole context handed
    to the answering model, for nothing a reader or a model can see."""
    padded = "What to watch this week" + "͏ ‌ ﻿" * 40
    row = gmail.to_row(message(snippet=padded))

    assert row["snippet"] == "What to watch this week"


def test_a_snippet_of_nothing_but_padding_is_dropped():
    row = gmail.to_row(message(snippet="‌﻿ ͏"))
    assert row["snippet"] is None


def test_ordinary_punctuation_and_accents_survive():
    """The strip is by Unicode category, so it must not reach real text."""
    row = gmail.to_row(message(snippet="Café — “quoted”, 50% off… naïve"))
    assert row["snippet"] == "Café — “quoted”, 50% off… naïve"


def test_metadata_format_is_what_is_requested(monkeypatch):
    """format=metadata is the guarantee that bodies are never stored: Gmail
    does not return them, so there is no path from here to message contents."""
    seen = {}

    def fake_get(url, params=None, **kw):
        seen.update(params or {})
        return message()

    monkeypatch.setattr(gmail, "get", fake_get)
    gmail.fetch_metadata("m1")
    assert seen["format"] == "metadata"


def test_no_body_field_is_ever_stored(db):
    """Even if Google sent a body, nothing reads it into a column."""
    payload = message()
    payload["payload"]["body"] = {"data": "c2VjcmV0IGJvZHk="}
    row = gmail.to_row(payload)
    assert "body" not in row
    assert "secret" not in json.dumps(row)


# ── storage ───────────────────────────────────────────────


def test_store_dedupes_on_message_id(db):
    from app.db import transaction

    for _ in range(2):
        with transaction() as conn:
            gmail.store(conn, gmail.to_row(message()))
    assert len(rows(db, "SELECT * FROM email_messages")) == 1


def test_store_updates_read_state(db):
    """The one field that legitimately changes after arrival — and keeping it
    current is what lets "anything I haven't read from Dana?" work."""
    from app.db import transaction

    with transaction() as conn:
        gmail.store(conn, gmail.to_row(message(labels=["INBOX", "UNREAD"])))
    with transaction() as conn:
        gmail.store(conn, gmail.to_row(message(labels=["INBOX"])))
    assert rows(db, "SELECT is_unread FROM email_messages")[0]["is_unread"] == 0


def test_stored_mail_is_searchable(db):
    from app import handlers
    from app.db import connect, transaction

    with transaction() as conn:
        gmail.store(conn, gmail.to_row(message(subject="Lease renewal")))

    conn = connect()
    try:
        found = handlers.search_email(conn, "lease")
    finally:
        conn.close()
    assert len(found) == 1
    assert found[0]["subject"] == "Lease renewal"


def test_prune_drops_old_mail_and_its_index_entry(db):
    """Hard delete, unlike notes — these rows are a cache of something Google
    still holds, so the FTS delete trigger fires properly and nothing lingers
    in the index."""
    from app import handlers
    from app.db import connect, transaction

    old = gmail.to_row(message("old", subject="Ancient thing"))
    old["received_at"] = "2020-01-01T00:00:00Z"
    with transaction() as conn:
        gmail.store(conn, old)
        assert gmail.prune(conn) == 1

    conn = connect()
    try:
        assert handlers.search_email(conn, "Ancient") == []
    finally:
        conn.close()


def test_ingestion_writes_no_mutations(db):
    """Same exception as the calendar ingester: this is not a user action, and
    burying the user's last real action would make /undo useless."""
    from app.db import transaction

    with transaction() as conn:
        for i in range(10):
            gmail.store(conn, gmail.to_row(message(f"m{i}")))
    assert rows(db, "SELECT * FROM mutations") == []


# ── the context pass ──────────────────────────────────────


@pytest.fixture
def fake_api(monkeypatch):
    calls: list[tuple[str, dict]] = []
    responses: list = []

    def fake_get(url, params=None, **kw):
        calls.append((url, dict(params or {})))
        if not responses:
            raise AssertionError(f"unexpected extra request to {url}")
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(gmail, "get", fake_get)
    return type("FakeApi", (), {"calls": calls, "responses": responses})()


def test_first_run_is_a_full_search(fake_api, db):
    fake_api.responses.extend(
        [
            {"historyId": "9000"},                       # profile
            {"messages": [{"id": "m1"}, {"id": "m2"}]},  # list
            message("m1"),
            message("m2"),
        ]
    )
    result = gmail.sync_context()
    assert result["full"] is True and result["stored"] == 2
    assert rows(db, "SELECT token FROM sync_state WHERE source='gmail'")[0][
        "token"
    ] == "9000"


def test_the_cursor_is_read_before_listing_not_after(fake_api, db):
    """Taking the historyId afterwards would open a window in which anything
    arriving mid-run is covered by the new cursor without ever having been
    fetched — a message silently missing forever."""
    fake_api.responses.extend(
        [{"historyId": "9000"}, {"messages": []}]
    )
    gmail.sync_context()
    assert fake_api.calls[0][0].endswith("/profile")


def test_an_expired_history_id_falls_back_to_a_full_fetch(fake_api, db):
    """Gmail expires historyIds on its own schedule. Same shape as Calendar's
    410 — routine, not fatal."""
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sync_state (source, token) VALUES ('gmail','1')")
    conn.commit()
    conn.close()

    fake_api.responses.extend(
        [
            {"historyId": "9000"},
            ApiError(404, "startHistoryId is too old", "u"),
            {"messages": [{"id": "m1"}]},
            message("m1"),
        ]
    )
    result = gmail.sync_context()
    assert result["full"] is True and result["stored"] == 1


def test_history_pass_dedupes_repeated_ids(fake_api, db):
    """Gmail's history can report the same message more than once."""
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sync_state (source, token) VALUES ('gmail','1')")
    conn.commit()
    conn.close()

    fake_api.responses.extend(
        [
            {"historyId": "9100"},
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "m1"}}]},
                    {"messagesAdded": [{"message": {"id": "m1"}}]},
                ]
            },
            message("m1"),
        ]
    )
    result = gmail.sync_context()
    assert result["stored"] == 1


def test_a_message_deleted_mid_run_is_skipped(fake_api, db, monkeypatch):
    fake_api.responses.extend([{"historyId": "1"}, {"messages": [{"id": "gone"}]}])
    monkeypatch.setattr(gmail, "fetch_metadata", lambda mid: None)
    assert gmail.sync_context()["stored"] == 0


# ── the proposal pass ─────────────────────────────────────


def seed(db, *messages) -> None:
    from app.db import transaction

    with transaction() as conn:
        for m in messages:
            gmail.store(conn, gmail.to_row(m))


def test_candidates_skip_already_examined_messages(db):
    """Without this, every run re-pays a Haiku call for the same marketing
    email that matched the query and yielded nothing."""
    from app.db import connect, transaction

    seed(db, message("m1"), message("m2"))
    with transaction() as conn:
        row_id = conn.execute(
            "SELECT id FROM email_messages WHERE external_id='m1'"
        ).fetchone()["id"]
        gmail.mark_examined(conn, row_id)

    conn = connect()
    try:
        assert [c["external_id"] for c in gmail.candidates(conn, 10)] == ["m2"]
    finally:
        conn.close()


def test_a_rejected_message_is_never_proposed_again(db):
    """migration 005's partial index EXCLUDES rejected rows, so it would
    happily allow a duplicate. The rule has to be enforced by the query."""
    from app.db import connect, transaction

    seed(db, message("m1"))
    with transaction() as conn:
        conn.execute(
            """INSERT INTO proposals (source, external_id, kind, payload_json, status)
                 VALUES ('gmail','m1','event','{}','rejected')"""
        )

    conn = connect()
    try:
        assert gmail.candidates(conn, 10) == []
    finally:
        conn.close()


@pytest.fixture
def fake_extract(monkeypatch):
    """Replace the Haiku call. Every proposal-pass test is offline."""
    calls: list[dict] = []
    queue: list[dict] = []

    def fake(message, tz_name):
        calls.append(message)
        return queue.pop(0) if queue else {"has_event": False}

    monkeypatch.setattr(gmail, "extract", fake)
    monkeypatch.setattr(gmail, "_list_ids", lambda q, n: ["m1", "m2", "m3"])
    return type("FakeExtract", (), {"calls": calls, "queue": queue})()


def test_a_high_confidence_extraction_becomes_a_proposal(db, fake_extract):
    seed(db, message("m1"))
    fake_extract.queue.append(
        {
            "has_event": True,
            "title": "Flight UA 412",
            "starts_at": "2026-08-10T07:30:00-06:00",
            "confidence": 0.9,
        }
    )
    result = gmail.sync_proposals(TZ)

    assert result["proposed"] == 1
    proposal = rows(db, "SELECT * FROM proposals")[0]
    assert proposal["status"] == "pending"
    assert "Flight UA 412" in proposal["summary"]
    # Stored normalized, like every other timestamp in this schema.
    assert json.loads(proposal["payload_json"])["starts_at"] == "2026-08-10T13:30:00Z"


def test_nothing_reaches_events_without_a_human(db, fake_extract):
    """The emphatic rule. One invented dentist appointment teaches you to
    distrust the agenda, and an agenda you don't trust is decoration."""
    seed(db, message("m1"))
    fake_extract.queue.append(
        {
            "has_event": True,
            "title": "Dentist",
            "starts_at": "2026-08-10T07:30:00-06:00",
            "confidence": 0.99,
        }
    )
    gmail.sync_proposals(TZ)
    assert rows(db, "SELECT * FROM events") == []


def test_low_confidence_is_dropped(db, fake_extract):
    seed(db, message("m1"))
    fake_extract.queue.append(
        {
            "has_event": True,
            "title": "Maybe something",
            "starts_at": "2026-08-10T07:30:00-06:00",
            "confidence": 0.2,
        }
    )
    assert gmail.sync_proposals(TZ)["proposed"] == 0


def test_an_event_with_no_time_is_dropped(db, fake_extract):
    """Accepting this would produce a calendar entry at an invented hour."""
    seed(db, message("m1"))
    fake_extract.queue.append({"has_event": True, "title": "Something", "confidence": 0.95})
    assert gmail.sync_proposals(TZ)["proposed"] == 0


def test_an_unparseable_timestamp_is_dropped_not_guessed(db, fake_extract):
    seed(db, message("m1"))
    fake_extract.queue.append(
        {"has_event": True, "title": "X", "starts_at": "next tuesday", "confidence": 0.9}
    )
    assert gmail.sync_proposals(TZ)["proposed"] == 0


def test_every_examined_message_is_marked_even_when_it_yields_nothing(db, fake_extract):
    """The whole point of examined_at: a message that produced nothing must
    never be paid for twice."""
    seed(db, message("m1"), message("m2"))
    gmail.sync_proposals(TZ)
    assert all(
        r["examined_at"] is not None
        for r in rows(db, "SELECT examined_at FROM email_messages")
    )


def test_the_per_run_message_ceiling_holds(db, fake_extract, monkeypatch):
    """An inbox sweep has no human waiting on it, so it can be capped — and
    must be, because an uncapped one could dwarf a month of normal spend."""
    ids = [f"m{i}" for i in range(10)]
    seed(db, *[message(i) for i in ids])
    monkeypatch.setattr(gmail, "_list_ids", lambda q, n: ids)

    result = gmail.sync_proposals(TZ, limit=3)
    assert result["examined"] == 3
    assert len(fake_extract.calls) == 3
    # The other seven are untouched, so the next run picks them up.
    unexamined = rows(
        db, "SELECT count(*) AS n FROM email_messages WHERE examined_at IS NULL"
    )[0]["n"]
    assert unexamined == 7


def test_the_spend_ceiling_stops_a_run_early(db, fake_extract, monkeypatch):
    """The message count bounds the work; the dollar figure bounds the damage
    when an assumption behind the count is wrong — a forwarded thread with
    fifty quoted replies makes one 'small' message enormous."""
    ids = [f"m{i}" for i in range(10)]
    seed(db, *[message(i) for i in ids])
    monkeypatch.setattr(gmail, "_list_ids", lambda q, n: ids)

    # Pretend the very first call blew the whole budget.
    spent = iter([0.0] + [gmail.MAX_SPEND_USD_PER_RUN * 2] * 20)
    monkeypatch.setattr(gmail, "_spent_usd", lambda: next(spent))

    result = gmail.sync_proposals(TZ, limit=10)
    assert result["examined"] == 1
    assert result["capped"] is True


def test_summary_is_templated_not_generated(db):
    """The model was already asked one question. Asking a second for a display
    string is a round trip and a cost for no gain."""
    line = gmail._summary(
        {"title": "Flight UA 412", "starts_at": "2026-08-10T13:30:00Z"}, TZ
    )
    assert line.startswith("Flight UA 412 — ")
    assert "2026-08-10T13:30:00Z" not in line, "spoken form, not an ISO string"
