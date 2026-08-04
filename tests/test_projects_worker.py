"""Where a project's deep job runs, and what it is told."""

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "worker.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)

    from projects import workspace

    monkeypatch.setattr(workspace, "root", lambda: tmp_path / "work" / "projects")

    connection = appdb.connect()
    try:
        yield connection
    finally:
        connection.close()


def seed(conn, prompt="what does it take", project=True) -> dict:
    from projects import store

    project_id = (
        store.create(conn, None, "Hydroponic Lettuce", "salad in the garage")
        if project
        else None
    )
    job_id = int(
        conn.execute(
            "INSERT INTO jobs (prompt, project_id) VALUES (?,?) RETURNING id",
            (prompt, project_id),
        ).fetchone()["id"]
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def test_a_loose_job_still_runs_in_the_shared_scratch_dir(conn):
    from app.config import WORK_DIR
    from worker import run

    assert run._work_dir(seed(conn, project=False)) == WORK_DIR


def test_a_project_job_runs_in_the_projects_own_directory(conn, tmp_path):
    from worker import run

    job = seed(conn)
    path = run._work_dir(job)

    assert path.parent == tmp_path / "work" / "projects"
    assert path.name.endswith("-hydroponic-lettuce")
    assert path.is_dir(), "the directory must exist before the CLI is spawned"


def test_the_same_job_run_twice_uses_the_same_directory(conn):
    from worker import run

    job = seed(conn)
    assert run._work_dir(job) == run._work_dir(job)


def test_a_project_job_is_told_what_it_belongs_to(conn):
    from worker import run

    prompt = run._prompt_for(seed(conn))

    assert "Hydroponic Lettuce" in prompt
    assert "salad in the garage" in prompt
    assert "project_context" in prompt
    assert "what does it take" in prompt


def test_a_loose_job_gets_its_ask_and_nothing_else(conn):
    from worker import run

    assert run._prompt_for(seed(conn, project=False)) == "what does it take"


def test_a_reply_is_still_just_the_reply(conn):
    """A resumed session already has the preamble in its history. Repeating it
    would spend context restating what the agent is already sitting in."""
    from worker import run

    job = seed(conn)
    job["pending_input"] = "go with the second option"
    prompt = run._prompt_for(job)

    assert "go with the second option" in prompt
    assert "project_context" not in prompt
