"""The queries and the working directory, without a server or a model."""

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "store.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    connection = appdb.connect()
    try:
        yield connection
    finally:
        connection.close()


# ── slugs and creation ────────────────────────────────────


def test_a_slug_is_lowercase_hyphenated_and_bounded():
    from projects import store

    assert store.slugify("Hydroponic Lettuce") == "hydroponic-lettuce"
    assert store.slugify("  spaced   out  ") == "spaced-out"
    assert store.slugify("what/now?.v2") == "what-now-v2"
    assert store.slugify("café society") == "cafe-society"
    assert store.slugify("x" * 200) == "x" * store.SLUG_MAX
    # Truncation must not leave a trailing hyphen in a directory name.
    assert not store.slugify(("ab " * 40)).endswith("-")


def test_a_name_that_slugifies_to_nothing_still_gets_a_directory_name():
    from projects import store

    assert store.slugify("???") == "project"


def test_creating_a_project_is_logged_and_undoable(conn):
    from app import mutations
    from projects import store

    # mutations.utterance_id is a real foreign key — a log row pointing at an
    # utterance that never happened is not a log row worth having.
    utterance_id = conn.execute(
        "INSERT INTO utterances (raw_text) VALUES ('start a project') RETURNING id"
    ).fetchone()["id"]
    project_id = store.create(conn, utterance_id, "Hydroponic Lettuce", "salad in the garage")
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    assert row["name"] == "Hydroponic Lettuce"
    assert row["slug"] == "hydroponic-lettuce"
    assert row["description"] == "salad in the garage"
    assert row["status"] == "active"

    logged = conn.execute(
        "SELECT table_name, op FROM mutations WHERE row_id = ?", (project_id,)
    ).fetchone()
    assert (logged["table_name"], logged["op"]) == ("projects", "insert")

    mutations.undo_last(conn)
    assert conn.execute("SELECT count(*) AS n FROM projects").fetchone()["n"] == 0


def test_finding_by_name_ignores_case_and_padding(conn):
    from projects import store

    store.create(conn, None, "Hydroponic Lettuce")
    assert store.find_by_name(conn, "  hydroponic lettuce ")["name"] == "Hydroponic Lettuce"
    assert store.find_by_name(conn, "greenhouse") is None


# ── the router's block ────────────────────────────────────


def test_active_lists_only_live_projects_newest_first(conn):
    from projects import store

    old = store.create(conn, None, "old thing")
    store.set_status(conn, old, "done")
    live = store.create(conn, None, "lettuce")

    listed = store.active(conn)
    assert [p["id"] for p in listed] == [live]
    assert set(listed[0]) == {"id", "name"}


def test_active_is_capped(conn):
    from projects import store

    for index in range(15):
        store.create(conn, None, f"project {index}")
    assert len(store.active(conn)) == 10


# ── the screen's list ─────────────────────────────────────


def test_listing_counts_what_is_under_each_project(conn):
    from projects import store

    project_id = store.create(conn, None, "lettuce")
    conn.execute(
        "INSERT INTO notes (body, project_id) VALUES ('deep water culture', ?)",
        (project_id,),
    )
    conn.execute(
        "INSERT INTO notes (body, project_id, deleted_at) VALUES ('oops', ?, '2026-08-01T00:00:00Z')",
        (project_id,),
    )
    conn.execute(
        "INSERT INTO jobs (prompt, project_id) VALUES ('research it', ?)", (project_id,)
    )

    row = next(p for p in store.listing(conn) if p["id"] == project_id)
    assert row["note_count"] == 1, "a soft-deleted note is not a note"
    assert row["report_count"] == 1
    assert row["last_activity_at"] is not None


# ── detail ────────────────────────────────────────────────


def test_detail_gathers_everything_under_the_project(conn):
    from projects import store

    project_id = store.create(conn, None, "lettuce", "salad in the garage")
    conn.execute(
        "INSERT INTO notes (body, project_id) VALUES ('deep water culture', ?)",
        (project_id,),
    )
    conn.execute(
        """INSERT INTO events (title, starts_at, project_id)
             VALUES ('permit office', '2026-08-10T15:00:00Z', ?)""",
        (project_id,),
    )
    conn.execute(
        """INSERT INTO reminders (body, fire_at, project_id)
             VALUES ('order the pump', '2026-08-09T16:00:00Z', ?)""",
        (project_id,),
    )
    conn.execute(
        """INSERT INTO jobs (prompt, status, summary, project_id)
             VALUES ('what does it take', 'done', 'Pumps and light.', ?)""",
        (project_id,),
    )
    store.add_link(conn, None, project_id, "https://example.com/pumps", "Pumps")

    detail = store.detail(conn, project_id, "America/Denver")
    assert detail["name"] == "lettuce"
    assert [n["body"] for n in detail["notes"]] == ["deep water culture"]
    assert [r["summary"] for r in detail["reports"]] == ["Pumps and light."]
    assert [e["title"] for e in detail["events"]] == ["permit office"]
    assert [r["body"] for r in detail["reminders"]] == ["order the pump"]
    assert [link["url"] for link in detail["links"]] == ["https://example.com/pumps"]
    assert detail["files"] == []


