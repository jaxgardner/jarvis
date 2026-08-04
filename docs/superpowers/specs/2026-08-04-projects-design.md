# Projects

A project is a named space that collects the thinking, research, dates and
artifacts belonging to one thing you are working on. You start one by voice,
optionally with research attached; you add to it by naming it; you read it on
a screen or by asking where you are.

    "start a new project on hydroponic lettuce and research what it takes"
    "for the lettuce project, I'm thinking about doing deep water culture"
    "where am I on the lettuce project?"

## What already exists

Most of the pieces are in the repo and unused.

- `projects` has existed since migration 001 — `id, name, status
  (active|paused|done), notes, created_at`. Nothing writes `status`. Only
  `mcp_server.list_projects` and the note-search join read the table at all.
- `notes.project_id` exists. `add_note(project="…")` files a note under a
  project, and `handlers._lookup_or_create` silently creates the project when
  the name is new.
- The deep path is the research half already: `escalate` → `jobs` row →
  Claude Code → `result` + a Haiku `summary` + a push. Replying resumes the
  same session in place.
- The `REPORTS` block in the router's system prompt is the pattern for
  referring to a stored row by name. It exists because guessing "the most
  recent one" was right often enough to feel fine and wrong in the case you
  would care about.

This design is mostly wiring those together, plus one new tool and one screen.

## Decisions

### A project is a full container

Notes, deep reports, events, reminders, links and the agent's files all file
under a project. Not a tag on notes.

### Attachment is by naming, never by inference

A PROJECTS block in the router's system prompt lists active projects as
`id  name`, and every write tool takes an optional `project_id` chosen from
it. Nothing is attached because its content looked related, and there is no
sticky "current project" that a later utterance silently inherits. Naming
beats guessing — the same conclusion `escalate`'s `job_id` reached after
`is_follow_up` was deleted.

A name that matches nothing files nowhere. It does **not** create a project:
see below.

### Status is recomputed, never stored

There is no "state of play" paragraph on the project row. Asking where you are
gathers the project's notes, reports and dated items live and answers from
them, one Haiku hop, the way `query` already works. The screen shows the rows
themselves — latest thinking on top.

Storing a generated status would mean a metered call on every note and a
paragraph that can disagree with the rows beneath it. This is the morning
brief's rule applied again: store only the irreducible part, and staleness
becomes impossible rather than merely unlikely. For a project there is no
irreducible part, so nothing is stored.

### Naming an unknown project does not create one

`add_note`'s free-text `project` parameter is replaced by an integer
`project_id` drawn from the PROJECTS block. Voice is lossy; a free-text name
means "the grain house project" spawns a ghost row that then needs a merge
tool the system does not have. Starting a project is now the only way one
comes into existence.

`_lookup_or_create` stays, serving `people` only.

### Status changes are screen-only

No `set_project_status` tool. Marking a project done is a once-per-project
action taken with your eyes open, and every router tool costs prompt budget
and misroute surface. Creation stays on voice because that is the moment your
hands are busy.

### The agent reads a project; it does not write to it

The research agent gets a read-only `project_context` MCP tool and no way to
file notes or links back. Sources it found are in the report text, where you
can read them. This is the same human-disposes rule that governs `proposals`
and `receipts`. If filing sources by hand becomes tedious twice, revisit it
then.

## Schema — migration 014

```sql
ALTER TABLE projects RENAME COLUMN notes TO description;
ALTER TABLE projects ADD COLUMN slug TEXT;

ALTER TABLE events    ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE reminders ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE jobs      ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;

CREATE TABLE project_links (
  id         INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  url        TEXT NOT NULL,
  title      TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_events_project    ON events(project_id)    WHERE project_id IS NOT NULL;
CREATE INDEX idx_reminders_project ON reminders(project_id) WHERE project_id IS NOT NULL;
CREATE INDEX idx_jobs_project      ON jobs(project_id)      WHERE project_id IS NOT NULL;
CREATE INDEX idx_notes_project     ON notes(project_id)     WHERE project_id IS NOT NULL;
CREATE INDEX idx_project_links     ON project_links(project_id);
```

`projects.notes` is renamed rather than dropped: it has never been written, and
the name is confusing beside the `notes` table. It becomes the one-line "what
this project is", set at creation and shown as the card's subtitle.

Subtleties worth not rediscovering:

- **`ON DELETE SET NULL`, not CASCADE**, on `events`, `reminders` and `jobs` —
  the same call migration 007 made for `proposals.event_id`. Deleting a
  project must not take a dentist appointment off your calendar. Links cascade,
  because a link has no life outside its project.
- **`notes.project_id` keeps its existing no-action FK**, so a project with
  notes cannot be deleted at all. You mark it `done`. This is deliberate: the
  notes are the point of the project, and the failure mode of a delete button
  that also erases three weeks of thinking is not one worth having.
- **`slug` is stored, not derived from `name`.** The working directory is
  `projects/<id>-<slug>/` and reports quote paths inside it. Deriving the slug
  would move the directory on rename and invalidate every path in every report
  already written.
- `project_links` joins the `mutations` allowlist. A pasted link is a human
  action and human actions are what `/undo` exists to reverse — the same
  reasoning that routes `accept_proposal` through the helper while synced
  writes bypass it.

## Router surface

One new tool; `project_id` added to five existing ones.

