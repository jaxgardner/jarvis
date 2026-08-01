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

from app import config, timeutil, usage

MODEL = "claude-haiku-4-5"

# USD per million tokens, for the model above. Stored here rather than in the
# database because prices change and token counts don't — /metrics multiplies
# at read time, so a price change re-costs history correctly instead of
# freezing whatever the rate happened to be on the day.
PRICE_PER_MTOK = {"input": 1.00, "output": 5.00}


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * PRICE_PER_MTOK["input"]
        + output_tokens * PRICE_PER_MTOK["output"]
    ) / 1_000_000

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
        "name": "consume_item",
        "description": (
            "Record that food has been used up or is running out. Use whenever "
            "the user STATES that something in the kitchen is finished or "
            "partly used: 'we're out of milk', 'finished the spinach', 'used "
            "half the chicken', 'the eggs are gone'. This is a statement about "
            "food, not a request to buy — if they ask you to put something on "
            "the shopping list, use add_to_list instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The food, in a word or two. e.g. 'milk', 'spinach'.",
                },
                "amount": {
                    "type": "string",
                    "description": (
                        "Only when they used PART of it — 'half', 'some', 'most'. "
                        "Omit entirely when it is finished or they are out."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "add_item",
        "description": (
            "Record that food is now IN the house. Use when the user says "
            "they have something or are putting it away: 'add a gallon of "
            "milk to the fridge', 'I've got two chicken breasts in the "
            "freezer', 'we have eggs now'. This is the opposite of "
            "consume_item. It is NOT a request to buy — 'we need milk' is "
            "add_to_list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The food, in a word or two. e.g. 'whole milk'.",
                },
                "quantity": {"type": "number", "description": "How many, if said."},
                "unit": {
                    "type": "string",
                    "description": "e.g. 'gal', 'lb', 'ct'. Omit if not said.",
                },
                "location": {
                    "type": "string",
                    "enum": ["fridge", "freezer", "pantry"],
                    "description": "Only if the user says where it is going.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "add_to_list",
        "description": (
            "Put something on the shopping list without claiming anything "
            "about what is in the kitchen. Use when the user asks to buy or "
            "add something: 'add paper towels to the list', 'we need "
            "batteries', 'remember to buy stamps'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "What to buy."},
            },
            "required": ["name"],
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
                "kind": {
                    "type": "string",
                    "enum": ["agenda", "when", "recall", "pantry", "other"],
                    "description": (
                        "The shape of the question. 'agenda' = what is "
                        "happening in a date range ('what's on tomorrow', "
                        "'what does my week look like'). 'when' = the time of "
                        "one specific known thing ('when is my dentist "
                        "appointment'). 'recall' = retrieving a stored fact "
                        "('what did I say about Sarah', 'what's the wifi "
                        "password'). 'pantry' = food in the house or the "
                        "shopping list ('what's in the fridge', 'do we have "
                        "eggs', 'what do I need at the store'). 'other' = "
                        "anything needing reasoning, counting, or comparison "
                        "across items."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": (
                        "For 'when', 'recall' and 'pantry': the thing being "
                        "asked about, as a few keywords. e.g. 'dentist', "
                        "'Sarah', 'wifi', 'eggs', 'fridge', 'shopping list'."
                    ),
                },
                "date_from": {
                    "type": "string",
                    "description": (
                        "For 'agenda': first day to include, YYYY-MM-DD, taken "
                        "from the calendar table. Today's date for 'today'."
                    ),
                },
                "date_to": {
                    "type": "string",
                    "description": (
                        "For 'agenda': last day to include, YYYY-MM-DD, "
                        "inclusive. Same as date_from for a single day."
                    ),
                },
                "window_days": {
                    "type": "integer",
                    "description": "How many days ahead are relevant. Default 7.",
                },
            },
            "required": ["question", "kind"],
        },
    },
    {
        # Not in the original tool table, but the router test set requires
        # it: "dentist moved to friday" is annotated "update, not
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
- Food being used up or run out -> consume_item. "we're out of X", "finished \
the X" are statements about the kitchen, not requests to buy.
- Food arriving in the house -> add_item. "we have X now", "put X in the \
freezer". The opposite of consume_item, and still not a request to buy.
- Something to buy, with no claim about what you have -> add_to_list.
- What to cook, or a recipe from what is in the house -> escalate.
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
    # Start at 0, not 1. Starting at tomorrow omits today's own weekday from
    # the table, so "Friday at 6" said on a Friday morning resolved to next
    # Friday — the model had no row saying Friday could mean today.
    for offset in range(0, 7):
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


_CLIENT: anthropic.Anthropic | None = None


def _client() -> anthropic.Anthropic:
    """One client for the process lifetime.

    Constructing a client per call throws away the HTTP connection pool, so
    every request pays a fresh TCP + TLS handshake to api.anthropic.com — pure
    overhead on a path with a two-second budget. The daemon is long-lived, so
    the pool should be too.
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=config.anthropic_api_key())
    return _CLIENT


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
    usage.record(response.usage)
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
    usage.record(response.usage)
    return "".join(b.text for b in response.content if b.type == "text").strip()
