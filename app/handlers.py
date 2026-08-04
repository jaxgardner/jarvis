"""Tool handlers.

Confirmations are **templated, not generated** — once the router has given us
add_reminder(body=..., fire_at=...), the reply is a formatting problem, not a
model problem. Deterministic, and it saves a round trip against the 2s budget.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta

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


# Spoken counts, so the reply reads as speech rather than as a scoreboard.
_GRATITUDE_WORDS = {1: "one", 2: "two", 3: "three"}
_GRATITUDE_ORDINALS = {
    4: "a fourth",
    5: "a fifth",
    6: "a sixth",
    7: "another one",
}


def log_gratitude(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    """Record what the user is grateful for.

    Three is a target, not a limit: a fourth thing is stored and the day still
    reads complete. Turning down gratitude because a counter is full would be
    the same pedantry `consume_item` already declines when it lists an unknown
    food rather than complaining the pantry row is missing.
    """
    from gratitude import entries

    _, total = entries.add(conn, utterance_id, args.get("items") or [], tz_name)

    if total > entries.TARGET:
        which = _GRATITUDE_ORDINALS.get(total, "another one")
        return f"That's {which} — logged."
    if total == entries.TARGET:
        return "Three for today. Done."

    left = entries.TARGET - total
    more = "One more" if left == 1 else f"{_GRATITUDE_WORDS[left].capitalize()} more"
    return f"That's {_GRATITUDE_WORDS[total]} down. {more} when you're ready."


def consume_item(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    """Record food used up.

    `amount` is deliberately coarse: present means a partial, absent means
    finished. See `inventory.consume` for why there is no fractional model.

    A name that matches nothing is not an error. Refusing to add milk to the
    list because the fridge row is missing is the assistant being pedantic
    about its own bookkeeping — the useful half still happens.
    """
    from pantry import inventory

    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("consume_item needs a name")

    partial = bool((args.get("amount") or "").strip())
    item = inventory.find(conn, name)

    if item is None:
        inventory.add_to_list(conn, utterance_id, name, "out")
        return f"I don't have {name} in the pantry, but I've added it to the list."

    inventory.consume(conn, utterance_id, item, partial)
    if partial:
        return f"Noted — some of the {item['name']} used, still marked as on hand."
    return f"Got it — {item['name']} is finished, and it's on the shopping list."


def add_item(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    """Record food that is now in the house.

    Straight to `active` with no review, unlike the receipt path. The review
    screen guards against a *model* having misread a photograph; here the user
    is stating what they have, so there is nothing to second-guess. The date
    still comes from the shelf-life table and is marked `default`, which the
    fridge list renders as "estimated".
    """
    from pantry import inventory, shelflife

    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("add_item needs a name")

    category = shelflife.guess_category(name)
    location = args.get("location")
    if location not in shelflife.LOCATIONS:
        location = shelflife.DEFAULT_LOCATION.get(category, "pantry")

    today = timeutil.now(tz_name).date().isoformat()
    expires_on = shelflife.expires_on(category, location, today)

    inventory.add_item(
        conn,
        utterance_id,
        name,
        category=category,
        location=location,
        expires_on=expires_on,
        quantity=args.get("quantity"),
        unit=(args.get("unit") or "").strip() or None,
    )

    where = "the fridge" if location == "fridge" else f"the {location}"
    if expires_on is None:
        return f"Added {name} to {where}."
    days = (date.fromisoformat(expires_on) - date.fromisoformat(today)).days
    if days <= 1:
        return f"Added {name} to {where} — use it by tomorrow."
    return f"Added {name} to {where}, good for about {days} days."


def add_to_list(conn, utterance_id: int, args: dict, tz_name: str) -> str:
    from pantry import inventory

    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("add_to_list needs a name")

    added = inventory.add_to_list(conn, utterance_id, name, "manual")
    if added is None:
        return f"{name.capitalize()} is already on the list."
    return f"Added {name} to the list."


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


# ── notification actions ──────────────────────────────────
# Driven by the buttons on a fired reminder, not by an utterance — hence
# utterance_id=None. They still go through `mutations`, so a fat-fingered
# Snooze on a lock screen is undoable like everything else.


def snooze(conn, reminder_id: int, minutes: int, tz_name: str) -> str | None:
    """Push a fired reminder out and put it back in the queue.

    Recurrence is deliberately dropped from the snoozed row. The scheduler
    already inserted the next occurrence when this one fired; leaving the rule
    attached would make the snoozed copy insert a *second* one when it fires
    again, and recurring reminders would quietly multiply.
    """
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    if row is None:
        return None

    fire_at = timeutil.to_utc_iso(
        timeutil.now("UTC") + timedelta(minutes=max(1, min(minutes, 24 * 60)))
    )
    mutations.update(
        conn,
        None,
        "reminders",
        reminder_id,
        {"fire_at": fire_at, "status": "pending", "fired_at": None, "recurrence": None},
    )
    return f"Snoozed — I'll remind you again {timeutil.speak_datetime(fire_at, tz_name)}."


def ack(conn, reminder_id: int) -> str | None:
    """Mark a reminder dealt with. This is the only thing that ever writes
    'acked' — the status migration 002 declared and nothing has set until now.
    """
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    if row is None:
        return None
    if row["status"] == "acked":
        return "Already done."  # idempotent: a double-tap is not an error
    mutations.update(conn, None, "reminders", reminder_id, {"status": "acked"})
    return "Done."


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

    # Notes matched — but so might mail, and then the templated note answer
    # would be a confident wrong one. "Did Sarah email me?" asked by someone
    # who also has a note mentioning Sarah must not come back as "You noted:
    # …". Hand both to the model instead of picking the wrong source.
    if search_email(conn, subject, limit=1):
        return None

    if len(notes) == 1:
        return f"You noted: {notes[0]['body']}"
    bodies = "; ".join(n["body"] for n in notes[:3])
    return f"You noted: {bodies}"


# Words meaning "the whole inventory" rather than one specific food.
_PANTRY_ALL = {"fridge", "freezer", "pantry", "kitchen", "food", "groceries"}
_PANTRY_LIST = {"shopping list", "list", "store", "shopping"}


def _answer_pantry(conn, args: dict, tz_name: str) -> str | None:
    """Food questions, answered by formatting rows rather than calling a model.

    Returns None when it cannot answer confidently — an empty pantry is not
    the same as a confident "you have nothing", and falling through lets the
    model see the full context first. Same contract as the other templaters.
    """
    from pantry import inventory

    subject = (args.get("subject") or "").strip().lower()

    if subject in _PANTRY_LIST:
        listed = inventory.open_list(conn)
        if not listed:
            return "There's nothing on the shopping list."
        return "On the list: " + _join([entry["name"] for entry in listed]) + "."

    location = subject if subject in ("fridge", "freezer", "pantry") else None
    items = inventory.active(conn, location)
    if not items:
        return None

    if subject and subject not in _PANTRY_ALL:
        # "do we have eggs" — a question about one thing.
        matches = [
            item
            for item in items
            if subject in item["name"].lower()
            or subject in (item["category"] or "").lower()
        ]
        if not matches:
            return f"I don't have {subject} in the pantry."
        first = matches[0]
        days = first["days_left"]
        if days is None:
            return f"Yes — {first['name']}."
        if days < 0:
            return f"Yes, but the {first['name']} was due {_days_phrase(-days)} ago."
        return f"Yes — {first['name']}, good for another {_days_phrase(days)}."

    # The whole inventory. Lead with what is dying, and cap the list: this is
    # spoken aloud, and nobody wants forty items read at them.
    soonest = [item for item in items if item["days_left"] is not None][:5]
    if not soonest:
        return "Nothing in the pantry has a date on it."
    parts = [
        f"{item['name']}, {_days_phrase(item['days_left'])}"
        if item["days_left"] >= 0
        else f"{item['name']}, overdue"
        for item in soonest
    ]
    tail = "" if len(items) <= len(soonest) else f", plus {len(items) - len(soonest)} more"
    return "Expiring soonest: " + _join(parts) + tail + "."


def _days_phrase(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "1 day"
    return f"{days} days"


# Enough to answer a follow-up, bounded so a 14 KB report cannot crowd out the
# notes and mail that the same question may also need.
_REPORT_FALLBACK_CHARS = 1500


def _report_line(conn, job_id) -> str | None:
    """One context line about a report, or None when there is nothing to say.

    Prefers the stored summary and falls back to the head of the report
    itself, which is what lets reports that finished before summaries existed
    still answer questions.
    """
    if job_id is None:
        return None
    row = conn.execute(
        "SELECT prompt, summary, result FROM jobs WHERE id = ?", (int(job_id),)
    ).fetchone()
    if row is None:
        return None
    body = (row["summary"] or "").strip() or (row["result"] or "").strip()
    if not body:
        return None
    return f"REPORT ({row['prompt']}): {body[:_REPORT_FALLBACK_CHARS]}"


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
        "pantry": _answer_pantry,
    }.get(kind)
    if templated is not None:
        answer = templated(conn, args, tz_name)
        if answer:
            return answer

    # A named report leads the context. Seeded here rather than appended below
    # so it survives whatever else the question turns up.
    lines: list[str] = []
    report = _report_line(conn, args.get("job_id"))
    if report:
        lines.append(report)

    # Floor the window at 8 days regardless of what the router asked for.
    # The window starts at *today's* midnight, so window_days=1 — which the
    # router naturally picks for "what's on tomorrow" — produces a window that
    # ends before tomorrow begins, and the answer comes back as a confident
    # "nothing scheduled". Eight days also covers "next <weekday>", which can
    # be up to 13 days out. Over-fetching costs a few context tokens; under-
    # fetching costs a wrong answer.
    days = max(8, int(args.get("window_days") or 7))
    agenda = agenda_rows(conn, tz_name, days)

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

    # Mail, same treatment. Metadata and Google's snippet only — enough to
    # answer "did the landlord write back" by quoting what actually arrived,
    # and not enough to invent anything, because there is no body to embroider.
    for m in search_email(conn, args["question"], limit=6):
        when = timeutil.speak_datetime(m["received_at"], tz_name)
        unread = " [unread]" if m["is_unread"] else ""
        lines.append(
            f"EMAIL{unread}: from {m['sender'] or 'unknown'} {when} — "
            f"{m['subject'] or '(no subject)'}: {m['snippet'] or ''}"
        )

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


_EMAIL_COLUMNS = "sender, subject, snippet, received_at, is_unread"


def search_email(conn, question: str, limit: int = 6) -> list[dict]:
    """Search ingested mail. Same two-stage shape as _search_notes.

    Unlike notes, email rows are hard-deleted when they age out, so the FTS
    index needs no deleted_at join to stay honest — the join back to
    email_messages is only there for the columns FTS doesn't store.

    Returns [] when the table doesn't exist yet, so every caller keeps working
    on a database that hasn't had migration 006 applied.
    """
    terms = [
        w for w in "".join(c if c.isalnum() else " " for c in question).split()
        if len(w) > 2
    ]
    if not terms:
        return []
    try:
        rows = conn.execute(
            f"""SELECT {_EMAIL_COLUMNS} FROM email_fts f
                  JOIN email_messages e ON e.id = f.rowid
                  WHERE email_fts MATCH ?
                  ORDER BY rank LIMIT ?""",  # noqa: S608 — a fixed column list
            (" OR ".join(terms), limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if rows:
        return [dict(r) for r in rows]

    clause = " OR ".join(
        "(subject LIKE ? OR snippet LIKE ? OR sender LIKE ?)" for _ in terms
    )
    values: list[str] = []
    for term in terms:
        values.extend([f"%{term}%"] * 3)
    try:
        return [
            dict(r)
            for r in conn.execute(
                f"""SELECT {_EMAIL_COLUMNS} FROM email_messages WHERE {clause}
                      ORDER BY received_at DESC LIMIT ?""",  # noqa: S608
                (*values, limit),
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return []


# ── proposals ─────────────────────────────────────────────


def accept_proposal(conn, proposal_id: int, tz_name: str) -> str | None:
    """Turn a reviewed proposal into a real event.

    This one write DOES go through the mutations helper, unlike everything else
    ingestion does. The distinction is not about where the data came from — it
    is that a human pressed Accept, which makes this a user action, and user
    actions are the thing /undo exists to reverse.
    """
    row = conn.execute(
        "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return None
    if row["status"] != "pending":
        return f"That one was already {row['status']}."

    payload = json.loads(row["payload_json"])
    # The extractor drops these before they ever become proposals, so reaching
    # here means a hand-written row or a schema change. Answer rather than 500:
    # this endpoint is driven by a button on a phone.
    raw_start = payload.get("starts_at")
    try:
        starts_at = timeutil.to_utc_iso(raw_start) if raw_start else None
    except ValueError:
        starts_at = None
    if starts_at is None:
        return "That one has no usable time — I can't put it on the calendar."

    values = {
        "title": (payload.get("title") or "Untitled").strip(),
        "starts_at": starts_at,
        "all_day": int(bool(payload.get("all_day"))),
        # 'email', not 'calendar': this did not come from Google Calendar, and
        # labelling it so would make the calendar ingester's dedupe treat it as
        # a row it owns and is entitled to overwrite.
        "source": "email",
    }
    if payload.get("ends_at"):
        values["ends_at"] = timeutil.to_utc_iso(payload["ends_at"])
    if payload.get("location"):
        values["location"] = payload["location"].strip()

    event_id = mutations.insert(conn, None, "events", values)
    conn.execute(
        """UPDATE proposals
             SET status = 'accepted',
                 decided_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                 event_id = ?
             WHERE id = ?""",
        (event_id, proposal_id),
    )
    when = timeutil.speak_datetime(
        values["starts_at"], tz_name, bool(values["all_day"])
    )
    return f"Added {values['title']}, {when}."


def reject_proposal(conn, proposal_id: int) -> str | None:
    """Decline a proposal. Nothing is written to `events`, and the ingester
    will not offer this message again — `candidates()` skips any message with a
    proposals row of any status."""
    row = conn.execute(
        "SELECT status FROM proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        return None
    if row["status"] != "pending":
        return f"That one was already {row['status']}."
    conn.execute(
        """UPDATE proposals
             SET status = 'rejected',
                 decided_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
             WHERE id = ?""",
        (proposal_id,),
    )
    return "Dismissed."


def pending_proposals(conn, limit: int, tz_name: str) -> list[dict]:
    """The review queue, with the payload already unpacked for a phone.

    `title` and `when` are rendered here rather than shipped as a JSON blob for
    the client to parse, for the same reason /agenda renders `when`: two
    implementations of "tomorrow at 3 PM" drift, and the one you would trust is
    the one you can't see. `payload_json` is still returned intact — accepting
    is what writes the event, and the raw payload is what it writes.
    """
    rows = conn.execute(
        """SELECT id, source, kind, summary, confidence, payload_json, created_at
             FROM proposals WHERE status = 'pending'
             ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    proposals = []
    for row in rows:
        item = dict(row)
        try:
            payload = json.loads(item["payload_json"])
        except (TypeError, ValueError):
            payload = {}
        item["title"] = (payload.get("title") or "Untitled").strip()
        item["location"] = payload.get("location")
        starts_at = payload.get("starts_at")
        try:
            item["when"] = (
                timeutil.speak_datetime(starts_at, tz_name, bool(payload.get("all_day")))
                if starts_at
                else "no time given"
            )
        except ValueError:
            # A hand-written row, or a payload from before the extractor
            # normalised times. Accept() answers the same way rather than 500ing.
            item["when"] = "no usable time"
        proposals.append(item)
    return proposals


