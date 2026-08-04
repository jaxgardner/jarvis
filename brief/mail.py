"""What arrived overnight that you would want to know about.

One Haiku call a day over the snippets of yesterday's unread mail. This is the
second metered call in a system otherwise funded by the Claude Code
subscription — the first being `app.reports.summarize` — and for the same
reason: there is a human-shaped judgement to make and no human awake to make
it.

**Snippets only, never bodies.** `email_messages` holds Google's own snippet
and nothing more, because `format=metadata` means Gmail never returns a body.
So this cannot embroider: everything it says came from text Gmail chose to
show, and the worst failure available is a dull summary rather than an
invented one.

Deliberately NOT the proposal extractor reborn. That asked one model call per
message to decide whether something belonged on a calendar, and nothing it
produced ever reached `events` without a human accepting it. This asks one
call for the whole morning and produces prose that is read aloud and then
forgotten. Nothing here writes to a domain table.
"""

from app import router

# Yesterday's mail, not the backlog. 866 unread messages is a filing decision
# nobody made; the brief is about what changed overnight.
WINDOW_HOURS = 24

# Bounds the call on a bad day — a newsletter blast should not turn one Haiku
# call into a large one. Newest first, so what gets dropped is the oldest of
# an already-busy night.
MAX_MESSAGES = 60

# A hung call must not hold the 7am job past the hour it is named after.
TIMEOUT_SECONDS = 30.0

_SYSTEM = """\
You are writing the mail half of someone's morning brief. You will be given \
the sender, subject and Gmail snippet of every unread message that arrived in \
the last day.

Answer in two or three plain sentences. Lead with anything that needs a \
decision or has a date attached — a confirmation, an appointment, a reply \
someone is waiting on, a delivery arriving, a bill. Name senders and dates \
specifically: "the dentist confirmed Thursday at 3", not "you have an \
appointment".

Most mornings, most of a mailbox is job alerts, newsletters and marketing. \
Do not dress those up as news. Say how much of it there was and what kind, in \
one clause, and move on: "Thirty-one messages, nearly all job alerts and \
retail marketing." That is a complete and useful answer on a quiet morning.

Always write a sentence. A morning with nothing important in it is worth \
saying out loud — it is the difference between a quiet inbox and a broken \
assistant, and only you can tell the listener which one they woke up to.

This is read aloud. Plain sentences only: no markdown, no lists, no bullet \
points, no emoji, no subject lines quoted verbatim, no ISO timestamps."""


def unread_since(conn, cutoff_iso: str, limit: int = MAX_MESSAGES) -> list[dict]:
    """Unread mail newer than the cutoff, newest first."""
    return [
        dict(row)
        for row in conn.execute(
            """SELECT sender, subject, snippet, received_at
                 FROM email_messages
                WHERE is_unread = 1 AND received_at >= ?
             ORDER BY received_at DESC
                LIMIT ?""",
            (cutoff_iso, limit),
        ).fetchall()
    ]


def as_prompt(messages: list[dict]) -> str:
    """The model's input. One block per message, snippet included.

    The count is stated rather than left to be counted. "Thirty-one messages,
    nearly all job alerts" is the shape of a good quiet-morning answer, and
    models are unreliable at counting a list they are also reading.
    """
    lines = [f"{len(messages)} unread messages arrived since yesterday:\n"]
    for message in messages:
        lines.append(
            f"From: {message['sender'] or 'unknown'}\n"
            f"Subject: {message['subject'] or '(no subject)'}\n"
            f"{message['snippet'] or ''}".strip()
        )
    return "\n\n".join(lines)


def summarize(messages: list[dict]) -> str | None:
    """Two or three sentences on the morning's mail, or None.

    Never raises. None means only two things — no mail at all, or the call
    failed. It deliberately does *not* mean "nothing important": the model is
    told to always write a sentence, because a quiet morning and a broken
    assistant are indistinguishable from silence, and only one of them is fine.

    Both None cases have the same remedy: leave the line out and let the
    agenda answer alone.
    """
    if not messages:
        return None
    try:
        response = router._client().messages.create(
            model=router.MODEL,
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": as_prompt(messages)}],
            timeout=TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — see docstring; nothing here is fatal
        return None
    # No usage.record(): there is no utterance behind the brief, so it stays
    # out of /metrics the same way report summaries and receipts do.
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None
