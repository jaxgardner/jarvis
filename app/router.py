"""The Haiku router.

The router *is* the tool choice. `tool_choice: {"type": "any"}` forces a tool
call on every turn, so there is never free text to parse and no separate
classifier model.

On caching: the prompt and tool definitions are kept byte-stable, but note
that Haiku 4.5's minimum cacheable prefix is 4096 tokens and this prompt is
well under it — nothing is actually being cached today. Stability costs
nothing and starts paying if the prompt grows.
"""

from datetime import datetime, timedelta

import anthropic

from app import config, timeutil

MODEL = "claude-haiku-4-5"

TOOLS: list[dict] = [
    {
        "name": "add_event",
        "description": (
            "Record something happening at a specific time — an appointment, "
            "meeting, or scheduled commitment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What it is, in a few words."},
                "starts_at": {
                    "type": "string",
                    "description": "Absolute ISO 8601 with offset, e.g. 2026-07-30T15:00:00-06:00",
                },
                "ends_at": {"type": "string", "description": "Absolute ISO 8601 with offset."},
                "location": {"type": "string"},
                "all_day": {"type": "boolean"},
            },
            "required": ["title", "starts_at"],
        },
    },
    {
        "name": "add_reminder",
        "description": (
            "Set a reminder to fire at a time. Use when the user wants to be "
            "prompted later, rather than to record a scheduled event."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "What to remind them of."},
                "fire_at": {
                    "type": "string",
                    "description": "Absolute ISO 8601 with offset.",
                },
                "recurrence": {
                    "type": "string",
                    "description": "Optional: 'daily', or 'weekly:MO,WE'.",
                },
            },
            "required": ["body", "fire_at"],
        },
    },
    {
        "name": "add_note",
        "description": (
            "Remember a fact, thought, or list item. Use for anything worth "
            "recalling later that isn't tied to a time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string"},
                "person": {"type": "string", "description": "Person the note is about."},
            },
            "required": ["body"],
        },
    },
    {
        "name": "query",
        "description": (
            "Answer a QUESTION about what is already stored — the schedule, "
            "notes, or people. The user must be asking for information: "
            "'what's on tomorrow', 'when is my dentist appointment', 'what did "
            "I say about Sarah'. A statement that something changed is not a "
            "question — do not use this tool for it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "window_days": {
                    "type": "integer",
                    "description": "How many days ahead are relevant. Default 7.",
                },
            },
            "required": ["question"],
        },
    },
    {
        # Not in the design doc's tool table, but its own Phase 1 test set
        # requires it: "dentist moved to friday" is annotated "update, not
        # duplicate insert", and with only add_* tools the router's best
        # available move is the duplicate.
        "name": "reschedule",
        "description": (
            "Change the time of something already recorded. Use whenever the "
            "user STATES that an existing thing is now at a different time. "
            "These are all reschedules, not questions and not new items: "
            "'dentist moved to friday', 'lunch is now at 1', 'push the meeting "
            "to thursday', 'the call got bumped to tomorrow'. Terse phrasing "
            "like '<thing> moved to <time>' is the common case. Never add a "
            "second copy of something that already exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    "description": "Words identifying the existing item, e.g. 'dentist'.",
                },
                "new_time": {
                    "type": "string",
                    "description": "Absolute ISO 8601 with offset.",
                },
            },
            "required": ["what", "new_time"],
        },
    },
    {
        # Also absent from the doc's table; required by "cancel the dentist
        # reminder".
        "name": "cancel",
        "description": (
            "Cancel or delete something already recorded. Use when the user "
            "says to cancel, delete, or remove an existing event or reminder. "
            "For undoing a mistake you just made, use undo_last instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    "description": "Words identifying the existing item.",
                }
            },
            "required": ["what"],
        },
    },
    {
        "name": "undo_last",
        "description": (
            "Reverse the most recent change. Use when the user says to undo, "
            "cancel that, never mind, or that something was misheard."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "escalate",
        "description": (
            "Hand off to the slower deep agent. Use for anything needing "
            "research, web access, multi-step work, or writing files — "
            "anything a single database write cannot satisfy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "restated_task": {
                    "type": "string",
                    "description": "The task restated clearly and self-containedly.",
                },
                "is_follow_up": {
                    "type": "boolean",
                    "description": (
                        "True when this refers back to a deep task already done "
                        "— 'what did you find about X', 'go deeper on that', "
                        "'what about the second one'. Lets the agent resume the "
                        "earlier conversation instead of starting cold."
                    ),
                },
            },
            "required": ["restated_task"],
        },
    },
]

