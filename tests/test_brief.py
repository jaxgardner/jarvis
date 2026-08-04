"""The morning brief.

The stored half is the mail summary and nothing else. Everything these tests
assert about staleness follows from that: a brief written at 7am cannot
recite a 9am standup at four in the afternoon, because the calendar half was
never stored to go stale.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "brief.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


@pytest.fixture
def pushes(monkeypatch):
    from app import notify

    sent = []
    monkeypatch.setattr(
        notify, "push", lambda body, **kwargs: sent.append((body, kwargs)) or True
    )
    return sent


def rows(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def mail_in(db, subject, hours_ago=2, unread=1, sender="landlord@example.com"):
    from app.db import transaction

    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with transaction() as conn:
        conn.execute(
            """INSERT INTO email_messages
                 (external_id, sender, subject, snippet, received_at, is_unread)
               VALUES (?,?,?,?,?,?)""",
            (
                f"msg-{subject}-{hours_ago}",
                sender,
                subject,
                f"snippet for {subject}",
                when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                unread,
            ),
        )


# ── the window ────────────────────────────────────────────


def test_only_unread_mail_is_read(db):
    from app.db import connect
    from brief import mail

    mail_in(db, "unread one")
    mail_in(db, "already read", unread=0)

    conn = connect()
    try:
        got = mail.unread_since(conn, "2000-01-01T00:00:00Z")
    finally:
        conn.close()

    assert [m["subject"] for m in got] == ["unread one"]


def test_the_backlog_is_left_alone(db):
    """866 unread messages is a filing decision nobody made. The brief is
    about what changed overnight, not about the pile."""
    from app.db import connect
    from brief import mail

    mail_in(db, "last night", hours_ago=3)
    mail_in(db, "last month", hours_ago=24 * 30)

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = connect()
    try:
        got = mail.unread_since(conn, cutoff)
    finally:
        conn.close()

    assert [m["subject"] for m in got] == ["last night"]


def test_the_prompt_carries_sender_subject_and_snippet(db):
    from brief import mail

    text = mail.as_prompt(
        [{"sender": "dentist@example.com", "subject": "Confirming Thursday", "snippet": "3pm"}]
    )
    assert "dentist@example.com" in text
    assert "Confirming Thursday" in text
    assert "3pm" in text


def test_an_empty_inbox_costs_nothing(db, monkeypatch):
    """No messages must not mean a model call with an empty prompt."""
    from brief import mail

    called = []
    monkeypatch.setattr(
        mail.router, "_client", lambda: called.append(1) or (_ for _ in ()).throw(AssertionError)
    )
    assert mail.summarize([]) is None
    assert called == []


def test_a_quiet_morning_is_still_a_sentence(db, monkeypatch):
    """The first version let the model reply NOTHING when the mail was all
    newsletters, and on a real mailbox that fired on day one: 29 messages, no
    summary, no push. A quiet inbox and a broken assistant look identical
    from the outside, so the model always writes a sentence and only an empty
    mailbox or a failed call produces None."""
    from brief import mail

    fake = _fake_client("Twenty-nine messages, nearly all job alerts.")
    monkeypatch.setattr(mail.router, "_client", lambda: fake)

    summary = mail.summarize([{"sender": "a", "subject": "b", "snippet": "c"}])
    assert summary == "Twenty-nine messages, nearly all job alerts."


def test_the_prompt_states_the_message_count(db):
    """Models are unreliable at counting a list they are also reading."""
    from brief import mail

    text = mail.as_prompt([{"sender": "a", "subject": "b", "snippet": "c"}] * 3)
    assert text.startswith("3 unread messages")


def test_a_failed_call_is_not_fatal(db, monkeypatch):
    from brief import mail

    class Exploding:
        @property
        def messages(self):
            raise RuntimeError("anthropic is down")

    monkeypatch.setattr(mail.router, "_client", lambda: Exploding())
    assert mail.summarize([{"sender": "a", "subject": "b", "snippet": "c"}]) is None


def _recorder(seen):
    """Stand in for router.answer, capturing the context it was handed.

    A function rather than a lambda: `seen.setdefault(k, v) or "spoken"`
    returns the context, because a non-empty context is truthy.
    """

    def answer(question, context, tz_name):
        seen["context"] = context
        seen["question"] = question
        return "spoken"

    return answer


def _fake_client(text):
    class Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class Messages:
        def create(self, **kwargs):
            class Response:
                content = [Block(text)]

            return Response()

    class Client:
        messages = Messages()

    return Client()


# ── the job ───────────────────────────────────────────────


def test_the_job_stores_the_summary(db, monkeypatch):
    from brief import mail, run

    mail_in(db, "the lease")
    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")

    result = run.generate("America/Denver")

    assert result["generated"] is True
    stored = rows(db, "SELECT brief_on, mail_summary, message_count FROM briefs")
    assert len(stored) == 1
    assert stored[0]["mail_summary"] == "Your landlord replied."
    assert stored[0]["message_count"] == 1


def test_the_job_runs_once_a_day(db, monkeypatch):
    """launchd retrying after a failure must not produce two calls."""
    from brief import mail, run

    calls = []
    monkeypatch.setattr(mail, "summarize", lambda messages: calls.append(1) or "once")

    assert run.generate("America/Denver")["generated"] is True
    assert run.generate("America/Denver")["generated"] is False
    assert len(calls) == 1


def test_force_regenerates_in_place(db, monkeypatch):
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "first")
    run.generate("America/Denver")
    monkeypatch.setattr(mail, "summarize", lambda messages: "second")
    run.generate("America/Denver", force=True)

    stored = rows(db, "SELECT mail_summary FROM briefs")
    assert stored == [{"mail_summary": "second"}]


def test_a_quiet_morning_still_writes_a_row(db, monkeypatch):
    """The row means 'the job ran today'. Without it the job retries every
    time launchd wakes it, re-paying for the same quiet morning."""
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: None)
    run.generate("America/Denver")

    assert rows(db, "SELECT mail_summary FROM briefs") == [{"mail_summary": None}]


# ── the push ──────────────────────────────────────────────


def test_the_push_says_it_is_ready_not_what_it_says(db, pushes, monkeypatch):
    """A brief is a paragraph and iOS truncates it. The notification's job is
    to be tapped."""
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")
    mail_in(db, "the lease")
    run.generate("America/Denver")

    assert run.push("America/Denver") is True
    body, kwargs = pushes[0]
    assert "landlord" not in body
    assert kwargs["category"] == "BRIEF"
    assert kwargs["data"] == {"kind": "brief"}


def test_an_empty_day_is_not_pushed(db, pushes, monkeypatch):
    """Nothing on the calendar and nothing in the mail is how a useful prompt
    becomes a muted one."""
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: None)
    run.generate("America/Denver")

    assert run.push("America/Denver") is False
    assert pushes == []


def test_a_day_with_only_calendar_is_still_pushed(db, pushes, monkeypatch):
    from app.db import transaction
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: None)
    with transaction() as conn:
        conn.execute(
            """INSERT INTO events (title, starts_at)
                 VALUES ('standup', strftime('%Y-%m-%dT%H:%M:%SZ','now','+2 hours'))"""
        )
    run.generate("America/Denver")

    assert run.push("America/Denver") is True


def test_a_row_written_before_seven_does_not_eat_the_push(db, pushes, monkeypatch):
    """The bug this test exists for: the push used to be reachable only from
    the branch that generated the row, so anything that wrote today's row
    early — a manual run, a `--force`, a retry — left the 7am job saying
    "already ran today" and pushing nothing. The row means the summary
    exists, not that you were told about it."""
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")
    mail_in(db, "the lease")

    run.generate("America/Denver")  # the 2am run that wrote the row
    assert pushes == []

    monkeypatch.setattr(run.sys, "argv", ["brief.run"])
    run.main()  # the 7am job, finding the row already there

    assert len(pushes) == 1


def test_one_push_a_morning(db, pushes, monkeypatch):
    """launchd retrying, a late catch-up run after a sleeping machine, and a
    manual run must between them produce one notification."""
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")
    mail_in(db, "the lease")
    run.generate("America/Denver")

    assert run.push("America/Denver") is True
    assert run.push("America/Denver") is False
    assert len(pushes) == 1


def test_a_push_that_went_nowhere_is_not_recorded_as_delivered(db, monkeypatch):
    """`notify.push` returns False when no device is registered, which on
    APNs alone is a real state. Stamping it would claim a delivery that did
    not happen and burn the day's only notification."""
    from app import notify
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")
    mail_in(db, "the lease")
    run.generate("America/Denver")

    monkeypatch.setattr(notify, "push", lambda body, **kwargs: False)
    assert run.push("America/Denver") is False

    sent = []
    monkeypatch.setattr(
        notify, "push", lambda body, **kwargs: sent.append(body) or True
    )
    assert run.push("America/Denver") is True
    assert len(sent) == 1


