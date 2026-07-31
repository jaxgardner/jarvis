"""Calendar ingestion — offline, no network.

Every Google response here is shaped like a real one. The traps this covers are
the ones docs/phase-6-ingestion.md §3 lists, and each of them is silent in
production: a sync token saved a page early skips events forever, a 410 treated
as fatal stops the ingester permanently, a cancellation not applied leaves a
meeting you will plan around.
"""

import sqlite3

import pytest

from app.config import REPO_ROOT
from ingest import calendar as cal
from ingest.client import ApiError
from tests.helpers import apply_migrations

TZ = "America/Denver"


@pytest.fixture
def conn():
    """In-memory, for the pure write helpers."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    for name in ("001_init.sql", "005_ingest.sql"):
        sql = (REPO_ROOT / "migrations" / name).read_text()
        c.executescript(
            "\n".join(
                line
                for line in sql.splitlines()
                if not line.strip().upper().startswith("PRAGMA")
            )
        )
    yield c
    c.close()


class LiveDB:
    """A real database file, because sync_calendar() opens its own connections
    through app.db.transaction() rather than taking one."""

    def __init__(self, path):
        self.path = path

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        c = self._conn()
        try:
            row = c.execute(sql, args).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def token(self, source: str) -> str | None:
        row = self.one("SELECT token FROM sync_state WHERE source = ?", (source,))
        return row["token"] if row else None

    def set_token(self, source: str, value: str) -> None:
        c = self._conn()
        try:
            c.execute(
                "INSERT INTO sync_state (source, token) VALUES (?,?)"
                " ON CONFLICT(source) DO UPDATE SET token = excluded.token",
                (source, value),
            )
            c.commit()
        finally:
            c.close()


@pytest.fixture
def live_db(tmp_path, monkeypatch):
    path = tmp_path / "ingest.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return LiveDB(path)


def event(
    event_id: str = "e1",
    *,
    summary: str = "Standup",
    start: dict | None = None,
    end: dict | None = None,
    status: str = "confirmed",
    **extra,
) -> dict:
    payload = {
        "id": event_id,
        "status": status,
        "summary": summary,
        "updated": "2026-07-30T18:00:00.000Z",
        "start": start if start is not None else {"dateTime": "2026-08-03T09:00:00-06:00"},
        "end": end if end is not None else {"dateTime": "2026-08-03T09:30:00-06:00"},
    }
    payload.update(extra)
    return payload


# ── field conversion ──────────────────────────────────────


def test_timed_events_normalize_to_utc():
    row = cal.to_row(event(), "cal@x", TZ)
    assert row["starts_at"] == "2026-08-03T15:00:00Z"
    assert row["ends_at"] == "2026-08-03T15:30:00Z"
    assert row["all_day"] == 0


def test_all_day_events_anchor_to_local_midnight():
    """They arrive as a bare YYYY-MM-DD with no offset at all. Storing that
    verbatim breaks the schema's rule and sorts wrongly against every other
    row, so it has to be anchored before normalizing."""
    row = cal.to_row(
        event(start={"date": "2026-08-03"}, end={"date": "2026-08-04"}), "cal@x", TZ
    )
    # Denver is UTC-6 in August, so local midnight is 06:00Z the same day.
    assert row["starts_at"] == "2026-08-03T06:00:00Z"
    assert row["all_day"] == 1


def test_all_day_anchoring_follows_the_zone_not_utc():
    denver = cal.to_row(event(start={"date": "2026-08-03"}, end=None), "c", "America/Denver")
    tokyo = cal.to_row(event(start={"date": "2026-08-03"}, end=None), "c", "Asia/Tokyo")
    assert denver["starts_at"] != tokyo["starts_at"]


# ── URL construction ──────────────────────────────────────


def test_a_calendar_id_containing_a_hash_survives_the_url():
    """The bug this test exists for, found on the first real run.

    Google's built-in holiday calendar is `en.usa#holiday@group.v.calendar.
    google.com`. Interpolated raw, that `#` starts a URL *fragment* and
    everything after it is stripped before the request leaves the process — so
    Google receives `/calendars/en.usa/events` and answers a truthful,
    completely baffling 404.
    """
    import httpx

    holidays = "en.usa#holiday@group.v.calendar.google.com"
    path = httpx.Request("GET", cal.events_url(holidays)).url.path

    assert path.endswith("/events"), "the path must not be truncated at the '#'"
    assert "en.usa" in path and "holiday" in path


def test_calendar_ids_cannot_escape_their_path_segment():
    """quote(safe='') and not quote(): the default leaves '/' alone, which
    would let an id containing a slash address a different endpoint."""
    assert "/" not in cal.events_url("a/../b").split("/calendars/")[1].split("/")[0]


def test_ordinary_calendar_ids_are_unharmed():
    assert cal.events_url("me@gmail.com").endswith("/calendars/me%40gmail.com/events")


def test_external_id_is_namespaced_by_calendar():
    """The same meeting on your primary and on a shared calendar carries the
    SAME Google event id. Keying on the bare id would collide on
    idx_events_ext and the row would flap between the two calendars' copies on
    every sync."""
    assert cal.external_id("work@x", "e1") != cal.external_id("home@x", "e1")


def test_untitled_events_get_a_placeholder():
    """events.title is NOT NULL, and Google omits summary for events with no
    title rather than sending an empty string."""
    payload = event()
    del payload["summary"]
    assert cal.to_row(payload, "c", TZ)["title"] == "(no title)"


def test_long_descriptions_are_truncated():
    row = cal.to_row(event(description="x" * 2000), "c", TZ)
    assert len(row["notes"]) <= cal.NOTES_LIMIT + 1  # +1 for the ellipsis


# ── writing ───────────────────────────────────────────────


def test_upsert_dedupes_on_re_sync(conn):
    """Running the same sync twice must not double the calendar."""
    for _ in range(2):
        cal.upsert(conn, cal.to_row(event(), "c", TZ))
    assert conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"] == 1


def test_upsert_updates_in_place(conn):
    cal.upsert(conn, cal.to_row(event(summary="Standup"), "c", TZ))
    cal.upsert(conn, cal.to_row(event(summary="Standup (moved)"), "c", TZ))
    rows = conn.execute("SELECT title FROM events").fetchall()
    assert [r["title"] for r in rows] == ["Standup (moved)"]


def test_synced_events_do_not_collide_with_voice_events(conn):
    """idx_events_ext is partial — voice rows keep a NULL external_id and many
    of them must coexist."""
    cal.upsert(conn, cal.to_row(event(), "c", TZ))
    for title in ("lunch", "gym"):
        conn.execute(
            "INSERT INTO events (title, starts_at) VALUES (?, '2026-08-03T15:00:00Z')",
            (title,),
        )
    assert conn.execute(
        "SELECT count(*) AS n FROM events WHERE external_id IS NULL"
    ).fetchone()["n"] == 2


def test_cancel_soft_deletes(conn):
    cal.upsert(conn, cal.to_row(event(), "c", TZ))
    assert cal.cancel(conn, "c", "e1") is True
    row = conn.execute("SELECT deleted_at FROM events").fetchone()
    assert row["deleted_at"] is not None


def test_cancelling_an_unknown_event_is_not_an_error(conn):
    """A full sync with showDeleted=true returns cancellations for events we
    never had. Nothing to do, and certainly not a crash."""
    assert cal.cancel(conn, "c", "never-seen") is False


def test_a_restored_event_stops_being_deleted(conn):
    """An event can be cancelled and then put back on. Leaving deleted_at set
    would hide a meeting that is happening."""
    cal.upsert(conn, cal.to_row(event(), "c", TZ))
    cal.cancel(conn, "c", "e1")
    cal.upsert(conn, cal.to_row(event(), "c", TZ))
    assert conn.execute("SELECT deleted_at FROM events").fetchone()["deleted_at"] is None


def test_synced_writes_leave_no_mutations(conn):
    """The documented exception to 'every write goes through the helper'.

    A few hundred synced rows per sync would bury the user's last real action
    and make /undo useless for the voice capture it exists to reverse.
    """
    for i in range(20):
        cal.upsert(conn, cal.to_row(event(f"e{i}"), "c", TZ))
    assert conn.execute("SELECT count(*) AS n FROM mutations").fetchone()["n"] == 0


# ── request parameters ────────────────────────────────────


def test_full_sync_asks_for_a_window():
    params = cal._params(None)
    assert "timeMin" in params and "timeMax" in params
    assert "syncToken" not in params


def test_incremental_sync_never_sends_a_time_range():
    """Google rejects timeMin combined with syncToken outright. The window is
    baked into the token by the first sync."""
    params = cal._params("tok123")
    assert params["syncToken"] == "tok123"
    assert "timeMin" not in params and "timeMax" not in params


def test_deletions_and_expansion_are_always_requested():
    for params in (cal._params(None), cal._params("tok")):
        # Without showDeleted, cancellations are omitted entirely and deleted
        # meetings stay in the agenda forever.
        assert params["showDeleted"] == "true"
        # Without singleEvents, recurring events arrive as rules and /agenda
        # would have to reimplement RRULE.
        assert params["singleEvents"] == "true"


# ── the sync loop ─────────────────────────────────────────


@pytest.fixture
def fake_api(monkeypatch):
    """Queue up responses for successive get() calls and record the requests."""
    calls: list[tuple[str, dict]] = []
    responses: list = []

    def fake_get(url, params=None, **kwargs):
        calls.append((url, dict(params or {})))
        if not responses:
            raise AssertionError(f"unexpected extra request to {url}")
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(cal, "get", fake_get)
    return type("FakeApi", (), {"calls": calls, "responses": responses})()


def test_sync_token_is_saved_only_after_the_last_page(fake_api, live_db):
    """Only the final page carries nextSyncToken. Saving a cursor early would
    skip every event on the pages not yet read — permanently, because the next
    run starts after them."""
    fake_api.responses.extend(
        [
            {"items": [event("e1")], "nextPageToken": "p2"},
            {"items": [event("e2")], "nextSyncToken": "SYNC-FINAL"},
        ]
    )
    result = cal.sync_calendar("c@x", TZ)

    assert result["written"] == 2
    assert live_db.token("calendar:c@x") == "SYNC-FINAL"
    # The second request must carry the page token, not a sync token.
    assert fake_api.calls[1][1]["pageToken"] == "p2"


def test_a_410_drops_the_cursor_and_refetches(fake_api, live_db):
    """Google expires sync tokens on its own schedule. An ingester that treats
    410 as fatal stops permanently after a quiet week."""
    live_db.set_token("calendar:c@x", "STALE")
    fake_api.responses.extend(
        [
            ApiError(410, "Sync token is no longer valid", "u"),
            {"items": [event("e1")], "nextSyncToken": "FRESH"},
        ]
    )
    result = cal.sync_calendar("c@x", TZ)

    assert result["full"] is True, "must fall back to a full fetch"
    assert result["written"] == 1
    assert live_db.token("calendar:c@x") == "FRESH"
    # The retry must be a real full sync: windowed, with no stale token.
    assert "syncToken" not in fake_api.calls[1][1]
    assert "timeMin" in fake_api.calls[1][1]


def test_a_non_410_error_is_not_swallowed(fake_api, live_db):
    """A 403 means the API is disabled or the scope was refused. Silently
    refetching in full would turn a configuration error into an infinite loop
    of expensive no-ops."""
    fake_api.responses.append(ApiError(403, "Calendar API has not been used", "u"))
    with pytest.raises(ApiError):
        cal.sync_calendar("c@x", TZ)


def test_cancellations_in_an_incremental_page_are_applied(fake_api, live_db):
    """Incremental sync is the ONLY way deletions arrive, and a cancelled entry
    carries almost nothing but an id and a status — so it has to be handled
    before anything tries to read its start block."""
    fake_api.responses.append(
        {"items": [event("e1"), event("e2")], "nextSyncToken": "T1"}
    )
    cal.sync_calendar("c@x", TZ)

    fake_api.responses.append(
        {
            "items": [{"id": "e1", "status": "cancelled"}],
            "nextSyncToken": "T2",
        }
    )
    result = cal.sync_calendar("c@x", TZ)

    assert result["deleted"] == 1
    assert live_db.one(
        "SELECT deleted_at FROM events WHERE external_id = 'c@x:e1'"
    )["deleted_at"] is not None
    assert live_db.one(
        "SELECT deleted_at FROM events WHERE external_id = 'c@x:e2'"
    )["deleted_at"] is None


def test_a_cancelled_event_is_not_resurrected_by_its_own_page(fake_api, live_db):
    """to_row() would raise on the missing start block — the status check has
    to come first."""
    fake_api.responses.append(
        {"items": [{"id": "ghost", "status": "cancelled"}], "nextSyncToken": "T1"}
    )
    result = cal.sync_calendar("c@x", TZ)
    assert result["written"] == 0
    assert live_db.one("SELECT count(*) AS n FROM events")["n"] == 0


def test_sync_state_records_success(fake_api, live_db):
    fake_api.responses.append({"items": [event()], "nextSyncToken": "T"})
    cal.sync_calendar("c@x", TZ)
    row = live_db.one("SELECT * FROM sync_state WHERE source = 'calendar:c@x'")
    assert row["last_ok_at"] is not None
    assert row["last_run_at"] is not None
    assert "written=1" in row["detail"]


def test_a_failing_calendar_does_not_stop_the_others(fake_api, live_db, monkeypatch):
    """A shared calendar that was revoked must not take your own down with it."""
    monkeypatch.setattr(
        cal, "calendars", lambda **kw: [{"id": "bad@x"}, {"id": "good@x"}]
    )
    fake_api.responses.extend(
        [
            ApiError(403, "forbidden", "u"),
            {"items": [event("e1")], "nextSyncToken": "T"},
        ]
    )
    result = cal.sync(TZ)

    assert result["written"] == 1, "the healthy calendar still synced"
    assert len(result["errors"]) == 1
    # The failure is recorded, and last_ok_at stays empty for that one.
    assert live_db.one("SELECT * FROM sync_state WHERE source='calendar:bad@x'")[
        "last_ok_at"
    ] is None


def test_a_failure_does_not_clear_the_cursor(fake_api, live_db):
    """Clearing on failure would silently promote every transient network blip
    into a full refetch."""
    live_db.set_token("calendar:c@x", "KEEP-ME")
    fake_api.responses.append(ApiError(500, "boom", "u"))
    with pytest.raises(ApiError):
        cal.sync_calendar("c@x", TZ)
    # sync() is what records the failure; the token must survive either way.
    assert live_db.token("calendar:c@x") == "KEEP-ME"


# ── calendar selection ────────────────────────────────────


def test_only_selected_calendars_sync(monkeypatch):
    monkeypatch.setattr(
        cal,
        "get",
        lambda url, params=None, **kw: {
            "items": [
                {"id": "primary@x", "primary": True},
                {"id": "family@x", "selected": True},
                {"id": "holidays@x"},  # unticked in the sidebar
            ]
        },
    )
    assert [c["id"] for c in cal.calendars()] == ["primary@x", "family@x"]


def test_primary_syncs_even_when_unticked(monkeypatch):
    """A hidden primary calendar is a strange state, and silently omitting
    your own calendar is the worst failure this ingester has."""
    monkeypatch.setattr(
        cal,
        "get",
        lambda url, params=None, **kw: {"items": [{"id": "me@x", "primary": True}]},
    )
    assert [c["id"] for c in cal.calendars()] == ["me@x"]
