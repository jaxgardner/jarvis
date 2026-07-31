"""Google Calendar -> `events`.

    uv run python -m ingest.calendar            sync every selected calendar
    uv run python -m ingest.calendar --full     drop the cursors, refetch all
    uv run python -m ingest.calendar --status   what the cursors say

Read-only, one direction. Voice-created events keep `source='voice'` and are
never pushed back to Google. Two-way sync is a much larger problem — conflict
resolution, echo suppression, and a bug class that deletes real calendar
entries — and none of it is needed to make /agenda useful.

**Incremental sync is not an optimization.** `syncToken` is the only way
deletions arrive: Google reports them as entries with `status: "cancelled"`,
and a plain windowed re-fetch simply omits them, so a cancelled meeting stays
in the agenda forever. That is worse than never importing it, because you plan
around it.

**These writes bypass the mutations helper**, which is an exception to an
invariant CLAUDE.md states flatly. The reason (docs/phase-6-ingestion.md §3):
the mutations log exists to make *voice* input reversible, and a calendar
import is not a user action. There is nothing to regret, /undo on a synced row
is meaningless because the next sync re-adds it, and routing a few hundred rows
per sync through the log would bury the user's last real action under them —
making /undo useless for exactly the thing it was built for.
"""

import sys
import urllib.parse
from datetime import datetime, timedelta

from app import config, timeutil
from app.db import connect, transaction
from ingest import state
from ingest.client import ApiError, get

BASE = "https://www.googleapis.com/calendar/v3"

# Window for a FULL sync only. Incremental syncs cannot carry a time range at
# all — Google rejects timeMin together with syncToken — so the window is baked
# into the cursor by the first sync and these values stop applying until the
# next full refetch.
PAST_DAYS = 30
FUTURE_DAYS = 400

PAGE_SIZE = 250

# Google descriptions carry meeting boilerplate — dial-in blocks, legal
# footers. The first few hundred characters hold the part worth having (an
# address, a joining link); the rest is padding that would bloat every deep
# path prompt that reads this table.
NOTES_LIMIT = 500


# ── which calendars ───────────────────────────────────────


def calendars(*, all_visible: bool = False) -> list[dict]:
    """The calendars to sync: everything ticked in Google Calendar's sidebar.

    `selected` is what the sidebar checkbox writes, so this follows what the
    user already curated rather than inventing a second place to configure it.
    Shared and family calendars come along; a subscription unticked in the
    sidebar stays out.

    `primary` is included unconditionally. A hidden primary calendar is a
    strange state, and silently omitting your own calendar is the worst
    possible failure for this ingester.
    """
    items = get(f"{BASE}/users/me/calendarList", {"maxResults": 250}).get("items", [])
    if all_visible:
        return items
    return [c for c in items if c.get("selected") or c.get("primary")]


# ── field conversion ──────────────────────────────────────


def _when(marker: dict, tz_name: str) -> tuple[str, bool]:
    """One Calendar start/end block -> (stored UTC timestamp, all_day).

    All-day events arrive as a bare `YYYY-MM-DD` with no offset whatsoever.
    Storing that verbatim would violate the schema's timestamps-carry-an-offset
    rule and sort wrongly against every other row, so it is anchored to local
    midnight before normalizing.
    """
    if marker.get("dateTime"):
        return timeutil.to_utc_iso(marker["dateTime"]), False
    day = datetime.strptime(marker["date"], "%Y-%m-%d").replace(
        tzinfo=timeutil.zone(tz_name)
    )
    return timeutil.to_utc_iso(day), True


def to_row(event: dict, calendar_id: str, tz_name: str) -> dict:
    starts_at, all_day = _when(event["start"], tz_name)
    ends_at = None
    if event.get("end"):
        ends_at, _ = _when(event["end"], tz_name)

    description = (event.get("description") or "").strip()
    if len(description) > NOTES_LIMIT:
        description = description[:NOTES_LIMIT].rstrip() + "…"

    return {
        "title": (event.get("summary") or "(no title)").strip(),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "all_day": int(all_day),
        "location": (event.get("location") or "").strip() or None,
        "notes": description or None,
        "external_id": external_id(calendar_id, event["id"]),
        # Google's own last-modified for the event, not our write time: this
        # column then answers "when did this meeting last change", which is
        # the useful question. Our write time is implicit in the sync run.
        "updated_at": timeutil.to_utc_iso(event["updated"])
        if event.get("updated")
        else timeutil.to_utc_iso(timeutil.now("UTC")),
    }