# ── the voice answer ──────────────────────────────────────


def test_the_brief_line_is_offered_to_the_answering_model(db, monkeypatch):
    from app import handlers
    from app.db import connect, transaction
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")
    mail_in(db, "the lease")
    run.generate("America/Denver")

    with transaction() as conn:
        conn.execute(
            """INSERT INTO events (title, starts_at)
                 VALUES ('standup', strftime('%Y-%m-%dT%H:%M:%SZ','now','+2 hours'))"""
        )

    seen = {}
    monkeypatch.setattr(handlers.router, "answer", _recorder(seen))

    conn = connect()
    try:
        answer = handlers.query(
            conn, None, {"question": "what've I got going on today", "kind": "brief"},
            "America/Denver",
        )
    finally:
        conn.close()

    assert answer == "spoken"
    assert "MAIL THIS MORNING: Your landlord replied." in seen["context"]
    # The calendar half is read live rather than recited from the brief.
    assert "EVENT: standup" in seen["context"]


def test_a_missing_brief_leaves_the_agenda_to_answer_alone(db, monkeypatch):
    """No row is normal and permanent — the machine may have been asleep at
    7. It must not turn into a missing answer."""
    from app import handlers
    from app.db import connect

    conn = connect()
    try:
        assert handlers._brief_line(conn, "America/Denver") is None
    finally:
        conn.close()


