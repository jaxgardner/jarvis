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
import httpx

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

# The same property on every tool that can file something under a project.
# One object referenced five times rather than five copies: they must read
# identically, and a description that drifts on one tool is a routing bug you
# find months later in exactly one phrasing.
_PROJECT_ID = {
    "type": "integer",
    "description": (
        "The id of a project from PROJECTS, ONLY when the user names that "
        "project in this sentence: 'for the lettuce project, …', 'add this "
        "to the remodel', 'on the greenhouse, …'.\n"
        "Never infer a project from subject matter. If the user did not say "
        "which project this belongs to, omit this parameter — even when the "
        "content is obviously related to one. A note about a broken "
        "thermostat is NOT filed under a gardening project just because both "
        "concern plants. Being unfiled is correct and normal; guessing wrong "
        "puts the thought somewhere the user will not look for it.\n"
        "Use an id only if it appears in PROJECTS."
    ),
}

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
                "project_id": _PROJECT_ID,
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
                "project_id": _PROJECT_ID,
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
                "project_id": _PROJECT_ID,
                "person": {"type": "string", "description": "Person the note is about."},
            },
            "required": ["body"],
        },
    },
    {
        "name": "start_project",
        "description": (
            "Begin a new project — a named space for one thing the user is "
            "working on. Use when they say to start, begin or set up a "
            "project: 'start a project on hydroponic lettuce', 'set up a "
            "project for the kitchen remodel'.\n"
            "If the same sentence also asks for research, reading or "
            "investigation — 'and find out what it takes', 'and see what's "
            "involved', 'let me know what you find' — put that in "
            "research_task and it runs as deep work under the new project. "
            "Without a research ask, the project is simply created.\n"
            "This tool is only for creating a project. Filing something under "
            "one that already exists is add_note, add_event or add_reminder "
            "with project_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "What the project is called, in a few words.",
                },
                "description": {
                    "type": "string",
                    "description": "One line saying what it is, if they said more.",
                },
                "research_task": {
                    "type": "string",
                    "description": (
                        "The research to start now, restated clearly and "
                        "self-containedly. Omit if they only asked to start "
                        "the project."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "log_gratitude",
        "description": (
            "Record what the user is grateful or thankful for. Use whenever "
            "they name something they are grateful for, thankful for, or "
            "glad about: 'I'm grateful for the sun and Emma calling', "
            "'thankful my sister rang', 'today I was glad about the quiet'. "
            "Each thing they name is its own item.\n"
            "The user must actually SAY they are grateful, thankful or glad. "
            "A sentence that merely reports something pleasant — 'my sister "
            "called', 'the meeting went well' — is add_note, not this. And an "
            "instruction to note or remember something is ALWAYS add_note, "
            "however nice the thing is: 'note that my sister called' is a "
            "note even though 'I'm thankful my sister called' is a gratitude."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Each thing named, as its own short string, in the "
                        "order it was said. One to three."
                    ),
                }
            },
            "required": ["items"],
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
            "question — do not use this tool for it. Use it whenever the "
            "answer is not already in front of you: when the CONTEXT block "
            "holds the note being asked about, `answer` is the tool, and this "
            "one only repeats a search that has already happened."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "job_id": {
                    "type": "integer",
                    "description": (
                        "The id of a report from REPORTS, when the question is "
                        "about what one of them found — 'what did that report "
                        "say about pricing', 'what did you find out'. Omit "
                        "otherwise. Use an id only if it appears in REPORTS."
                    ),
                },
                "project_id": _PROJECT_ID,
                "kind": {
                    "type": "string",
                    "enum": [
                        "agenda",
                        "when",
                        "recall",
                        "pantry",
                        "brief",
                        "project",
                        "message",
                        "call",
                        "other",
                    ],
                    "description": (
                        "The shape of the question. 'agenda' = what is "
                        "happening in a date range ('what's on tomorrow', "
                        "'what does my week look like'). 'when' = the time of "
                        "one specific known thing ('when is my dentist "
                        "appointment'). 'recall' = retrieving a stored fact "
                        "('what did I say about Sarah', 'what's the wifi "
                        "password'). 'pantry' = food in the house or the "
                        "shopping list ('what's in the fridge', 'do we have "
                        "eggs', 'what do I need at the store'). 'brief' = a "
                        "rundown of the whole day rather than a list of "
                        "appointments ('what've I got going on today', 'brief "
                        "me', 'catch me up', 'what's my day look like', "
                        "'anything I should know about'). Prefer 'brief' over "
                        "'agenda' when the question is broad enough to want "
                        "the morning's mail in the answer. 'project' = where "
                        "something the user is working on stands, or what "
                        "they are working on at all ('where am I on the "
                        "lettuce project', 'what am I working on', 'catch me "
                        "up on the remodel'); set project_id when they named "
                        "one. A question about a text message or a phone call "
                        "is kind='message' or kind='call' ('did Sarah text "
                        "back', 'what did she say', 'did I miss a call', "
                        "'who called'). 'other' = "
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
                "job_id": {
                    "type": "integer",
                    "description": (
                        "The id of an existing report from the REPORTS list, "
                        "when the user is answering it or asking it to carry "
                        "on — 'go with the second one', 'answer the vendor "
                        "report with B', 'go deeper on that'. Omit for new "
                        "work. Use an id only if it appears in REPORTS."
                    ),
                },
                "project_id": _PROJECT_ID,
            },
            "required": ["restated_task"],
        },
    },
    {
        "name": "answer",
        "description": (
            "Answer a question directly, out loud, from the TODAY or CONTEXT "
            "blocks in the system prompt. Use it whenever either of them "
            "already contains what was asked — TODAY holds the day's mail "
            "summary, the calendar, reminders due, food about to spoil and "
            "reports just finished; CONTEXT holds notes and mail already "
            "searched for this utterance, so a question about a stored note "
            "whose text is sitting in CONTEXT is answered here rather than "
            "searched for again. If neither block bears on the question, use "
            "query. This is the one tool whose output is spoken to the user "
            "verbatim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": (
                        "The spoken answer. One or two plain sentences, no "
                        "markdown, no lists, no emoji, times said the way a "
                        "person would say them."
                    ),
                }
            },
            "required": ["reply"],
        },
    },
]

