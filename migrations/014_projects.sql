-- Projects: a named space that collects the thinking about one thing.
--
-- The table itself has existed since 001 and nothing ever wrote to it. This
-- migration gives it the columns a real project needs and hangs the rest of
-- the domain off it.
--
-- `notes` -> `description`: the column was never written and its name sat one
-- character away from the `notes` table it has nothing to do with. It now
-- holds the one line saying what the project is.
--
-- `slug` is stored rather than derived from `name` because the deep agent's
-- working directory is `work/projects/<id>-<slug>/` and its reports quote
-- paths inside it. Deriving the slug would move the directory on rename and
-- invalidate every path in every report already written.

ALTER TABLE projects RENAME COLUMN notes TO description;
ALTER TABLE projects ADD COLUMN slug TEXT;

-- ON DELETE SET NULL, not CASCADE — the same call 007 made for
-- proposals.event_id. Deleting a project must not take a dentist appointment
-- off the calendar or destroy the report you asked for.
--
-- SQLite allows ADD COLUMN with a REFERENCES clause only when the default is
-- NULL, which is exactly what is wanted: every existing row files nowhere.
ALTER TABLE events    ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE reminders ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE jobs      ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;

-- notes.project_id keeps the no-action FK it was born with, so a project with
-- notes cannot be deleted at all. Deliberate: the notes are the point of the
-- project, and a delete button that also erases three weeks of thinking is not
-- a button worth having. You mark it done.

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

-- Backfill for the rows add_note created implicitly before projects were
-- deliberate. Crude on purpose — projects.store.slugify does the real job for
-- everything created from here on, and these rows only need a directory name
-- that exists and is stable.
UPDATE projects
   SET slug = lower(replace(replace(replace(name, ' ', '-'), '/', '-'), '.', '-'))
 WHERE slug IS NULL;
