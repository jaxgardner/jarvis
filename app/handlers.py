"""Tool handlers.

Confirmations are **templated, not generated** — once the router has given us
add_reminder(body=..., fire_at=...), the reply is a formatting problem, not a
model problem. Deterministic, and it saves a round trip against the 2s budget.
"""

import json
import sqlite3
from datetime import datetime, timedelta

from app import mutations, router, timeutil


def _lookup_or_create(
    conn: sqlite3.Connection, utterance_id: int, table: str, name: str
) -> int:
    name = name.strip()
    found = conn.execute(
        f"SELECT id FROM {table} WHERE lower(name) = lower(?)", (name,)  # noqa: S608
    ).fetchone()
    if found:
        return int(found["id"])
    return mutations.insert(conn, utterance_id, table, {"name": name})


# ── writes ────────────────────────────────────────────────


def add_event(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    all_day = bool(args.get("all_day"))
    starts_at = timeutil.to_utc_iso(args["starts_at"])
    values = {
        "title": args["title"].strip(),
        "starts_at": starts_at,
        "all_day": int(all_day),
        "source": "voice",
    }
    if args.get("ends_at"):
        values["ends_at"] = timeutil.to_utc_iso(args["ends_at"])
    if args.get("location"):
        values["location"] = args["location"].strip()

    mutations.insert(conn, utterance_id, "events", values)
    when = timeutil.speak_datetime(starts_at, tz_name, all_day)
    where = f" at {values['location']}" if values.get("location") else ""
    return f"Got it — {values['title']}, {when}{where}."


def add_reminder(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    fire_at = timeutil.to_utc_iso(args["fire_at"])
    values = {"body": args["body"].strip(), "fire_at": fire_at}
    if args.get("recurrence"):
        values["recurrence"] = args["recurrence"].strip()

    mutations.insert(conn, utterance_id, "reminders", values)
    when = timeutil.speak_datetime(fire_at, tz_name)
    repeat = f", repeating {values['recurrence']}" if values.get("recurrence") else ""
    return f"Got it — I'll remind you to {values['body']} {when}{repeat}."


def add_note(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    values = {"body": args["body"].strip()}
    if args.get("tags"):
        values["tags"] = json.dumps(args["tags"])
    if args.get("project"):
        values["project_id"] = _lookup_or_create(
            conn, utterance_id, "projects", args["project"]
        )
    if args.get("person"):
        values["person_id"] = _lookup_or_create(
            conn, utterance_id, "people", args["person"]
        )

    mutations.insert(conn, utterance_id, "notes", values)
    return "Noted."


def _find_match(conn, what: str) -> tuple[str, dict] | None:
    """Locate an existing event or reminder the user is referring to.

    Matches on any word longer than two characters, preferring soonest-first
    among things that haven't happened yet — "the dentist thing" almost always
    means the upcoming one, not last year's.
    """
    terms = [w for w in "".join(c if c.isalnum() else " " for c in what).split() if len(w) > 2]
    if not terms:
        return None
    now_utc = timeutil.to_utc_iso(timeutil.now("UTC"))

    ev_clause = " OR ".join("lower(title) LIKE lower(?)" for _ in terms)
    event = conn.execute(
        f"""SELECT * FROM events WHERE deleted_at IS NULL AND ({ev_clause})
              ORDER BY (starts_at < ?), starts_at LIMIT 1""",  # noqa: S608
        (*[f"%{t}%" for t in terms], now_utc),
    ).fetchone()

    rm_clause = " OR ".join("lower(body) LIKE lower(?)" for _ in terms)
    reminder = conn.execute(
        f"""SELECT * FROM reminders WHERE status IN ('pending','firing') AND ({rm_clause})
              ORDER BY (fire_at < ?), fire_at LIMIT 1""",  # noqa: S608
        (*[f"%{t}%" for t in terms], now_utc),
    ).fetchone()

    if event and reminder:
        # Whichever comes up sooner is the one being talked about.
        return (
            ("events", dict(event))
            if event["starts_at"] <= reminder["fire_at"]
            else ("reminders", dict(reminder))
        )
    if event:
        return "events", dict(event)
    if reminder:
        return "reminders", dict(reminder)
    return None


def reschedule(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    found = _find_match(conn, args["what"])
    if found is None:
        return f"I couldn't find anything about {args['what']} to move."

    table, row = found
    new_time = timeutil.to_utc_iso(args["new_time"])
    column = "starts_at" if table == "events" else "fire_at"
    label = row["title"] if table == "events" else row["body"]

    mutations.update(conn, utterance_id, table, row["id"], {column: new_time})
    when = timeutil.speak_datetime(
        new_time, tz_name, bool(row.get("all_day")) if table == "events" else False
    )
    return f"Moved {label} to {when}."


def cancel(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    found = _find_match(conn, args["what"])
    if found is None:
        return f"I couldn't find anything about {args['what']} to cancel."

    table, row = found
    if table == "events":
        mutations.soft_delete(conn, utterance_id, "events", row["id"])
        return f"Cancelled {row['title']}."
    # reminders have no deleted_at — status is the soft-delete mechanism, and
    # routing it through update() keeps it logged and undoable.
    mutations.update(conn, utterance_id, "reminders", row["id"], {"status": "cancelled"})
    return f"Cancelled the reminder to {row['body']}."


def undo_last(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    undone = mutations.undo_last(conn)
    if undone is None:
        return "There's nothing to undo."

    label = {"events": "event", "reminders": "reminder", "notes": "note"}.get(
        undone["table"], undone["table"]
    )
    if undone["op"] == "insert":
        return f"Undone — I removed that {label}."
    if undone["op"] == "delete":
        return f"Undone — I restored that {label}."
    return f"Undone — I reverted that {label}."


# ── reads ─────────────────────────────────────────────────


def agenda_rows(conn, tz_name: str, days: int) -> dict:
    start, end = timeutil.window_utc(tz_name, days)
    events = conn.execute(
        """SELECT id, title, starts_at, ends_at, all_day, location FROM events
             WHERE deleted_at IS NULL AND starts_at >= ? AND starts_at < ?
             ORDER BY starts_at""",
        (start, end),
    ).fetchall()
    reminders = conn.execute(
        """SELECT id, body, fire_at, status FROM reminders
             WHERE status IN ('pending','firing') AND fire_at >= ? AND fire_at < ?
             ORDER BY fire_at""",
        (start, end),
    ).fetchall()
    return {
        "events": [dict(r) for r in events],
        "reminders": [dict(r) for r in reminders],
    }


def _join(parts: list[str]) -> str:
    """Natural spoken list: 'a', 'a and b', 'a, b, and c'."""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _day_window(args: dict, tz_name: str) -> tuple[str, str, str] | None:
    """Resolve the router's date_from/date_to into a UTC range plus a spoken
    label. Returns None if the dates are missing or unparseable, which sends
    the caller to the model instead of guessing."""
    raw_from = (args.get("date_from") or "").strip()
    raw_to = (args.get("date_to") or raw_from).strip()
    if not raw_from:
        return None
    try:
        zone = timeutil.zone(tz_name)
        start = datetime.strptime(raw_from, "%Y-%m-%d").replace(tzinfo=zone)
        end_day = datetime.strptime(raw_to, "%Y-%m-%d").replace(tzinfo=zone)
    except ValueError:
        return None

    end = end_day + timedelta(days=1)  # inclusive of the last day
    today = timeutil.now(tz_name).date()
    delta = (start.date() - today).days
    if start.date() == end_day.date():
        label = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(
            delta, start.strftime("%A")
        )
    else:
        label = "Coming up"
    return timeutil.to_utc_iso(start), timeutil.to_utc_iso(end), label


def _answer_agenda(conn, args: dict, tz_name: str) -> str | None:
    window = _day_window(args, tz_name)
    if window is None:
        return None
    start, end, label = window

    events = conn.execute(
        """SELECT title, starts_at, all_day, location FROM events
             WHERE deleted_at IS NULL AND starts_at >= ? AND starts_at < ?
             ORDER BY starts_at""",
        (start, end),
    ).fetchall()
    reminders = conn.execute(
        """SELECT body, fire_at FROM reminders
             WHERE status IN ('pending','firing') AND fire_at >= ? AND fire_at < ?
             ORDER BY fire_at""",
        (start, end),
    ).fetchall()

    if not events and not reminders:
        return f"{label} looks clear."

    # Single day: the sentence already names it ("Tomorrow you have…"), so
    # speak only the clock time. Multi-day: each item needs its own day.
    single_day = label != "Coming up"

    def phrase(value: str, all_day: bool = False) -> str:
        if all_day:
            return "all day" if single_day else timeutil.speak_datetime(value, tz_name, True)
        if single_day:
            return f"at {timeutil.speak_time(timeutil.to_local(value, tz_name))}"
        return timeutil.speak_datetime(value, tz_name)

    parts: list[str] = []
    for e in events:
        where = f" at {e['location']}" if e["location"] else ""
        parts.append(f"{e['title']} {phrase(e['starts_at'], bool(e['all_day']))}{where}")
    for r in reminders:
        parts.append(f"a reminder to {r['body']} {phrase(r['fire_at'])}")

    return f"{label} you have {_join(parts)}."


def _answer_when(conn, args: dict, tz_name: str) -> str | None:
    subject = (args.get("subject") or "").strip()
    if not subject:
        return None
    found = _find_match(conn, subject)
    if found is None:
        return f"I don't have anything about {subject}."

    table, row = found
    if table == "events":
        when = timeutil.speak_datetime(row["starts_at"], tz_name, bool(row["all_day"]))
        where = f" at {row['location']}" if row["location"] else ""
        return f"{row['title']} is {when}{where}."
    return f"You have a reminder to {row['body']} {timeutil.speak_datetime(row['fire_at'], tz_name)}."


def _answer_recall(conn, args: dict, tz_name: str) -> str | None:
    subject = (args.get("subject") or args.get("question") or "").strip()
    notes = _search_notes(conn, subject, limit=3)
    if not notes:
        return None  # let the model try; it may reword the question usefully
    if len(notes) == 1:
        return f"You noted: {notes[0]['body']}"
    bodies = "; ".join(n["body"] for n in notes[:3])
    return f"You noted: {bodies}"


def query(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    # Fast path: the router already told us the question's shape in the call
    # we had to make anyway, so common questions are answered by formatting
    # rows in Python — no second model hop. Each templater returns None when
    # it can't answer confidently, which falls through to the model rather
    # than guessing.
    kind = (args.get("kind") or "other").strip()
    templated = {
        "agenda": _answer_agenda,
        "when": _answer_when,
        "recall": _answer_recall,
    }.get(kind)
    if templated is not None:
        answer = templated(conn, args, tz_name)
        if answer:
            return answer

    # Floor the window at 8 days regardless of what the router asked for.
    # The window starts at *today's* midnight, so window_days=1 — which the
    # router naturally picks for "what's on tomorrow" — produces a window that
    # ends before tomorrow begins, and the answer comes back as a confident
    # "nothing scheduled". Eight days also covers "next <weekday>", which can
    # be up to 13 days out. Over-fetching costs a few context tokens; under-
    # fetching costs a wrong answer.
    days = max(8, int(args.get("window_days") or 7))
    agenda = agenda_rows(conn, tz_name, days)

    lines: list[str] = []
    for e in agenda["events"]:
        when = timeutil.speak_datetime(e["starts_at"], tz_name, bool(e["all_day"]))
        loc = f" at {e['location']}" if e["location"] else ""
        lines.append(f"EVENT: {e['title']} — {when}{loc}")
    for r in agenda["reminders"]:
        lines.append(
            f"REMINDER: {r['body']} — {timeutil.speak_datetime(r['fire_at'], tz_name)}"
        )

    # Notes are searched, not windowed — "what did I say about Sarah" has no
    # time bound. FTS5 first; fall back to LIKE when the question tokenizes to
    # nothing useful (punctuation, stopwords).
    notes = _search_notes(conn, args["question"])
    for n in notes:
        lines.append(f"NOTE: {n['body']}")

    if not lines:
        lines.append("(nothing stored in this window)")

    return router.answer(args["question"], "\n".join(lines), tz_name)


def _search_notes(conn, question: str, limit: int = 10) -> list[dict]:
    terms = [w for w in "".join(c if c.isalnum() else " " for c in question).split() if len(w) > 2]
    if not terms:
        return []
    try:
        # Soft-deleted notes stay in the FTS index (the update trigger fires,
        # not the delete trigger), so the join to notes is what filters them.
        rows = conn.execute(
            """SELECT n.body FROM notes_fts f
                 JOIN notes n ON n.id = f.rowid
                 WHERE notes_fts MATCH ? AND n.deleted_at IS NULL
                 ORDER BY rank LIMIT ?""",
            (" OR ".join(terms), limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if rows:
        return [dict(r) for r in rows]

    clause = " OR ".join("body LIKE ?" for _ in terms)
    return [
        dict(r)
        for r in conn.execute(
            f"""SELECT body FROM notes WHERE deleted_at IS NULL AND ({clause})
                  ORDER BY id DESC LIMIT ?""",  # noqa: S608
            (*[f"%{t}%" for t in terms], limit),
        ).fetchall()
    ]


# `escalate` is handled in main.py — it enqueues a job rather than writing a
# domain row, so it doesn't share this signature.
FAST_HANDLERS = {
    "add_event": add_event,
    "add_reminder": add_reminder,
    "add_note": add_note,
    "reschedule": reschedule,
    "cancel": cancel,
    "undo_last": undo_last,
    "query": query,
}
