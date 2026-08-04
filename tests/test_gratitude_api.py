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

    from app import config
    from app.main import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {config.jarvis_token()}"
        yield c


def seed(on, bodies):
    from app.db import transaction

    with transaction() as conn:
        for index, body in enumerate(bodies):
            conn.execute(
                """INSERT INTO gratitude_entries (body, entry_on, created_at)
                     VALUES (?,?,?)""",
                (body, on, f"{on}T{20 + index:02d}:00:00Z"),
            )


def today_on():
    from app import config, timeutil
    from gratitude import entries

    return entries.day_for(timeutil.now(config.DEFAULT_TZ))


def test_an_empty_day_still_answers_with_a_shape(client):
    body = client.get("/gratitude").json()

    assert body["today"]["on"] == today_on()
    assert body["today"]["target"] == 3
    assert body["today"]["entries"] == []
    assert body["streak"] == 0
    assert body["days"] == []


def test_todays_entries_come_back_separately_from_history(client):
    seed(today_on(), ["the sun", "Emma calling"])
    seed("2026-01-02", ["a", "b", "c"])

    body = client.get("/gratitude?days=3650").json()

    assert [e["body"] for e in body["today"]["entries"]] == ["the sun", "Emma calling"]
    assert [d["on"] for d in body["days"]] == ["2026-01-02"]
    assert set(body["today"]["entries"][0]) == {"id", "body", "at"}


def test_the_window_is_clamped(client):
    """A windowed endpoint that trusts its query string is one request away
    from scanning the whole table."""
    assert client.get("/gratitude?days=99999").status_code == 200
    assert client.get("/gratitude?days=0").status_code == 200


def test_it_needs_a_token(client):
    response = client.get("/gratitude", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
