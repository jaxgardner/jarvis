"""The shape the phone decodes. See ios/JarvisTests/ContractTests.swift for
the other half of this contract."""

import pytest
from fastapi.testclient import TestClient

from tests.helpers import apply_migrations


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "api.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)

    from projects import workspace

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "work" / "projects")

    from app import config
    from app.main import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {config.jarvis_token()}"
        yield c


def seed() -> int:
    from app.db import transaction
    from projects import store

    with transaction() as conn:
        project_id = store.create(conn, None, "Hydroponic Lettuce", "salad in the garage")
        conn.execute(
            "INSERT INTO notes (body, project_id) VALUES ('deep water culture', ?)",
            (project_id,),
        )
        conn.execute(
            """INSERT INTO jobs (prompt, status, summary, project_id)
                 VALUES ('what does it take', 'done', 'Pumps and light.', ?)""",
            (project_id,),
        )
    return project_id


def test_an_empty_install_answers_with_a_shape(client):
    assert client.get("/projects").json() == {"projects": []}


def test_the_list_carries_counts_and_a_last_touched_stamp(client):
    seed()
    listed = client.get("/projects").json()["projects"]

    assert listed[0]["name"] == "Hydroponic Lettuce"
    assert listed[0]["note_count"] == 1
    assert listed[0]["report_count"] == 1
    assert listed[0]["status"] == "active"
    assert listed[0]["last_activity_at"]


def test_the_detail_carries_every_section(client):
    project_id = seed()
    detail = client.get(f"/projects/{project_id}").json()

    assert detail["description"] == "salad in the garage"
    for section in ("notes", "reports", "events", "reminders", "links", "files"):
        assert section in detail
    assert detail["reports"][0]["summary"] == "Pumps and light."


def test_a_project_that_does_not_exist_is_a_404(client):
    assert client.get("/projects/999").status_code == 404


def test_pasting_a_link_returns_the_updated_project(client):
    project_id = seed()
    body = client.post(
        f"/projects/{project_id}/links",
        json={"url": "https://example.com/pumps", "title": "Pumps"},
    ).json()

    assert [link["url"] for link in body["links"]] == ["https://example.com/pumps"]


def test_pasting_the_same_link_twice_does_not_double_it(client):
    project_id = seed()
    for _ in range(2):
        body = client.post(
            f"/projects/{project_id}/links", json={"url": "https://example.com/pumps"}
        ).json()
    assert len(body["links"]) == 1


def test_a_blank_url_is_rejected(client):
    project_id = seed()
    assert client.post(f"/projects/{project_id}/links", json={"url": "  "}).status_code == 422


def test_marking_a_project_done_takes_it_out_of_the_routers_view(client):
    from app.db import connect
    from projects import store

    project_id = seed()
    body = client.patch(f"/projects/{project_id}", json={"status": "done"}).json()
    assert body["status"] == "done"

    conn = connect()
    try:
        assert store.active(conn) == []
    finally:
        conn.close()


def test_an_unknown_status_is_rejected(client):
    project_id = seed()
    assert client.patch(f"/projects/{project_id}", json={"status": "shelved"}).status_code == 422


def test_renaming_keeps_the_directory_where_it_was(client):
    """Reports quote paths under the old name. Moving the directory would
    invalidate every one of them."""
    from app.db import connect
    from projects import store

    project_id = seed()
    client.patch(f"/projects/{project_id}", json={"name": "Lettuce, Actually"})

    conn = connect()
    try:
        assert store.get(conn, project_id)["slug"] == "hydroponic-lettuce"
    finally:
        conn.close()


def test_a_file_the_agent_wrote_can_be_read(client, tmp_path):
    from app.db import connect
    from projects import store, workspace

    project_id = seed()
    conn = connect()
    try:
        project = store.get(conn, project_id)
    finally:
        conn.close()
    (workspace.ensure(project) / "sources.md").write_text("# Sources\n")

    body = client.get(f"/projects/{project_id}/files/sources.md").json()
    assert body == {"name": "sources.md", "text": "# Sources\n"}


def test_a_file_outside_the_project_is_refused(client):
    project_id = seed()
    response = client.get(f"/projects/{project_id}/files/..%2F..%2Fsecret.txt")
    # 400 means Starlette rejected the encoded slashes before routing, which is
    # also a pass for the property under test: the file is not served.
    assert response.status_code in (400, 404)