| Tool | Parameters |
| :-- | :-- |
| `start_project` | `name`, `description?`, `research_task?` |
| `add_note` | `body`, `tags?`, `project_id?`, `person?` — `project` removed |
| `add_event` | …existing…, `project_id?` |
| `add_reminder` | …existing…, `project_id?` |
| `escalate` | `restated_task`, `job_id?`, `project_id?` |
| `query` | …existing…, `project_id?`, `kind` gains `"project"` |

The PROJECTS block is byte-identical in shape to REPORTS: active projects only,
`id  name`, capped at ten, and **omitted entirely when empty** — an empty table
invites the model to invent an id.

### `start_project`

One sentence does two things — "start a project on X and build some research
for it" — but `tool_choice: {"type": "any"}` emits exactly one call, so one
tool has to cover both.

`start_project` is a fast handler that may enqueue a deep job. With
`research_task` present it inserts the project, then a `jobs` row carrying its
`project_id`, and returns `route: "deep"` with a `job_id` — so the push, the
poll and the Reports screen work unchanged. Without it, the project is created
and the reply says so.

Replies are templated as everything else is:

    "Started the lettuce project — I'll dig into it and ping you."
    "Started the lettuce project."

### Prompt budget

The router prompt plus its twelve tool definitions measures **3767** tokens
against Haiku 4.5's 4096-token minimum cacheable prefix, which is why nothing
is cached today. A new tool, five new parameters and the PROJECTS block is
roughly 300–400 tokens.

This will likely tip the prompt over 4096 for the first time. If it does,
caching begins to fire for free and `cache_control` on the prefix becomes worth
adding. If it lands just under, this is the one circumstance where padding the
prefix stops being the bloat it was previously judged to be.

**Measure it with `count_tokens` rather than assuming either way**, and rewrite
the CLAUDE.md note to match whichever is true.

## Deep jobs attached to a project

Two changes in `worker/run.py`, both keyed on `jobs.project_id`:

- **The job runs in `WORK_DIR/projects/<id>-<slug>/`** instead of the shared
  scratch dir. Artifacts accumulate somewhere stable, and a resumed session —
  a reply to the report — finds its own earlier work. This is a subdirectory of
  the existing quarantine, not a loosening of it: `.env` and the repo are no
  more reachable than before.
- **The prompt gains a PROJECT preamble** naming the project's id, name and
  description, and pointing at `project_context`.

`mcp_server` gains **`project_context(project_id)`** — notes newest-first, the
links, dated items, and prior report summaries. This is what makes "for the
lettuce project, I'm thinking about deep water culture, what do you think?"
worth asking: research started three weeks in sees the three weeks of thinking
rather than only today's sentence.

`mcp_server.list_projects` is updated for the renamed column.

## Reads

| Endpoint | Purpose |
| :-- | :-- |
| `GET /projects` | Projects with counts and last activity; paused/done flagged |
| `GET /projects/{id}` | Everything under it — notes, reports, dated items, links, files |
| `GET /projects/{id}/files/{name}` | Text of one artifact |
| `POST /projects/{id}/links` | Paste a URL. Through the mutations helper |
| `PATCH /projects/{id}` | Rename, or set status `active` / `paused` / `done` |

`GET /projects/{id}/files/{name}` resolves the name against the project
directory and rejects anything that escapes it — `Path.resolve()` and an
`is_relative_to` check, not string inspection. The directory holds whatever the
agent wrote; the endpoint returns text and refuses binary.

### By voice

`query(kind="project", project_id=…)` gathers the project's rows into the same
`NOTE:` / `REPORT:` / `EVENT:` / `REMINDER:` / `LINK:` context lines `query`
already builds, and hands them to `router.answer`. No new answering machinery:
a project is another thing the assistant knows about, not a mode it enters.

With `project_id` omitted, `kind="project"` answers "what am I working on" from
the active list.

## The screen

`Reports` moves into Health's nav group, beside Activity and Review, and
`Projects` takes its place in the tab bar. Six tabs, not seven — the seventh
is what made "Gratitude" wrap. Every project screen lists its own reports with
a reply box, so the standalone Reports list is what is left over: loose reports
and old ones.

`ProjectsView` — active projects as cards showing name, description, last
activity and counts. Paused and done collapse below.

`ProjectDetailView`, in sections:

- **Thinking** — notes, newest first. The train of thought, and the top of the
  screen.
- **Reports** — each with its summary and the existing `ReplyBox`.
- **Dates** — events and reminders filed here, next first.
- **Links** — with the paste field, since you cannot speak a URL.
- **Files** — what the agent produced. Tap to read.

## Testing

Live router set:

- `"for the lettuce project, I'm thinking about deep water culture"` →
  `add_note` with that project's `project_id`.
- `"start a project on hydroponic lettuce and research what it takes"` →
  `start_project` **with** `research_task`, not `escalate`.
- `"start a project on hydroponic lettuce"` → `start_project` with no
  `research_task`.
- A note naming a project that does not exist → `project_id` **omitted**, never
  invented.
- `"where am I on the lettuce project"` → `query(kind="project")` with the id.

Unit and integration:

- The PROJECTS block is absent entirely when no active projects exist.
- A project job runs in its own directory; a second run of the same job runs
  in the same one.
- `../` and absolute paths in a file name are rejected by the files endpoint.
- Deleting a project nulls its events rather than deleting them.
- Deleting a project that has notes fails.
- `count_tokens` on the new system prompt, recorded against the 4096 threshold.

## Out of scope

- Merging two projects. There is no path that creates a duplicate any more.
- Agent-written notes or links.
- Voice status changes.
- A share-sheet extension for links. The paste field first.
