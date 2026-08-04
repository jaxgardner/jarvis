"""The 7am job.

    uv run python -m brief.run           generate today's brief if absent
    uv run python -m brief.run --force   regenerate it, replacing today's row
    uv run python -m brief.run --show    print today's stored summary

Its own launchd job rather than a scheduler tick, and that is not an
arrangement of convenience: design principle 3 says reminders fire when the
agent is broken, which is why `scheduler/run.py` may not import anything that
reaches a model. This makes a Haiku call, so it cannot live there. It sits
beside `ingest.gmail` instead, which has the same shape — scheduled,
model-touching, and free to fail without taking reminders with it.

Idempotent by the day: a second run finds today's row and does nothing, so
launchd retrying after a failure cannot produce two pushes or two calls.
"""

import sys
from datetime import timedelta

from app import config, notify, timeutil
from app.db import connect, transaction
from brief import mail


def today(tz_name: str) -> str:
    """The brief's day. No cutoff — 7am is nowhere near midnight, unlike the
    gratitude day, which genuinely needed one."""
    return timeutil.now(tz_name).date().isoformat()


def stored(conn, on: str) -> dict | None:
    row = conn.execute("SELECT * FROM briefs WHERE brief_on = ?", (on,)).fetchone()
    return dict(row) if row else None


def _already_pushed(conn, on: str) -> bool:
    row = conn.execute("SELECT detail FROM heartbeats WHERE name = 'brief'").fetchone()
    return row is not None and row["detail"] == on


def _stamp(conn, on: str) -> None:
    """One push a morning, tracked in `heartbeats` rather than inferred from
    the `briefs` row.

    The two facts are genuinely separate and used to be conflated: the row
    means the summary exists, the stamp means you were told. Reading the
    first as the second meant anything that wrote the row early — a manual
    run, a `--force`, a retry after a failed push — left the 7am job with
    nothing to do and no notification to send. `gratitude.nudge` keeps the
    same bookkeeping in the same table for the same reason.
    """
    conn.execute(
        """INSERT INTO heartbeats (name, last_run_at, detail) VALUES ('brief',?,?)
             ON CONFLICT(name) DO UPDATE SET last_run_at = excluded.last_run_at,
                                             detail = excluded.detail""",
        (timeutil.to_utc_iso(timeutil.now("UTC")), on),
    )


def store(conn, on: str, summary: str | None, count: int) -> None:
    """Write the day's row, replacing any existing one.

    Stored even when the summary is None. The row means "the job ran today",
    which is what stops it running again every minute until midnight; the
    summary being empty is a separate fact and a normal one.
    """
    conn.execute(
        """INSERT INTO briefs (brief_on, mail_summary, message_count) VALUES (?,?,?)
             ON CONFLICT(brief_on) DO UPDATE SET mail_summary = excluded.mail_summary,
                                                 message_count = excluded.message_count""",
        (on, summary, count),
    )


def generate(tz_name: str | None = None, force: bool = False) -> dict:
    """Summarize the night's mail into today's row.

    Returns what happened rather than raising, so launchd sees a clean exit
    and the log carries the reason.
    """
    tz_name = tz_name or config.DEFAULT_TZ
    on = today(tz_name)

    conn = connect()
    try:
        if not force and stored(conn, on) is not None:
            return {"day": on, "generated": False, "reason": "already ran today"}
        cutoff = timeutil.to_utc_iso(
            timeutil.now("UTC") - timedelta(hours=mail.WINDOW_HOURS)
        )
        messages = mail.unread_since(conn, cutoff)
    finally:
        conn.close()

    summary = mail.summarize(messages)

    with transaction() as conn:
        store(conn, on, summary, len(messages))

    return {
        "day": on,
        "generated": True,
        "messages": len(messages),
        "summarized": summary is not None,
    }


def push(tz_name: str | None = None) -> bool:
    """Tell the phone the brief is ready.

    The body is deliberately not the brief. A brief is a paragraph and iOS
    truncates it, so the notification's job is to be tapped — which opens Talk
    with the mic live, and the answer is spoken from live data plus the stored
    summary.

    Silent on a day with nothing in it. A notification that opens to "nothing
    on the calendar and nothing in the mail" is how a useful prompt becomes a
    muted one.

    Deduped by the day and independent of whether this run generated the row,
    so a late catch-up after a sleeping machine still announces the brief and
    a second run cannot announce it twice. Nothing is stamped unless the push
    landed: `notify.push` returning False means it went nowhere, and on APNs
    alone no registered device is a real state.
    """
    from app import handlers

    tz_name = tz_name or config.DEFAULT_TZ
    on = today(tz_name)

    conn = connect()
    try:
        if _already_pushed(conn, on):
            return False
        row = stored(conn, on)
        agenda = handlers.agenda_rows(conn, tz_name, 1)
    finally:
        conn.close()

    has_mail = bool(row and row["mail_summary"])
    has_day = bool(agenda["events"] or agenda["reminders"])
    if not has_mail and not has_day:
        return False

    ok = notify.push(
        "Your morning brief is ready.",
        title="Morning brief",
        tags="sunrise",
        priority="default",
        category="BRIEF",
        data={"kind": "brief"},
        collapse_id=f"brief-{on}",
    )
    if not ok:
        return False

    with transaction() as conn:
        _stamp(conn, on)
    return True


def main() -> int:
    tz_name = config.DEFAULT_TZ

    if "--show" in sys.argv:
        conn = connect()
        try:
            row = stored(conn, today(tz_name))
        finally:
            conn.close()
        print(row["mail_summary"] if row and row["mail_summary"] else "(nothing stored)")
        return 0

    try:
        result = generate(tz_name, force="--force" in sys.argv)
    except Exception as exc:  # noqa: BLE001 — a CLI and a LaunchDaemon
        print(f"brief generation failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"brief {result['day']}: generated={result['generated']} "
        + (
            f"messages={result['messages']} summarized={result['summarized']}"
            if result["generated"]
            else result["reason"]
        )
    )

    # Unconditional, not hung off `result["generated"]`. A row that already
    # exists means the summary is written, not that it was announced — and
    # the push does its own once-a-day bookkeeping.
    print(f"pushed={push(tz_name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
