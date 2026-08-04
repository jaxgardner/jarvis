"""jarvis-mcp — stdio MCP server over the database.

The shared seam. The deep-path worker uses it, and anything else that needs
structured access to the same data uses it too, rather than reimplementing
queries against the schema. Keep it small.

Every write goes through app.mutations, exactly like the fast path — the agent
writing to your calendar is logged and undoable on the same terms as your
voice. That is what makes it safe to let it write at all.

    uv run python -m mcp_server.server        # stdio; run by the worker
"""

import json
import os

# mcp 2.x: FastMCP was replaced by MCPServer. Same decorator API.
from mcp.server import MCPServer

from app import handlers, mutations, timeutil
from app.config import DEFAULT_TZ
from app.db import connect, transaction
from mcp_server.search import search_notes as _search_notes

mcp = MCPServer("jarvis")


def _utterance_id() -> int | None:
    """Link writes back to the utterance that spawned the job, so the audit
    trail survives the hop into the deep path."""
    raw = os.getenv("JARVIS_UTTERANCE_ID", "").strip()
    return int(raw) if raw.isdigit() else None


@mcp.tool()
def search_notes(query: str, limit: int = 10) -> str:
    """Search stored notes by keyword. Returns the most relevant notes."""
    conn = connect()
    try:
        results = _search_notes(conn, query, limit)
    finally:
        conn.close()
    if not results:
        return f"No notes matching {query!r}."
    return json.dumps(results, indent=2)


@mcp.tool()
def search_email(query: str, limit: int = 10) -> str:
    """Search ingested email by keyword — sender, subject, and Google's snippet.

    Metadata only. Message bodies are never stored, so this can tell you that
    an email arrived and roughly what it said, but not its full contents.
    """
    conn = connect()
    try:
        results = handlers.search_email(conn, query, max(1, min(limit, 50)))
    finally:
        conn.close()
    if not results:
        return f"No email matching {query!r}."
    return json.dumps(results, indent=2)


@mcp.tool()
def get_agenda(days: int = 7) -> str:
    """Events and pending reminders in the next N days."""
    start, end = timeutil.window_utc(DEFAULT_TZ, max(1, min(days, 365)))
    conn = connect()
    try:
        events = conn.execute(
            """SELECT id, title, starts_at, ends_at, all_day, location FROM events
                 WHERE deleted_at IS NULL AND starts_at >= ? AND starts_at < ?
                 ORDER BY starts_at""",
            (start, end),
        ).fetchall()
        reminders = conn.execute(
            """SELECT id, body, fire_at FROM reminders
                 WHERE status = 'pending' AND fire_at >= ? AND fire_at < ?
                 ORDER BY fire_at""",
            (start, end),
        ).fetchall()
    finally:
        conn.close()

    payload = {
        "events": [
            {**dict(e), "when": timeutil.speak_datetime(
                e["starts_at"], DEFAULT_TZ, bool(e["all_day"]))}
            for e in events
        ],
        "reminders": [
            {**dict(r), "when": timeutil.speak_datetime(r["fire_at"], DEFAULT_TZ)}
            for r in reminders
        ],
    }
    return json.dumps(payload, indent=2)


@mcp.tool()
def pantry_inventory(location: str | None = None) -> str:
    """What food is in the house, soonest to expire first, plus the shopping list.

    Use this before suggesting anything to cook. Lead with what expires
    soonest — the point of a suggestion is usually to stop something being
    thrown away.

    `location` optionally narrows to 'fridge', 'freezer' or 'pantry'.
    """
    from pantry import inventory

    conn = connect()
    try:
        items = inventory.active(conn, location)
        listed = inventory.open_list(conn)
    finally:
        conn.close()

    lines: list[str] = []
    if not items:
        lines.append("Nothing in the pantry.")
    else:
        lines.append("IN THE HOUSE (soonest to expire first):")
        for item in items:
            days = item["days_left"]
            if days is None:
                when = "no expiry"
            elif days < 0:
                when = f"{-days} days overdue"
            elif days == 0:
                when = "expires today"
            elif days == 1:
                when = "1 day left"
            else:
                when = f"{days} days left"
            quantity = f" x{item['quantity']:g}" if item["quantity"] else ""
            lines.append(f"  {item['name']}{quantity} [{item['location']}] — {when}")

    if listed:
        lines.append("")
        lines.append("ON THE SHOPPING LIST: " + ", ".join(e["name"] for e in listed))

    return "\n".join(lines)