def test_an_agenda_question_picks_up_the_brief_too(db, monkeypatch):
    """The router can reasonably call 'what's on today' either kind. Both
    have to reach the mail, or the answer depends on a coin flip."""
    from app import handlers
    from app.db import connect
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")
    mail_in(db, "the lease")
    run.generate("America/Denver")

    seen = {}
    monkeypatch.setattr(handlers.router, "answer", _recorder(seen))

    conn = connect()
    try:
        handlers.query(
            conn, None, {"question": "what's on today", "kind": "agenda"}, "America/Denver"
        )
    finally:
        conn.close()

    assert "MAIL THIS MORNING" in seen["context"]


def test_expiring_food_and_finished_reports_reach_the_brief(db, monkeypatch):
    from app import handlers
    from app.db import connect, transaction

    with transaction() as conn:
        conn.execute(
            """INSERT INTO pantry_items (name, status, expires_on)
                 VALUES ('spinach','active', date('now','+1 day'))"""
        )
        conn.execute(
            """INSERT INTO jobs (prompt, status, finished_at)
                 VALUES ('compare the three vendors','done',
                         strftime('%Y-%m-%dT%H:%M:%SZ','now','-2 hours'))"""
        )

    conn = connect()
    try:
        lines = handlers._needs_doing(conn, "America/Denver")
    finally:
        conn.close()

    assert any("spinach" in line for line in lines)
    assert any("vendors" in line for line in lines)


def test_a_when_question_does_not_drag_in_the_brief(db, monkeypatch):
    """'When is my dentist appointment' wants a time, not a rundown."""
    from app import handlers
    from app.db import connect
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")
    mail_in(db, "the lease")
    run.generate("America/Denver")

    conn = connect()
    try:
        assert handlers._brief_line(conn, "America/Denver") is not None
        lines = handlers._needs_doing(conn, "America/Denver")
    finally:
        conn.close()
    assert lines == []


# ── answered in one call ──────────────────────────────────


def test_today_block_carries_the_three_parts_of_a_brief(db, monkeypatch):
    """What the router is handed so it can answer without a second call: the
    stored mail summary, the live calendar, and what is waiting on you."""
    from app import handlers
    from app.db import connect, transaction
    from brief import mail, run

    monkeypatch.setattr(mail, "summarize", lambda messages: "Your landlord replied.")
    mail_in(db, "the lease")
    run.generate("America/Denver")

    with transaction() as conn:
        conn.execute(
            """INSERT INTO events (title, starts_at)
                 VALUES ('standup', strftime('%Y-%m-%dT%H:%M:%SZ','now','+2 hours'))"""
        )
        conn.execute(
            """INSERT INTO pantry_items (name, status, expires_on)
                 VALUES ('spinach','active', date('now','+1 day'))"""
        )

    conn = connect()
    try:
        block = handlers.today_block(conn, "America/Denver")
    finally:
        conn.close()

    assert "MAIL THIS MORNING: Your landlord replied." in block
    assert "EVENT: standup" in block
    assert "EXPIRING: spinach" in block


def test_an_empty_day_produces_an_empty_block(db):
    """Nothing stored must not become a TODAY heading with nothing under it."""
    from app import handlers
    from app.db import connect

    conn = connect()
    try:
        assert handlers.today_block(conn, "America/Denver") == ""
    finally:
        conn.close()


def test_the_spoken_answer_is_one_flat_line(db):
    """`/say` promises a single plain-text string safe to hand to a TTS
    engine. A newline in the tool argument would be spoken as a pause that is
    not in the sentence."""
    from app import handlers

    reply = handlers.answer(
        None, 1, {"reply": "You have a dentist appointment\ntomorrow  at 8 AM."}, "America/Denver"
    )
    assert reply == "You have a dentist appointment tomorrow at 8 AM."


def test_an_empty_answer_does_not_reach_the_user_as_silence(db):
    from app import handlers

    assert handlers.answer(None, 1, {"reply": "   "}, "America/Denver")
    assert handlers.answer(None, 1, {}, "America/Denver")