# Byte-stable except for the datetime block, which necessarily varies.
_SYSTEM = """\
You are the router for a personal assistant. You never talk to the user — you \
only choose exactly one tool and fill in its arguments.

Current date and time: {now_iso}
Timezone: {tz_name}
Today is: {weekday}

CALENDAR — copy dates from this table. Do not calculate them yourself.
{calendar}

Resolving times is the most important thing you do:
- Every time you emit MUST be absolute ISO 8601 with an offset. Never a \
relative phrase, never a bare date for something that has a time.
- For any weekday name, take the date from the table above verbatim. Use the \
"next" column only when the user actually says "next".
- If today is the named weekday, it means today when the time has not yet \
passed, otherwise the date in the table.
- If a time of day is not given: reminders default to 9:00 AM, events to 12:00 PM.

Choosing the tool:
- Something at a set time the user is attending -> add_event.
- Something the user wants to be prompted about -> add_reminder.
- A fact to retain, with no time -> add_note.
- A question about stored information -> query.
- An existing thing MOVING to a different time -> reschedule. Never add a \
second copy of something that already exists.
- An existing thing being called off -> cancel.
- Undoing or correcting the previous turn -> undo_last. Prefer this over \
cancel when the user is fixing something you just misheard.
- Anything requiring research, the web, or multi-step work -> escalate.

The input is dictated speech, so it may be lightly garbled. Interpret it \
charitably.

One distinction is worth being careful about, because it is easy to get \
backwards: a STATEMENT that something changed is not a QUESTION about it. \
"dentist moved to friday" is the user telling you a fact — reschedule it. \
Only reach for query when they are actually asking you something.\
"""


def calendar_table(local: datetime) -> str:
    """Pre-computed weekday -> date lookup for the system prompt.

    Haiku reliably gets weekday arithmetic wrong — asked on Thursday
    2026-07-30, it returned 2026-08-01 for "Friday" (the correct answer is
    07-31), consistently and across phrasings. Handing it a lookup table
    removes the arithmetic instead of asking it to be more careful.
    """
    lines = [
        f"  today          {local:%A} {local:%Y-%m-%d}",
        f"  tomorrow       {local + timedelta(days=1):%A} {local + timedelta(days=1):%Y-%m-%d}",
        "",
        "  weekday        soonest       when preceded by \"next\"",
    ]
    for offset in range(1, 8):
        day = local + timedelta(days=offset)
        following = day + timedelta(days=7)
        # Pad the weekday name separately — a width spec inside a datetime
        # format string is read as literal strftime text, not alignment.
        lines.append(
            f"  {day.strftime('%A'):<14} {day:%Y-%m-%d}    {following:%Y-%m-%d}"
        )
    return "\n".join(lines)


def system_prompt(tz_name: str) -> str:
    local = timeutil.now(tz_name)
    return _SYSTEM.format(
        now_iso=local.isoformat(timespec="seconds"),
        tz_name=tz_name,
        weekday=local.strftime("%A, %B %-d, %Y"),
        calendar=calendar_table(local),
    )


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=config.anthropic_api_key())


def route(text: str, tz_name: str) -> tuple[str, dict]:
    """Classify one utterance. Returns (tool_name, tool_input)."""
    response = _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt(tz_name),
        tools=TOOLS,
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": text}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return block.name, dict(block.input)
    # tool_choice=any makes this unreachable in practice; treat it as a bug
    # rather than silently guessing an intent.
    raise RuntimeError(f"router returned no tool_use (stop_reason={response.stop_reason})")


def answer(question: str, context: str, tz_name: str) -> str:
    """Second hop for `query`: turn rows into one spoken sentence."""
    response = _client().messages.create(
        model=MODEL,
        max_tokens=512,
        system=(
            "Answer the question using only the data provided. One or two "
            "sentences, spoken aloud to someone who cannot see a screen: no "
            "markdown, no lists, no emoji, no ISO timestamps — say times the "
            "way a person would. If the data does not contain the answer, say "
            f"so plainly. Current time: {timeutil.now(tz_name).isoformat(timespec='minutes')}"
        ),
        messages=[
            {"role": "user", "content": f"Data:\n{context}\n\nQuestion: {question}"}
        ],
    )
    return "".join(b.text for b in response.content if b.type == "text").strip()