def events_url(calendar_id: str) -> str:
    """The events endpoint for one calendar, with the id safely encoded.

    Calendar ids are not opaque tokens — they are addresses, and Google's
    built-in calendars use characters that mean something in a URL. The US
    holiday calendar is `en.usa#holiday@group.v.calendar.google.com`, and that
    `#` starts a *fragment*: interpolated raw, everything after it is stripped
    before the request leaves the process, so Google receives a request for
    `/calendars/en.usa` and answers a truthful, baffling 404.

    quote(safe="") and not quote(): the default leaves `/` alone, which would
    let an id containing a slash escape its path segment.
    """
    return f"{BASE}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"


def external_id(calendar_id: str, event_id: str) -> str:
    """Namespaced by calendar, and it has to be.

    Google event ids are unique per *event*, not per calendar — the same
    meeting sitting on your primary calendar and on a shared team calendar
    carries the same id in both. Keying on the bare id would make those two
    rows collide on `idx_events_ext`, and each sync would overwrite the other's
    copy: the row would flap between two calendars' versions forever.

    The cost is that such a meeting stores as two rows and shows twice in the
    agenda. That is visible and mildly annoying; the flapping alternative is
    invisible and wrong.
    """
    return f"{calendar_id}:{event_id}"


# ── writing ───────────────────────────────────────────────

_UPSERT = """
INSERT INTO events
  (title, starts_at, ends_at, all_day, location, notes,
   source, external_id, updated_at, deleted_at)
VALUES (?,?,?,?,?,?,'calendar',?,?,NULL)
ON CONFLICT(source, external_id) WHERE external_id IS NOT NULL DO UPDATE SET
  title      = excluded.title,
  starts_at  = excluded.starts_at,
  ends_at    = excluded.ends_at,
  all_day    = excluded.all_day,
  location   = excluded.location,
  notes      = excluded.notes,
  updated_at = excluded.updated_at,
  -- Cleared on purpose. An event can be cancelled and then restored, and
  -- leaving deleted_at set would hide a meeting that is back on.
  deleted_at = NULL
"""


def upsert(conn, row: dict) -> None:
    conn.execute(
        _UPSERT,
        (
            row["title"],
            row["starts_at"],
            row["ends_at"],
            row["all_day"],
            row["location"],
            row["notes"],
            row["external_id"],
            row["updated_at"],
        ),
    )


def cancel(conn, calendar_id: str, event_id: str) -> bool:
    """Soft-delete a synced event. Returns whether a row was actually there.

    Soft, not hard: /agenda already filters `deleted_at IS NULL`, and keeping
    the row means a restored meeting reuses its id instead of arriving as a new
    one.
    """
    cur = conn.execute(
        """UPDATE events
             SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
             WHERE source = 'calendar' AND external_id = ? AND deleted_at IS NULL""",
        (external_id(calendar_id, event_id),),
    )
    return cur.rowcount > 0


# ── one calendar ──────────────────────────────────────────


def _params(sync_token: str | None) -> dict:
    common = {
        # Recurring events arrive expanded, one occurrence per row. Storing the
        # series and expanding at read time would mean reimplementing RRULE in
        # /agenda, and the scheduler's recurrence support is deliberately
        # limited to 'daily' / 'weekly:MO,WE'.
        "singleEvents": "true",
        # Without this, cancellations are omitted entirely and deleted meetings
        # stay in the agenda forever. It is the whole point of incremental sync.
        "showDeleted": "true",
        "maxResults": PAGE_SIZE,
    }
    if sync_token:
        # timeMin/timeMax cannot be combined with syncToken — Google rejects
        # the request outright.
        return {**common, "syncToken": sync_token}
    now = timeutil.now("UTC")
    return {
        **common,
        "timeMin": timeutil.to_utc_iso(now - timedelta(days=PAST_DAYS)),
        "timeMax": timeutil.to_utc_iso(now + timedelta(days=FUTURE_DAYS)),
    }