# The cacheable prefix is tools, then system, in that order — so everything up
# to the first `cache_control` marker has to be byte-stable.
#
# This used to be impossible: the system prompt carried the datetime on its
# third line, so every byte after it differed on each call, leaving the tools
# as the only candidate. The tools measure 4199 tokens against Haiku 4.5's
# documented 4096 floor and **caching still did not fire** — probed directly,
# both counters came back 0, while the same probe padded to 7240 tokens cached
# 6912 immediately. The cache measures a smaller prefix than `count_tokens`
# reports, and 103 tokens of headroom was not enough.
#
# Splitting the prompt into a static half and a live tail fixed it. Measured:
# the prefix is 5000 tokens by `count_tokens`, the cache reports **4676**
# written then read back on every subsequent call. So the real threshold sits
# between 4199 and 4676.
#
# What it buys is spend, not speed: 1229ms -> 1153ms median, inside the noise,
# because a Haiku call has a ~660ms floor that prefill is a small share of.
# `tests/test_router_prompt.py` asserts the behaviour — declare cache_control
# and it must produce an actual cache read.

# Byte-stable. Nothing derived from the clock, the database or the request may
# appear here; it all belongs in _SYSTEM_LIVE, below the cache breakpoint.
_SYSTEM_STATIC = """\
You are the router for a personal assistant. You choose exactly one tool and \
fill in its arguments.

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
- Something the user is grateful, thankful or glad about -> log_gratitude, \
one item per thing they name.
- Food being used up or run out -> consume_item. "we're out of X", "finished \
the X" are statements about the kitchen, not requests to buy.
- Food arriving in the house -> add_item. "we have X now", "put X in the \
freezer". The opposite of consume_item, and still not a request to buy.
- Something to buy, with no claim about what you have -> add_to_list.
- Starting a new named space for something the user is working on -> \
start_project. If the same sentence asks for research, put it in \
research_task rather than escalating separately.
- Filing something under a project that already exists -> the normal tool for \
what it is, with project_id set from PROJECTS.
- What to cook, or a recipe from what is in the house -> escalate.
- A question the TODAY or CONTEXT block below already answers -> answer, \
putting the spoken sentence in `reply`. This is the only tool that talks to \
the user.
- Any other question about stored information -> query.
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
Only reach for query when they are actually asking you something.

Using `answer`:
- Prefer it whenever TODAY below contains what was asked for. "What's my \
morning brief", "what have I got going on today", "what's on tomorrow", \
"anything I need to deal with", "when is my dentist appointment" are all \
`answer` when the relevant line is in TODAY. Do not route these to query \
merely because they are questions.
- CONTEXT below, when it is there, is the archive already searched for this \
utterance. If a line in it answers the question, that is `answer` as well — \
the search has happened and query would only repeat it.
- But only when TODAY or CONTEXT actually holds it. TODAY is the day, not the \
archive, and CONTEXT is whatever happened to match the words you were given. \
If neither has a line bearing on the question, use query. An absent or \
unhelpful CONTEXT means the search still has to happen — never that there is \
nothing to find.
- One or two sentences, spoken to someone who cannot see a screen: no \
markdown, no lists, no emoji, no ISO timestamps. Say times the way a person \
would.
- Never mention where it came from. No "based on your data", no "it looks \
like". Say the answer directly, as something you know.
- Summarise and draw the obvious conclusion rather than reading rows aloud.\
"""

