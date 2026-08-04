"""Every query about a project.

Nothing here computes or stores a status. "Where am I on this" is answered by
handing these rows to a model at the moment you ask — the morning brief's rule
applied again: store only the irreducible part, and for a project there is no
irreducible part.
"""

import re
import unicodedata

from app import mutations, timeutil

STATUSES = ("active", "paused", "done")

# Long enough to tell two projects apart in a directory listing, short enough
# that the path stays inside espeak's and everything else's comfort zone.
SLUG_MAX = 40

# What the router is shown, and therefore what is reachable by voice. Ten is
# the same number `handlers.recent_reports` picked, for the same reason: enough
# to name what you are working on, short enough to leave the prompt small.
ACTIVE_LIMIT = 10

# Enough thinking for a model to answer from, bounded so one talkative project
# cannot crowd out the mail and calendar that the same question may also need.
CONTEXT_NOTES = 20
CONTEXT_REPORTS = 5
_SUMMARY_CHARS = 400


def slugify(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return cleaned[:SLUG_MAX].strip("-") or "project"


# ── writes ────────────────────────────────────────────────


def create(conn, utterance_id, name: str, description: str | None = None) -> int:
    values = {"name": name.strip(), "slug": slugify(name)}
    if description and description.strip():
        values["description"] = description.strip()
    return mutations.insert(conn, utterance_id, "projects", values)


def set_status(conn, project_id: int, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    return mutations.update(conn, None, "projects", project_id, {"status": status})


def rename(conn, project_id: int, name: str) -> bool:
    """The name changes; the slug does not. Reports already written quote paths
    under the old directory name and those paths must stay valid."""
    return mutations.update(conn, None, "projects", project_id, {"name": name.strip()})


def add_link(conn, utterance_id, project_id: int, url: str, title: str | None) -> int | None:
    """Returns None when the link is already on the project — a second paste of
    the same URL is not an error and not a second row."""
    url = url.strip()
    existing = conn.execute(
        "SELECT id FROM project_links WHERE project_id = ? AND url = ?",
        (project_id, url),
    ).fetchone()
    if existing:
        return None
    values = {"project_id": project_id, "url": url}
    if title and title.strip():
        values["title"] = title.strip()
    return mutations.insert(conn, utterance_id, "project_links", values)


# ── reads ─────────────────────────────────────────────────


def find_by_name(conn, name: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE lower(name) = lower(?)", (name.strip(),)
    ).fetchone()
    return dict(row) if row else None


def get(conn, project_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def active(conn, limit: int = ACTIVE_LIMIT) -> list[dict]:
    """What the router is shown. Id and name only — the prompt is a lookup
    table, not a summary."""
    return [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            """SELECT id, name FROM projects WHERE status = 'active'
                 ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ]


_LISTING_SQL = """
SELECT p.id, p.name, p.description, p.status, p.slug, p.created_at,
       (SELECT count(*) FROM notes n
          WHERE n.project_id = p.id AND n.deleted_at IS NULL)  AS note_count,
       (SELECT count(*) FROM jobs j WHERE j.project_id = p.id) AS report_count,
       (SELECT count(*) FROM project_links l WHERE l.project_id = p.id) AS link_count,
       max(
         p.created_at,
         coalesce((SELECT max(n.created_at) FROM notes n
                     WHERE n.project_id = p.id AND n.deleted_at IS NULL), ''),
         coalesce((SELECT max(j.created_at) FROM jobs j WHERE j.project_id = p.id), '')
       ) AS last_activity_at
  FROM projects p
 ORDER BY (p.status = 'active') DESC, last_activity_at DESC
"""


def listing(conn) -> list[dict]:
    """The screen's list. Active first, then most recently touched."""
    return [dict(r) for r in conn.execute(_LISTING_SQL).fetchall()]


def detail(conn, project_id: int, tz_name: str) -> dict | None:
    from projects import workspace

    project = get(conn, project_id)
    if project is None:
        return None

    notes = conn.execute(
        """SELECT id, body, tags, created_at FROM notes
             WHERE project_id = ? AND deleted_at IS NULL
             ORDER BY id DESC""",
        (project_id,),
    ).fetchall()
    reports = conn.execute(
        """SELECT id, prompt, status, summary, error, created_at, finished_at
             FROM jobs WHERE project_id = ? ORDER BY id DESC""",
        (project_id,),
    ).fetchall()
    events = conn.execute(
        """SELECT id, title, starts_at, ends_at, all_day, location FROM events
             WHERE project_id = ? AND deleted_at IS NULL ORDER BY starts_at""",
        (project_id,),
    ).fetchall()
    reminders = conn.execute(
        """SELECT id, body, fire_at, status FROM reminders
             WHERE project_id = ? ORDER BY fire_at""",
        (project_id,),
    ).fetchall()
    links = conn.execute(
        """SELECT id, url, title, created_at FROM project_links
             WHERE project_id = ? ORDER BY id DESC""",
        (project_id,),
    ).fetchall()

    # `when` is rendered here, not on the phone. /agenda and /proposals both do
    # this and both say why: two implementations of "tomorrow at 3 PM" drift,
    # and the one you would trust is the one you cannot see.
    def spoken(rows, column, all_day_column=None):
        out = []
        for row in rows:
            item = dict(row)
            item["when"] = timeutil.speak_datetime(
                item[column], tz_name, bool(item[all_day_column]) if all_day_column else False
            )
            out.append(item)
        return out

    files = workspace.listing(project)
    for entry in files:
        entry["when"] = timeutil.speak_datetime(entry["modified_at"], tz_name)

    return {
        **project,
        "notes": spoken(notes, "created_at"),
        "reports": [dict(r) for r in reports],
        "events": spoken(events, "starts_at", "all_day"),
        "reminders": spoken(reminders, "fire_at"),
        "links": [dict(r) for r in links],
        "files": files,
    }


def context_lines(conn, project_id: int | None, tz_name: str) -> list[str]:
    """What `query` puts in front of the model.

    Same line shapes as the rest of `handlers.query` — NOTE:, REPORT:, EVENT:,
    REMINDER: — so a project question and a normal one produce context the
    answering prompt already knows how to read.
    """
    if project_id is None:
        return [
            f"PROJECT: {p['name']}"
            for p in listing(conn)
            if p["status"] == "active"
        ]

    project = get(conn, project_id)
    if project is None:
        return []

    lines = [
        f"PROJECT: {project['name']}"
        + (f" — {project['description']}" if project["description"] else "")
    ]

    for row in conn.execute(
        """SELECT body FROM notes WHERE project_id = ? AND deleted_at IS NULL
             ORDER BY id DESC LIMIT ?""",
        (project_id, CONTEXT_NOTES),
    ).fetchall():
        lines.append(f"NOTE: {row['body']}")

    for row in conn.execute(
        """SELECT prompt, status, summary, result FROM jobs
             WHERE project_id = ? ORDER BY id DESC LIMIT ?""",
        (project_id, CONTEXT_REPORTS),
    ).fetchall():
        # Same fallback handlers._report_line makes: NULL summary is normal and
        # permanent for anything that finished before summaries existed.
        body = (row["summary"] or "").strip() or (row["result"] or "").strip()
        if body:
            lines.append(f"REPORT ({row['prompt']}): {body[:_SUMMARY_CHARS]}")
        elif row["status"] in ("queued", "running"):
            lines.append(f"REPORT ({row['prompt']}): still working")

    for row in conn.execute(
        """SELECT title, starts_at, all_day, location FROM events
             WHERE project_id = ? AND deleted_at IS NULL ORDER BY starts_at""",
        (project_id,),
    ).fetchall():
        when = timeutil.speak_datetime(row["starts_at"], tz_name, bool(row["all_day"]))
        where = f" at {row['location']}" if row["location"] else ""
        lines.append(f"EVENT: {row['title']} — {when}{where}")

    for row in conn.execute(
        """SELECT body, fire_at FROM reminders
             WHERE project_id = ? AND status IN ('pending','firing') ORDER BY fire_at""",
        (project_id,),
    ).fetchall():
        lines.append(
            f"REMINDER: {row['body']} — {timeutil.speak_datetime(row['fire_at'], tz_name)}"
        )

    for row in conn.execute(
        "SELECT url, title FROM project_links WHERE project_id = ? ORDER BY id DESC",
        (project_id,),
    ).fetchall():
        lines.append(f"LINK: {row['title'] or ''} {row['url']}".strip())

    return lines