# ── replying to a report ──────────────────────────────────
# A reply re-queues the job it belongs to rather than inserting a new one.
# Deliberately not routed through app.mutations: jobs are operational rows,
# and /undo exists to reverse things you said to the assistant, not to
# un-send an answer to one of its questions.


def recent_reports(conn, limit: int = 10) -> list[dict]:
    """The reports the router is shown, newest first.

    Finished only: a queued or running job cannot be resumed and has nothing
    to say yet. Ten is a guess, and it is the number that decides what is
    reachable by voice — everything older is still on the Reports screen with
    a reply box.
    """
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, prompt FROM jobs
                 WHERE status IN ('done','failed')
                 ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ]


def reply_to_job(conn, job_id: int, text: str) -> str:
    """Queue `text` as the next input to an already-finished job.

    Returns "ok", "missing" (no such job), or "live" (still queued or
    running, so its session cannot be resumed).
    """
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return "missing"
    if row["status"] in ("queued", "running"):
        return "live"

    # `result` is deliberately left alone: you keep reading the old report
    # while the rerun works, and the worker overwrites it on finish.
    # `attempts` resets because MAX_ATTEMPTS counts across the life of the
    # row — a job already at 2 would give your reply no retries at all.
    conn.execute(
        """UPDATE jobs SET pending_input = ?, status = 'queued', attempts = 0,
                           error = NULL, started_at = NULL, finished_at = NULL
             WHERE id = ?""",
        (text.strip(), job_id),
    )
    return "ok"


# `escalate` is handled in main.py — it enqueues a job rather than writing a
# domain row, so it doesn't share this signature.
FAST_HANDLERS = {
    "add_event": add_event,
    "add_reminder": add_reminder,
    "add_note": add_note,
    "log_gratitude": log_gratitude,
    "consume_item": consume_item,
    "add_item": add_item,
    "add_to_list": add_to_list,
    "reschedule": reschedule,
    "cancel": cancel,
    "undo_last": undo_last,
    "query": query,
}
