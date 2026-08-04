"""Token accounting — offline, with the model stubbed.

The property that matters: one utterance's tally covers every model call it
made, and two concurrent requests never see each other's counts.
"""

import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app import router, usage
from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "usage.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {SHARED}"
    return c


def fake_usage(input_tokens: int, output_tokens: int):
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


def row(db, utterance_id: int) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return dict(
            conn.execute("SELECT * FROM utterances WHERE id=?", (utterance_id,)).fetchone()
        )
    finally:
        conn.close()


# ── the tally ─────────────────────────────────────────────


def test_tally_accumulates_across_calls():
    """One utterance, two hops — `query` routes and then answers."""
    with usage.tally() as counts:
        usage.record(fake_usage(2557, 90))
        usage.record(fake_usage(1800, 60))
    assert counts == {"input_tokens": 4357, "output_tokens": 150, "model_calls": 2}


def test_recording_outside_a_scope_is_a_no_op():
    """The router is also called from tests, the MCP server, and scripts —
    none of which have a request around them."""
    usage.record(fake_usage(100, 10))  # must not raise
    assert usage.current()["model_calls"] == 0


def test_concurrent_requests_do_not_share_a_tally():
    """The reason this is a ContextVar and not a module-level dict. FastAPI
    runs sync endpoints in a threadpool, so two /say calls are genuinely
    concurrent and a shared dict would bill one utterance for the other's
    tokens."""
    seen: dict[str, int] = {}
    started = threading.Barrier(2)

    def request(name: str, tokens: int):
        with usage.tally():
            usage.record(fake_usage(tokens, 0))
            started.wait(timeout=5)  # force overlap
            seen[name] = usage.current()["input_tokens"]

    threads = [
        threading.Thread(target=request, args=("a", 100)),
        threading.Thread(target=request, args=("b", 900)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert seen == {"a": 100, "b": 900}


# ── persistence ───────────────────────────────────────────


def test_say_records_tokens_on_the_utterance(client, db, monkeypatch):
    monkeypatch.setattr(
        router,
        "route",
        lambda text, tz, reports=(), projects=(), today="", context="": (usage.record(fake_usage(2557, 95)), ("add_note", {"body": text}))[1],
    )
    response = client.post("/say", json={"text": "milk", "client": "test"})
    assert response.status_code == 200, response.text

    stored = row(db, response.json()["utterance_id"])
    assert (stored["input_tokens"], stored["output_tokens"]) == (2557, 95)
    assert stored["model_calls"] == 1


def test_a_two_hop_query_bills_both_calls(client, db, monkeypatch):
    """The number this exists to keep honest: a per-utterance average that
    folds in a silent second call is the kind of figure you optimize against
    for a week before noticing."""
    monkeypatch.setattr(
        router,
        "route",
        lambda text, tz, reports=(), projects=(), today="", context="": (
            usage.record(fake_usage(2557, 80)),
            ("query", {"question": text, "kind": "other"}),
        )[1],
    )
    monkeypatch.setattr(
        router,
        "answer",
        lambda q, ctx, tz: (usage.record(fake_usage(1900, 40)), "Nothing on.")[1],
    )
    response = client.post("/say", json={"text": "what did I say about Sarah", "client": "test"})
    assert response.status_code == 200, response.text

    stored = row(db, response.json()["utterance_id"])
    assert stored["model_calls"] == 2
    assert stored["input_tokens"] == 2557 + 1900


# ── /metrics ──────────────────────────────────────────────


def test_metrics_reports_spend(client, db, monkeypatch):
    monkeypatch.setattr(
        router,
        "route",
        lambda text, tz, reports=(), projects=(), today="", context="": (usage.record(fake_usage(2000, 100)), ("add_note", {"body": text}))[1],
    )
    client.post("/say", json={"text": "milk", "client": "test"})

    spend = client.get("/metrics").json()["spend"]
    assert spend["utterances"] == 1
    assert spend["input_tokens"] == 2000 and spend["output_tokens"] == 100
    # 2000/1e6 * $1 + 100/1e6 * $5
    assert spend["usd"] == pytest.approx(0.0025, abs=1e-6)


def test_pre_migration_rows_are_excluded_not_counted_as_zero(client, db, monkeypatch):
    """Rows written before 004 have NULL token columns. Counting them as zero
    would understate spend per utterance and read as an improvement."""
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO utterances (raw_text, route, latency_ms) VALUES ('old','fast',400)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        router,
        "route",
        lambda text, tz, reports=(), projects=(), today="", context="": (usage.record(fake_usage(2000, 100)), ("add_note", {"body": text}))[1],
    )
    client.post("/say", json={"text": "milk", "client": "test"})

    spend = client.get("/metrics").json()["spend"]
    assert spend["utterances"] == 1, "the pre-migration row should not be averaged in"
    assert spend["usd_per_utterance"] == pytest.approx(0.0025, abs=1e-6)


def test_cost_matches_published_pricing():
    """$1.00 / $5.00 per million for Haiku 4.5. If Anthropic changes it, this
    is the line to update — and history re-costs correctly because the
    database stores tokens, not dollars."""
    assert router.cost_usd(1_000_000, 0) == pytest.approx(1.00)
    assert router.cost_usd(0, 1_000_000) == pytest.approx(5.00)