# Everything derived from the clock, the database or the request. Sits AFTER
# the cache breakpoint, so none of it invalidates the cached prefix.
_SYSTEM_LIVE = """\

Current date and time: {now_iso}
Timezone: {tz_name}
Today is: {weekday}

CALENDAR — copy dates from this table. Do not calculate them yourself.
{calendar}
{today}{reports}{projects}{context}\
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


# Sixty characters is enough to tell two reports apart and short enough that
# ten rows stay under about 200 tokens — which keeps the whole prompt under
# Haiku's 4096-token minimum cacheable prefix, so the "caching does not fire
# here" note in CLAUDE.md stays true.
_REPORT_PROMPT_CHARS = 60


def reports_table(reports) -> str:
    """The REPORTS block body. Empty string when there is nothing to list."""
    lines = []
    for report in reports:
        ask = " ".join(str(report["prompt"]).split())
        if len(ask) > _REPORT_PROMPT_CHARS:
            ask = ask[: _REPORT_PROMPT_CHARS - 1].rstrip() + "…"
        lines.append(f"  {report['id']:<5} {ask}")
    return "\n".join(lines)


def projects_table(projects) -> str:
    """The PROJECTS block body. Empty string when there is nothing to list."""
    return "\n".join(
        f"  {project['id']:<5} {' '.join(str(project['name']).split())}"
        for project in projects
    )


def system_blocks(
    tz_name: str, reports=(), projects=(), today: str = "", context: str = ""
) -> list[dict]:
    """The system prompt as two blocks, with the cache breakpoint between them.

    The marker goes on the static block, so the cached prefix is tools plus
    that block and nothing after it. Everything live — the clock, the
    calendar table, TODAY, REPORTS, PROJECTS, CONTEXT — is in the second
    block, which is re-read every call and costs a few hundred tokens of
    prefill.
    """
    return [
        {
            "type": "text",
            "text": _SYSTEM_STATIC,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": _live_half(tz_name, reports, projects, today, context)},
    ]


def system_prompt(
    tz_name: str, reports=(), projects=(), today: str = "", context: str = ""
) -> str:
    """The same prompt as one string. What the model sees is `system_blocks`;
    this is for tests and for counting tokens."""
    return _SYSTEM_STATIC + _live_half(tz_name, reports, projects, today, context)


def _live_half(
    tz_name: str, reports=(), projects=(), today: str = "", context: str = ""
) -> str:
    local = timeutil.now(tz_name)
    table = reports_table(reports)
    # Omitted entirely rather than rendered empty — an empty table invites the
    # model to invent an id.
    block = (
        "\nREPORTS — the user's recent deep reports. Refer to one by its id.\n"
        f"{table}\n"
        if table
        else ""
    )
    project_table = projects_table(projects)
    project_block = (
        "\nPROJECTS — the user's active projects. Refer to one by its id.\n"
        f"{project_table}\n"
        if project_table
        else ""
    )
    # Omitted entirely when the day is empty, for the same reason the REPORTS
    # table is: a heading with nothing under it invites the model to answer
    # from it anyway.
    today_block = (
        "\nTODAY — the day as it stands right now. Answer from this with the "
        "`answer` tool when it is enough.\n"
        f"{today}\n"
        if today.strip()
        else ""
    )
    # CONTEXT last, and only when it has something in it.
    #
    # This is the one question-derived block in the prompt. TODAY above it is
    # built before the utterance is read, which is what makes TODAY safe;
    # this is not, so it is labelled as candidates rather than answers and
    # the rules below tell the model what to do when it does not contain
    # what was asked for.
    context_block = (
        "\nCONTEXT — notes and mail that mention words from this utterance. "
        "These are candidates, not answers. If one of them answers the "
        "question, use `answer` and say it directly. If none of them does, "
        "call `query` — do not answer from this block by guessing, and never "
        "say the question cannot be answered just because it is not here.\n"
        f"{context}\n"
        if context.strip()
        else ""
    )
    return _SYSTEM_LIVE.format(
        now_iso=local.isoformat(timespec="seconds"),
        tz_name=tz_name,
        weekday=local.strftime("%A, %B %-d, %Y"),
        calendar=calendar_table(local),
        today=today_block,
        reports=block,
        projects=project_block,
        context=context_block,
    )


_CLIENT: anthropic.Anthropic | None = None


def _client() -> anthropic.Anthropic:
    """One client for the process lifetime, holding one warm connection.

    Constructing a client per call throws away the HTTP connection pool, so
    every request pays a fresh TCP + TLS handshake to api.anthropic.com — pure
    overhead on a path with a two-second budget. The daemon is long-lived, so
    the pool should be too.

    A single client is not enough on its own, though: httpx expires idle
    connections after five seconds by default and the SDK does not override
    it, so an assistant spoken to every few minutes re-handshakes on every
    single request anyway. Measured from the Mini that is only about 15ms —
    small, but it is pure waste and this is the one line that removes it.
    `DefaultHttpxClient` rather than a bare `httpx.Client` so the SDK's own
    timeout and retry defaults survive.
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(
            api_key=config.anthropic_api_key(),
            http_client=anthropic.DefaultHttpxClient(
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                    keepalive_expiry=300.0,
                )
            ),
        )
    return _CLIENT


