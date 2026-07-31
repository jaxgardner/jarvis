"""Gmail -> `email_messages` (context) and `proposals` (review queue).

    uv run python -m ingest.gmail             both passes
    uv run python -m ingest.gmail --context   metadata only, no model calls
    uv run python -m ingest.gmail --full      drop the cursor, refetch
    uv run python -m ingest.gmail --status    what the cursor says

Two passes, with very different risk profiles, which is why they are separate
functions and can be run separately.

**Pass 1, context.** Metadata and Google's own `snippet` for recent mail, into
`email_messages`. No model, no extraction, no cost — the assistant can answer
"did the landlord email me back?" by quoting what actually arrived. Nothing
from this pass ever becomes an event or a reminder.

**Pass 2, proposals.** A deliberately narrow query (flights, appointments,
deliveries, reservations) picks candidates, and one Haiku call per candidate
extracts a possible event into `proposals`. **Nothing here reaches `events`
without a human accepting it**, and acceptance goes through the mutations
helper so it is logged and undoable.

The query does the filtering, not the model. A broad query leaning on the model
to reject irrelevant mail costs a Haiku call per message and is worse at it.

Worth stating plainly: the risk is not that extraction is occasionally wrong.
It is that one invented dentist appointment teaches you to distrust the agenda,
and an agenda you don't trust is decoration.
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import anthropic

from app import config, timeutil, usage
from app.db import connect, transaction
from ingest import state
from ingest.client import ApiError, get

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# ── pass 1: context ───────────────────────────────────────

# Everything recent that isn't junk. Broad on purpose — this pass is free, and
# a narrow context window is what makes an assistant feel like it doesn't know
# anything.
CONTEXT_QUERY = "newer_than:30d -in:spam -in:trash -in:chats"

# Ceiling on a first run. A mailbox with 40,000 recent messages should not turn
# the first sync into an hour of metadata fetches.
CONTEXT_MAX_MESSAGES = 1500

# Rows older than this are pruned. Keeps the table and its FTS index bounded
# without anyone having to think about it.
RETENTION_DAYS = 90

HEADERS = ("From", "To", "Subject", "Date")

# ── pass 2: proposals ─────────────────────────────────────

PROPOSAL_QUERY = (
    "newer_than:14d -in:spam -in:trash -in:chats "
    "(flight OR boarding OR itinerary OR reservation OR booking OR appointment "
    "OR confirmed OR confirmation OR delivery OR shipped OR "
    'subject:("your order" OR "your booking" OR "your appointment"))'
)

# Two hard ceilings per run, and both are wanted. /say has a human waiting and
# is naturally self-limiting; an inbox sweep is not, and an uncapped one could
# dwarf a month of normal spend in a single tick.
#
# The message count bounds the work. The dollar figure bounds the *damage* if
# an assumption behind the count turns out to be wrong — a forwarded thread
# with fifty quoted replies makes one "small" message enormous, and the count
# alone would not notice.
MAX_EXTRACTIONS_PER_RUN = 25
MAX_SPEND_USD_PER_RUN = 0.10

# Below this, the extraction is not worth showing. A proposal you have to
# research before accepting is worse than no proposal.
MIN_CONFIDENCE = 0.5

EXTRACT_TOOL = {
    "name": "record_extraction",
    "description": "Record what this email describes, or that it describes nothing schedulable.",
    "input_schema": {
        "type": "object",
        "properties": {
            "has_event": {
                "type": "boolean",
                "description": (
                    "True ONLY if the email describes a specific commitment at "
                    "a specific date and time that the user should have on "
                    "their calendar. Marketing, newsletters, receipts for "
                    "things already finished, and generic 'book now' offers "
                    "are all false."
                ),
            },
            "title": {"type": "string", "description": "Short title, e.g. 'Flight UA 412 to Denver'."},
            "starts_at": {
                "type": "string",
                "description": "Absolute ISO 8601 with offset. Omit if the email does not state one.",
            },
            "ends_at": {"type": "string", "description": "Absolute ISO 8601 with offset."},
            "location": {"type": "string"},
            "all_day": {"type": "boolean"},
            "confidence": {
                "type": "number",
                "description": (
                    "0 to 1. Be harsh. Below 0.5 means you are guessing at the "
                    "date, the time, or whether this is a real commitment."
                ),
            },
        },
        "required": ["has_event"],
    },
}

_EXTRACT_SYSTEM = """\
You read one email and decide whether it describes a specific commitment that \
belongs on the user's calendar — a flight, an appointment, a reservation, a \
scheduled delivery.