@mcp.tool()
def add_note(body: str, tags: list[str] | None = None, person: str | None = None) -> str:
    """Store a note. Use this to save research findings and conclusions."""
    with transaction() as conn:
        values: dict = {"body": body}
        if tags:
            values["tags"] = json.dumps(tags)
        if person:
            row = conn.execute(
                "SELECT id FROM people WHERE lower(name) = lower(?)", (person,)
            ).fetchone()
            values["person_id"] = (
                int(row["id"])
                if row
                else mutations.insert(conn, _utterance_id(), "people", {"name": person})
            )
        note_id = mutations.insert(conn, _utterance_id(), "notes", values)
    return f"Saved note {note_id}."


@mcp.tool()
def add_event(
    title: str,
    starts_at: str,
    ends_at: str | None = None,
    location: str | None = None,
) -> str:
    """Add a calendar event. Times must be ISO 8601 with an offset."""
    values: dict = {
        "title": title,
        "starts_at": timeutil.to_utc_iso(starts_at),
        "source": "voice",
    }
    if ends_at:
        values["ends_at"] = timeutil.to_utc_iso(ends_at)
    if location:
        values["location"] = location
    with transaction() as conn:
        event_id = mutations.insert(conn, _utterance_id(), "events", values)
    return f"Added event {event_id}: {title}."


@mcp.tool()
def update_event(
    event_id: int,
    title: str | None = None,
    starts_at: str | None = None,
    location: str | None = None,
) -> str:
    """Change an existing event. Only the fields you pass are modified."""
    values: dict = {}
    if title is not None:
        values["title"] = title
    if starts_at is not None:
        values["starts_at"] = timeutil.to_utc_iso(starts_at)
    if location is not None:
        values["location"] = location
    if not values:
        return "Nothing to update."
    with transaction() as conn:
        ok = mutations.update(conn, _utterance_id(), "events", event_id, values)
    return f"Updated event {event_id}." if ok else f"No event {event_id}."


@mcp.tool()
def get_person(name: str) -> str:
    """Look up a person and everything noted about them."""
    conn = connect()
    try:
        person = conn.execute(
            "SELECT * FROM people WHERE lower(name) LIKE lower(?)", (f"%{name}%",)
        ).fetchone()
        if person is None:
            return f"No one matching {name!r}."
        notes = conn.execute(
            """SELECT body, created_at FROM notes
                 WHERE person_id = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 20""",
            (person["id"],),
        ).fetchall()
    finally:
        conn.close()
    return json.dumps(
        {**dict(person), "notes": [dict(n) for n in notes]}, indent=2
    )


@mcp.tool()
def list_projects() -> str:
    """All projects, what each is, and how many notes it has."""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT p.id, p.name, p.description, p.status,
                      (SELECT count(*) FROM notes n
                         WHERE n.project_id = p.id AND n.deleted_at IS NULL) AS note_count
                 FROM projects p ORDER BY p.name"""
        ).fetchall()
    finally:
        conn.close()
    return json.dumps([dict(r) for r in rows], indent=2)


@mcp.tool()
def project_context(project_id: int) -> str:
    """Everything stored about one project — the user's notes on it newest
    first, the reports already written, its dated items, its links, and the
    files in its working directory.

    Read this before doing project work. What the user has been thinking is
    usually the difference between research they wanted and research they
    already did.

    Read-only. Nothing here writes to a project: the user files their own
    thinking, and a report is how you hand yours back.
    """
    from projects import store

    conn = connect()
    try:
        detail = store.detail(conn, project_id, DEFAULT_TZ)
    finally:
        conn.close()
    if detail is None:
        return f"No project with id {project_id}."
    return json.dumps(detail, indent=2)


if __name__ == "__main__":
    mcp.run()