def route(
    text: str, tz_name: str, reports=(), projects=(), today: str = "", context: str = ""
) -> tuple[str, dict]:
    """Classify one utterance. Returns (tool_name, tool_input).

    `reports`, `projects`, `today` and `context` are passed in rather than
    read here: this module makes model calls and formats prompts, and giving
    it a database connection would make it impossible to test either without
    one.
    """
    response = _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_blocks(tz_name, reports, projects, today, context),
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
    """Second hop for `query`: turn rows into one spoken sentence.

    The instruction not to mention the context is load-bearing, and so is the
    absence of a `Data:` label on the user turn. The first version said
    "answer using only the data provided" over a message beginning "Data:",
    and the model dutifully echoed both: real answers opened "Based on the
    data provided, you're in the consideration phase of…". Grounding it and
    telling it where the grounding came from are different instructions, and
    only the first one is wanted out loud.
    """
    response = _client().messages.create(
        model=MODEL,
        max_tokens=512,
        system=(
            "You are a personal assistant answering your user out loud. "
            "Everything you know is below. Do not invent facts that are not "
            "there, but do summarise it and draw the obvious conclusion from "
            "it — a note saying what they were last thinking about is an "
            "answer to where they are. Only say you don't know when nothing "
            "below bears on the question.\n"
            "Never mention where it came from. No 'based on the data "
            "provided', no 'the data shows', no 'according to your notes', no "
            "'it looks like'. Say the answer directly, as something you know.\n"
            "One or two sentences, spoken to someone who cannot see a screen: "
            "no markdown, no lists, no emoji, no ISO timestamps — say times "
            "the way a person would. Current time: "
            f"{timeutil.now(tz_name).isoformat(timespec='minutes')}"
        ),
        messages=[{"role": "user", "content": f"{context}\n\nQuestion: {question}"}],
    )
    usage.record(response.usage)
    return "".join(b.text for b in response.content if b.type == "text").strip()