def sync_calendar(calendar_id: str, tz_name: str) -> dict:
    """Sync one calendar. Returns counts.

    Pagination and the cursor interact in a way that is easy to get wrong: only
    the *final* page carries `nextSyncToken`. Saving a cursor before the last
    page would silently skip every event on the pages not yet read — and skip
    them permanently, because the next run would start after them.
    """
    source = f"{state.CALENDAR_PREFIX}{calendar_id}"
    with transaction() as conn:
        state.start(conn, source)
        sync_token = state.token(conn, source)

    full = sync_token is None
    written = deleted = 0
    page_token: str | None = None
    next_sync_token: str | None = None

    while True:
        params = _params(sync_token)
        if page_token:
            params["pageToken"] = page_token

        try:
            payload = get(events_url(calendar_id), params)
        except ApiError as exc:
            if exc.status == 410 and not full:
                # Routine. Google expires sync tokens on its own schedule, and
                # an ingester that treats this as fatal stops permanently after
                # a quiet week. Drop the cursor and start over in full.
                with transaction() as conn:
                    state.clear_token(conn, source)
                return sync_calendar(calendar_id, tz_name)
            raise

        items = payload.get("items", [])
        with transaction() as conn:
            for event in items:
                # Checked before parsing: a cancellation delivered by an
                # incremental sync carries almost nothing else — often just an
                # id and a status — so to_row() would raise on the missing
                # start block.
                if event.get("status") == "cancelled":
                    if cancel(conn, calendar_id, event["id"]):
                        deleted += 1
                    continue
                if not event.get("start"):
                    continue  # defensive: no start, nothing storable
                upsert(conn, to_row(event, calendar_id, tz_name))
                written += 1

        page_token = payload.get("nextPageToken")
        next_sync_token = payload.get("nextSyncToken")
        if not page_token:
            break

    detail = f"{'full' if full else 'incremental'} written={written} deleted={deleted}"
    with transaction() as conn:
        state.succeeded(conn, source, next_sync_token, detail)

    return {"calendar": calendar_id, "written": written, "deleted": deleted, "full": full}


# ── all calendars ─────────────────────────────────────────


def sync(tz_name: str | None = None) -> dict:
    """Sync every selected calendar.

    One calendar's failure does not abort the others. A shared calendar that
    was revoked should not stop your own from syncing, and the per-calendar
    `sync_state` row records which one broke.
    """
    tz_name = tz_name or config.DEFAULT_TZ
    selected = calendars()
    results: list[dict] = []
    errors: list[str] = []

    for calendar in selected:
        calendar_id = calendar["id"]
        try:
            results.append(sync_calendar(calendar_id, tz_name))
        except Exception as exc:  # noqa: BLE001 — one bad calendar, not all of them
            message = f"{calendar.get('summary', calendar_id)}: {exc}"
            errors.append(message)
            with transaction() as conn:
                state.failed(conn, f"{state.CALENDAR_PREFIX}{calendar_id}", str(exc))

    return {
        # Attempted and succeeded, separately. Reporting only successes prints
        # "calendars=1" after trying two, which reads like the second one was
        # never selected rather than like it failed.
        "attempted": len(selected),
        "calendars": len(results),
        "written": sum(r["written"] for r in results),
        "deleted": sum(r["deleted"] for r in results),
        "results": results,
        "errors": errors,
    }


def reset() -> None:
    """Drop every calendar cursor, so the next run refetches in full."""
    with transaction() as conn:
        conn.execute(
            "UPDATE sync_state SET token = NULL WHERE source LIKE ?",
            (f"{state.CALENDAR_PREFIX}%",),
        )


def status() -> int:
    conn = connect()
    try:
        rows = [
            r
            for r in state.all_rows(conn)
            if r["source"].startswith(state.CALENDAR_PREFIX)
        ]
    finally:
        conn.close()
    if not rows:
        print("no calendar has been synced yet")
        return 0
    for row in rows:
        print(f"{row['source']}")
        print(f"  last run  {row['last_run_at'] or '—'}")
        print(f"  last ok   {row['last_ok_at'] or '—'}")
        print(f"  detail    {row['detail'] or '—'}")
    return 0


def main() -> int:
    if "--status" in sys.argv:
        return status()
    if "--full" in sys.argv:
        reset()
        print("cursors dropped — this run refetches in full")
    try:
        result = sync()
    except Exception as exc:  # noqa: BLE001 — a CLI and a LaunchDaemon
        print(f"calendar sync failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"calendars={result['calendars']}/{result['attempted']} "
        f"written={result['written']} deleted={result['deleted']}"
    )
    for error in result["errors"]:
        print(f"  ERROR {error}", file=sys.stderr)
    # A partial failure is a failure. launchd's log should show a non-zero exit
    # rather than a cheerful line with an error buried under it.
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
