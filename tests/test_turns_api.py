"""The turn is the number the user feels; latency_ms is not it.

Offline for the schema and endpoint tests. There is no shared conftest
fixture in this repo — conftest.py only redirects JARVIS_DB — so each test
file builds its own client, following tests/test_utterances.py.
"""

import sqlite3

import pytest

from app import config
from app.db import connect
from tests.helpers import apply_migrations


@pytest.fixture
def migrated(tmp_path):
    """Every migration, in order. The `conn` fixture other files use applies
    001_init.sql alone and would not see column 016."""
    path = tmp_path / "turns.db"
    apply_migrations(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_columns_exist(migrated):
    cols = {r["name"] for r in migrated.execute("PRAGMA table_info(utterances)")}
    assert "turn_ms" in cols
    assert "timings" in cols


def test_columns_are_nullable(migrated):
    """A Shortcut client has no microphone and reports no turn. That is a
    client without a mic, not a missing measurement."""
    migrated.execute("INSERT INTO utterances (raw_text, client) VALUES ('hi','shortcut')")
    migrated.commit()
    row = migrated.execute(
        "SELECT turn_ms, timings FROM utterances ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["turn_ms"] is None
    assert row["timings"] is None


@pytest.fixture(scope="module")
def client():
    """The canonical shape in this repo — see tests/test_utterances.py. The
    token goes on the client, so no separate auth fixture is needed."""
    from fastapi.testclient import TestClient

    import migrate
    from app.main import app

    assert migrate.migrate() == 0, "migrations failed"
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {config.jarvis_token()}"
        yield c


def _utterance(client) -> int:
    """A row to attach a turn to, written without a model call."""
    conn = connect()
    try:
        row_id = conn.execute(
            "INSERT INTO utterances (raw_text, client) VALUES ('testing','ios')"
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    return int(row_id)


def test_turn_is_recorded(client):
    utterance_id = _utterance(client)
    resp = client.post("/turns", json={"utterance_id": utterance_id, "turn_ms": 1840})
    assert resp.status_code == 204

    conn = connect()
    try:
        row = conn.execute(
            "SELECT turn_ms FROM utterances WHERE id = ?", (utterance_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["turn_ms"] == 1840


def test_unknown_id_is_not_an_error(client):
    """A late or duplicated report is not worth a failure path. The phone has
    already spoken by the time it sends this; nothing it hears back matters."""
    resp = client.post("/turns", json={"utterance_id": 999999, "turn_ms": 1000})
    assert resp.status_code == 204


def _stored_turns() -> list[int]:
    """Every turn already in the database.

    The module-scoped client shares one database across the file, and
    test_turn_is_recorded above has already written one. Asserting an absolute
    count here would encode the number of tests before this one rather than
    anything about /metrics, so the expectation is built from what is actually
    stored — which still pins the percentile arithmetic exactly.
    """
    conn = connect()
    try:
        return [
            r["turn_ms"]
            for r in conn.execute(
                "SELECT turn_ms FROM utterances WHERE turn_ms IS NOT NULL"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_metrics_reports_turn(client):
    expected = sorted(_stored_turns() + [1000, 1500, 2000])
    for ms in (1000, 1500, 2000):
        client.post("/turns", json={"utterance_id": _utterance(client), "turn_ms": ms})

    body = client.get("/metrics").json()
    assert body["turn"]["count"] == len(expected)
    assert body["turn"]["p50"] == expected[len(expected) // 2]
    assert body["turn"]["max"] == expected[-1]


def test_metrics_turn_counts_only_reported(client):
    """Counted only over utterances that reported one. A Shortcut has no
    microphone, and folding its silence in as a zero would report a headline
    number nobody experienced."""
    before = client.get("/metrics").json()["turn"]["count"]
    _utterance(client)  # written, never reported
    after = client.get("/metrics").json()["turn"]["count"]
    assert after == before