def test_detail_speaks_its_own_dates(conn):
    """The phone never formats a timestamp. /agenda and /proposals both render
    `when` server-side and both say why: two implementations of "tomorrow at
    3 PM" drift, and the one you would trust is the one you cannot see."""
    from projects import store

    project_id = store.create(conn, None, "lettuce")
    conn.execute(
        """INSERT INTO events (title, starts_at, project_id)
             VALUES ('permit office', '2026-08-10T21:00:00Z', ?)""",
        (project_id,),
    )
    conn.execute(
        "INSERT INTO notes (body, project_id) VALUES ('deep water culture', ?)",
        (project_id,),
    )

    detail = store.detail(conn, project_id, "America/Denver")
    assert "when" in detail["events"][0]
    assert "T" not in detail["events"][0]["when"]
    assert "when" in detail["notes"][0]


def test_detail_is_none_for_a_project_that_does_not_exist(conn):
    from projects import store

    assert store.detail(conn, 999, "America/Denver") is None


def test_a_duplicate_link_is_not_added_twice(conn):
    from projects import store

    project_id = store.create(conn, None, "lettuce")
    assert store.add_link(conn, None, project_id, "https://example.com", None) is not None
    assert store.add_link(conn, None, project_id, "https://example.com", None) is None


# ── context lines for `query` ─────────────────────────────


def test_context_lines_name_their_kind(conn):
    from projects import store

    project_id = store.create(conn, None, "lettuce", "salad in the garage")
    conn.execute(
        "INSERT INTO notes (body, project_id) VALUES ('deep water culture', ?)",
        (project_id,),
    )
    conn.execute(
        """INSERT INTO jobs (prompt, status, summary, project_id)
             VALUES ('what does it take', 'done', 'Pumps and light.', ?)""",
        (project_id,),
    )

    lines = store.context_lines(conn, project_id, "America/Denver")
    assert any(line.startswith("PROJECT: lettuce") for line in lines)
    assert any(line == "NOTE: deep water culture" for line in lines)
    assert any(line.startswith("REPORT (what does it take): Pumps and light.") for line in lines)


def test_context_lines_without_an_id_list_the_active_projects(conn):
    from projects import store

    store.create(conn, None, "lettuce")
    store.create(conn, None, "greenhouse")

    lines = store.context_lines(conn, None, "America/Denver")
    assert sum(line.startswith("PROJECT: ") for line in lines) == 2


# ── the working directory ─────────────────────────────────


def test_the_directory_is_named_for_the_id_and_the_slug(conn, monkeypatch, tmp_path):
    from projects import store, workspace

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "work" / "projects")
    project_id = store.create(conn, None, "Hydroponic Lettuce")
    project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())

    created = workspace.ensure(project)
    assert created.name == f"{project_id}-hydroponic-lettuce"
    assert created.is_dir()


def test_listing_reports_what_the_agent_wrote(conn, monkeypatch, tmp_path):
    from projects import store, workspace

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "work" / "projects")
    project_id = store.create(conn, None, "lettuce")
    project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    (workspace.ensure(project) / "sources.md").write_text("# Sources\n")

    listed = workspace.listing(project)
    assert [f["name"] for f in listed] == ["sources.md"]
    assert listed[0]["bytes"] == len("# Sources\n")
    assert listed[0]["modified_at"].endswith("Z")


def test_reading_a_file_cannot_escape_the_project(conn, monkeypatch, tmp_path):
    from projects import store, workspace

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "work" / "projects")
    project_id = store.create(conn, None, "lettuce")
    project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    workspace.ensure(project)
    (tmp_path / "work" / "secret.txt").write_text("the api key")

    for attempt in ("../secret.txt", "../../work/secret.txt", "/etc/passwd"):
        with pytest.raises(ValueError):
            workspace.read_text(project, attempt)


def test_reading_refuses_something_that_is_not_text(conn, monkeypatch, tmp_path):
    from projects import store, workspace

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "work" / "projects")
    project_id = store.create(conn, None, "lettuce")
    project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    (workspace.ensure(project) / "chart.png").write_bytes(b"\x89PNG\r\n\x00\x1a\n")

    with pytest.raises(ValueError):
        workspace.read_text(project, "chart.png")


def test_reading_a_real_file_returns_its_text(conn, monkeypatch, tmp_path):
    from projects import store, workspace

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "work" / "projects")
    project_id = store.create(conn, None, "lettuce")
    project = dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
    (workspace.ensure(project) / "sources.md").write_text("# Sources\n")

    assert workspace.read_text(project, "sources.md") == "# Sources\n"
