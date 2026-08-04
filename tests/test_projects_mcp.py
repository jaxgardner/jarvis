"""The seam the deep agent reads a project through."""

import json

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "mcp.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)

    from projects import workspace

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "work" / "projects")
    return path


def test_project_context_returns_the_thinking_and_the_reports(db):
    from app.db import connect
    from mcp_server import server
    from projects import store

    conn = connect()
    try:
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
        conn.commit()
    finally:
        conn.close()

    payload = json.loads(server.project_context(project_id))

    assert payload["name"] == "lettuce"
    assert payload["description"] == "salad in the garage"
    assert [n["body"] for n in payload["notes"]] == ["deep water culture"]
    assert payload["reports"][0]["summary"] == "Pumps and light."


def test_project_context_says_so_when_there_is_no_such_project(db):
    from mcp_server import server

    assert "999" in server.project_context(999)


def test_listing_projects_survived_the_column_rename(db):
    from app.db import connect
    from mcp_server import server
    from projects import store

    conn = connect()
    try:
        store.create(conn, None, "lettuce", "salad in the garage")
        conn.commit()
    finally:
        conn.close()

    listed = json.loads(server.list_projects())
    assert listed[0]["name"] == "lettuce"
    assert listed[0]["description"] == "salad in the garage"
