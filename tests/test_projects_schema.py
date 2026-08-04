"""What migration 014 guarantees.

The FK postures are the point. Deleting a project must not take your calendar
with it, and must not be able to erase the notes that were the reason the
project existed.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "schema.db"
    apply_migrations(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    # apply_migrations strips PRAGMAs, and every posture below is unenforced
    # without this.
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_the_unused_notes_column_became_a_description(conn):
    names = columns(conn, "projects")
    assert "description" in names
    assert "notes" not in names, "the column name collided with the notes table"
    assert "slug" in names


def test_the_three_tables_can_point_at_a_project(conn):
    for table in ("events", "reminders", "jobs"):
        assert "project_id" in columns(conn, table)


def test_deleting_a_project_leaves_the_calendar_alone(conn):
    project = conn.execute(
        "INSERT INTO projects (name, slug) VALUES ('greenhouse','greenhouse') RETURNING id"
    ).fetchone()["id"]
    conn.execute(
        """INSERT INTO events (title, starts_at, project_id)
             VALUES ('permit office', '2026-08-10T15:00:00Z', ?)""",
        (project,),
    )
    conn.execute("DELETE FROM projects WHERE id = ?", (project,))

    row = conn.execute("SELECT project_id FROM events").fetchone()
    assert row is not None, "the event was deleted along with the project"
    assert row["project_id"] is None


def test_a_project_with_notes_cannot_be_deleted(conn):
    project = conn.execute(
        "INSERT INTO projects (name, slug) VALUES ('greenhouse','greenhouse') RETURNING id"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO notes (body, project_id) VALUES ('deep water culture', ?)",
        (project,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM projects WHERE id = ?", (project,))


def test_links_die_with_their_project(conn):
    project = conn.execute(
        "INSERT INTO projects (name, slug) VALUES ('greenhouse','greenhouse') RETURNING id"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO project_links (project_id, url) VALUES (?, 'https://example.com')",
        (project,),
    )
    conn.execute("DELETE FROM projects WHERE id = ?", (project,))
    assert conn.execute("SELECT count(*) AS n FROM project_links").fetchone()["n"] == 0


def test_projects_that_predate_the_migration_get_a_slug(tmp_path):
    """`add_note` used to create projects implicitly, so real rows exist with
    no slug. The working directory is named from it, so NULL is not an option.

    Applied in two halves because that is the situation being tested: a row
    that existed before 014 ran.
    """
    from app.config import REPO_ROOT

    path = tmp_path / "upgrade.db"
    migrations = sorted((REPO_ROOT / "migrations").glob("*.sql"))
    before = [m for m in migrations if m.name < "014"]
    fourteen = next(m for m in migrations if m.name.startswith("014"))

    def body(sql_file) -> str:
        # PRAGMAs cannot run inside executescript's implicit transaction —
        # tests/helpers.apply_migrations strips them for the same reason.
        return "\n".join(
            line
            for line in sql_file.read_text().splitlines()
            if not line.strip().upper().startswith("PRAGMA")
        )

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        for sql_file in before:
            connection.executescript(body(sql_file))
        connection.execute("INSERT INTO projects (name) VALUES ('Old Thing / v2')")
        connection.executescript(body(fourteen))

        slug = connection.execute(
            "SELECT slug FROM projects WHERE name = 'Old Thing / v2'"
        ).fetchone()["slug"]
        assert slug == "old-thing---v2", slug
    finally:
        connection.close()