You are the filter, and you should be a harsh one. A false positive puts an \
invented appointment on someone's calendar and teaches them to distrust it. A \
false negative costs nothing: the email is still searchable.

Say has_event=false for anything you are not sure about, and especially for:
marketing that mentions a date, newsletters, "book your appointment today" \
offers, receipts for something already completed, and anything where you would \
have to guess the year.

Every timestamp you emit must be absolute ISO 8601 with an offset. If the \
email does not state a time zone, use the user's: {tz_name}. Today is \
{today}.\
"""


# ── fetching ──────────────────────────────────────────────


def profile() -> dict:
    return get(f"{BASE}/profile")


def _list_ids(query: str, limit: int) -> list[str]:
    """Message ids matching a Gmail search, newest first, up to `limit`."""
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < limit:
        params = {"q": query, "maxResults": min(500, limit - len(ids))}
        if page_token:
            params["pageToken"] = page_token
        payload = get(f"{BASE}/messages", params)
        ids.extend(m["id"] for m in payload.get("messages", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return ids[:limit]


def _added_since(history_id: str, limit: int) -> list[str]:
    """Message ids added since `history_id`.

    Raises ApiError(404) when Gmail has expired the cursor, which it does on
    its own schedule. That is routine — the caller drops the cursor and does a
    full pass, exactly like Calendar's 410.
    """
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < limit:
        params = {
            "startHistoryId": history_id,
            "historyTypes": "messageAdded",
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = get(f"{BASE}/history", params)
        for entry in payload.get("history", []):
            for added in entry.get("messagesAdded", []):
                message = added.get("message", {})
                if message.get("id"):
                    ids.append(message["id"])
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    # History can report the same message more than once; dedupe while keeping
    # order so the newest-first cap stays meaningful.
    return list(dict.fromkeys(ids))[:limit]


def fetch_metadata(message_id: str) -> dict | None:
    """One message, headers and snippet only.

    `format=metadata` is what keeps this honest: Gmail does not return the body
    at all, so there is no path from this function to storing message contents,
    accidentally or otherwise.
    """
    try:
        return get(
            f"{BASE}/messages/{message_id}",
            {"format": "metadata", "metadataHeaders": list(HEADERS)},
        )
    except ApiError as exc:
        if exc.status == 404:
            return None  # deleted between listing and fetching
        raise


def to_row(message: dict) -> dict:
    headers = {
        h.get("name", "").lower(): h.get("value", "")
        for h in message.get("payload", {}).get("headers", [])
    }
    labels = message.get("labelIds", []) or []
    # internalDate is epoch milliseconds as a string, and is the arrival time
    # Gmail itself sorts by — more reliable than the Date header, which the
    # sender controls and routinely gets wrong.
    received = datetime.fromtimestamp(
        int(message.get("internalDate", "0")) / 1000, tz=timezone.utc
    )
    return {
        "external_id": message["id"],
        "thread_id": message.get("threadId"),
        "sender": headers.get("from"),
        "recipient": headers.get("to"),
        "subject": headers.get("subject"),
        "snippet": (message.get("snippet") or "").strip() or None,
        "received_at": timeutil.to_utc_iso(received),
        "labels": json.dumps(labels),
        "is_unread": int("UNREAD" in labels),
    }


_UPSERT = """
INSERT INTO email_messages
  (external_id, thread_id, sender, recipient, subject, snippet,
   received_at, labels, is_unread)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(external_id) DO UPDATE SET
  thread_id = excluded.thread_id,
  sender    = excluded.sender,
  recipient = excluded.recipient,
  subject   = excluded.subject,
  snippet   = excluded.snippet,
  labels    = excluded.labels,
  -- Read/unread is the one field that legitimately changes after arrival, and
  -- keeping it current is what lets "anything I haven't read from Dana?" work.
  is_unread = excluded.is_unread
"""


def store(conn, row: dict) -> None:
    conn.execute(
        _UPSERT,
        (
            row["external_id"],
            row["thread_id"],
            row["sender"],
            row["recipient"],
            row["subject"],
            row["snippet"],
            row["received_at"],
            row["labels"],
            row["is_unread"],
        ),
    )


def prune(conn) -> int:
    """Drop rows past the retention window.

    A hard delete, unlike notes: these rows are a cache of something Google
    still holds, so there is nothing to preserve and the FTS delete trigger
    fires properly.
    """
    cutoff = timeutil.to_utc_iso(timeutil.now("UTC") - timedelta(days=RETENTION_DAYS))
    return conn.execute(
        "DELETE FROM email_messages WHERE received_at < ?", (cutoff,)
    ).rowcount


# ── pass 1 ────────────────────────────────────────────────


def sync_context() -> dict:
    """Metadata for recent mail. No model calls, so no spend.

    The cursor is read from the profile *before* listing, not after. Taking it
    afterwards would open a window in which anything arriving mid-run is
    covered by the new cursor without ever having been fetched — a message
    silently missing forever. Taking it first can only cause a few messages to
    be fetched twice, and the upsert makes that free.
    """
    with transaction() as conn:
        state.start(conn, state.GMAIL)
        cursor = state.token(conn, state.GMAIL)

    next_cursor = str(profile().get("historyId") or "")

    ids: list[str] = []
    full = cursor is None
    if not full:
        try:
            ids = _added_since(cursor, CONTEXT_MAX_MESSAGES)
        except ApiError as exc:
            if exc.status != 404:
                raise
            # Gmail expired the historyId. Routine, same as Calendar's 410.
            full = True
    if full:
        ids = _list_ids(CONTEXT_QUERY, CONTEXT_MAX_MESSAGES)

    stored = 0
    for message_id in ids:
        message = fetch_metadata(message_id)
        if message is None:
            continue
        with transaction() as conn:
            store(conn, to_row(message))
        stored += 1

    with transaction() as conn:
        pruned = prune(conn)
        state.succeeded(
            conn,
            state.GMAIL,
            next_cursor,
            f"{'full' if full else 'incremental'} stored={stored} pruned={pruned}",
        )

    return {"stored": stored, "pruned": pruned, "full": full}


# ── pass 2 ────────────────────────────────────────────────


def candidates(conn, limit: int) -> list[dict]:
    """Messages worth spending a Haiku call on.

    Two filters beyond the Gmail query, both about not paying twice:

      * `examined_at IS NULL` — a message already looked at is never looked at
        again, even if it produced nothing. Without this, every run re-pays for
        the same marketing email that matched the query forever.
      * no existing `proposals` row — including a *rejected* one. The partial
        unique index in migration 005 excludes rejected rows, so it would
        happily allow a duplicate; the rule that re-reading an inbox must not
        re-propose what you already said no to has to be enforced here.
    """
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, external_id, sender, subject, snippet, received_at
                 FROM email_messages
                 WHERE examined_at IS NULL
                   AND external_id NOT IN (
                     SELECT external_id FROM proposals WHERE source = 'gmail'
                   )
                 ORDER BY received_at DESC
                 LIMIT ?""",
            (limit,),
        ).fetchall()
    ]


def mark_examined(conn, row_id: int) -> None:
    conn.execute(
        "UPDATE email_messages SET examined_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        " WHERE id = ?",
        (row_id,),
    )


_CLIENT: anthropic.Anthropic | None = None


def _client() -> anthropic.Anthropic:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=config.anthropic_api_key())
    return _CLIENT


def extract(message: dict, tz_name: str) -> dict:
    """One Haiku call. Returns the tool input, always — `tool_choice: any`
    guarantees structured output, exactly as the fast-path router does."""
    from app.router import MODEL

    body = (
        f"From: {message.get('sender') or '(unknown)'}\n"
        f"Subject: {message.get('subject') or '(none)'}\n"
        f"Received: {message.get('received_at')}\n"
        f"Snippet: {message.get('snippet') or '(none)'}"
    )
    response = _client().messages.create(
        model=MODEL,
        max_tokens=512,
        system=_EXTRACT_SYSTEM.format(
            tz_name=tz_name,
            today=timeutil.now(tz_name).strftime("%A, %B %-d, %Y"),
        ),
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": body}],
    )
    usage.record(response.usage)
    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)
    return {"has_event": False}


def _summary(payload: dict, tz_name: str) -> str:
    """One line for the review list. Templated, not generated — the model was
    already asked one question and should not be asked a second."""
    title = payload.get("title") or "Untitled"
    if not payload.get("starts_at"):
        return title
    try:
        when = timeutil.speak_datetime(
            payload["starts_at"], tz_name, bool(payload.get("all_day"))
        )
    except ValueError:
        return title
    where = f" at {payload['location']}" if payload.get("location") else ""
    return f"{title} — {when}{where}"


def sync_proposals(tz_name: str | None = None, limit: int | None = None) -> dict:
    """Extract candidate events from recent mail into the review queue."""
    tz_name = tz_name or config.DEFAULT_TZ
    limit = MAX_EXTRACTIONS_PER_RUN if limit is None else limit

    # Refresh the candidate pool first: the narrow query decides what is worth
    # a model call, and it is a much tighter filter than the context query.
    try:
        narrow = set(_list_ids(PROPOSAL_QUERY, limit * 4))
    except ApiError as exc:
        return {"examined": 0, "proposed": 0, "error": str(exc)}

    conn = connect()
    try:
        pool = [m for m in candidates(conn, limit * 4) if m["external_id"] in narrow]
    finally:
        conn.close()

    examined = proposed = 0
    capped = False
    for message in pool[:limit]:
        if _spent_usd() >= MAX_SPEND_USD_PER_RUN:
            capped = True
            break

        payload = extract(message, tz_name)
        examined += 1

        with transaction() as conn:
            mark_examined(conn, message["id"])

            confidence = float(payload.get("confidence") or 0.0)
            if not payload.get("has_event") or confidence < MIN_CONFIDENCE:
                continue
            if not payload.get("starts_at"):
                # No time means nothing to put on a calendar. Accepting this
                # would produce an event at an invented hour.
                continue
            try:
                payload["starts_at"] = timeutil.to_utc_iso(payload["starts_at"])
                if payload.get("ends_at"):
                    payload["ends_at"] = timeutil.to_utc_iso(payload["ends_at"])
            except ValueError:
                continue  # unparseable timestamp — drop rather than guess

            cur = conn.execute(
                """INSERT INTO proposals
                     (source, external_id, kind, payload_json, summary, confidence)
                   VALUES ('gmail', ?, 'event', ?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (
                    message["external_id"],
                    json.dumps(payload),
                    _summary(payload, tz_name),
                    confidence,
                ),
            )
            # rowcount, not an unconditional increment: ON CONFLICT DO NOTHING
            # means the insert can be a no-op, and counting those would report
            # proposals that nobody will ever see in the review queue.
            proposed += cur.rowcount

    return {"examined": examined, "proposed": proposed, "capped": capped}


def _spent_usd() -> float:
    """What this run has cost so far, from the open usage tally.

    Returns 0.0 outside a tally scope — which is what tests and one-off calls
    get, and means the ceiling never fires spuriously when nothing is counting.
    """
    from app.router import cost_usd

    spend = usage.current()
    return cost_usd(spend["input_tokens"], spend["output_tokens"])


# ── CLI ───────────────────────────────────────────────────


def reset() -> None:
    with transaction() as conn:
        state.clear_token(conn, state.GMAIL)


def status() -> int:
    conn = connect()
    try:
        row = next(
            (r for r in state.all_rows(conn) if r["source"] == state.GMAIL), None
        )
        counts = conn.execute(
            """SELECT count(*) AS stored,
                      sum(examined_at IS NULL) AS unexamined
                 FROM email_messages"""
        ).fetchone()
        pending = conn.execute(
            "SELECT count(*) AS n FROM proposals WHERE status = 'pending'"
        ).fetchone()["n"]
    finally:
        conn.close()

    if row is None:
        print("gmail has never been synced")
    else:
        print(f"last run  {row['last_run_at'] or '—'}")
        print(f"last ok   {row['last_ok_at'] or '—'}")
        print(f"detail    {row['detail'] or '—'}")
    print(f"messages  {counts['stored']} stored, {counts['unexamined'] or 0} unexamined")
    print(f"proposals {pending} pending review")
    return 0


def main() -> int:
    if "--status" in sys.argv:
        return status()
    if "--full" in sys.argv:
        reset()
        print("cursor dropped — this run refetches")

    with usage.tally():
        try:
            context = sync_context()
        except Exception as exc:  # noqa: BLE001 — a CLI and a LaunchDaemon
            print(f"gmail context sync failed: {exc}", file=sys.stderr)
            with transaction() as conn:
                state.failed(conn, state.GMAIL, str(exc))
            return 1

        print(
            f"context: stored={context['stored']} pruned={context['pruned']} "
            f"full={context['full']}"
        )

        if "--context" in sys.argv:
            return 0

        try:
            result = sync_proposals()
        except Exception as exc:  # noqa: BLE001
            print(f"gmail proposal pass failed: {exc}", file=sys.stderr)
            return 1

        spend = usage.current()
        print(
            f"proposals: examined={result['examined']} proposed={result['proposed']} "
            f"calls={spend['model_calls']} spend=${_spent_usd():.4f}"
        )
        if result.get("capped"):
            print(
                f"  (stopped at the ${MAX_SPEND_USD_PER_RUN:.2f} per-run ceiling; "
                "the rest will be picked up next run)",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
