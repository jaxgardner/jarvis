"""Reports the assistant can name and talk about.

The router sees the last ten in its system prompt, so "answer the vendor one"
and "what did it say about pricing" both resolve to an id rather than a guess.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "reports.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


def rows(db, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def make_job(db, **overrides) -> int:
    """A finished report with a session to resume, unless told otherwise."""
    fields = {
        "prompt": "Compare the three vendors",
        "status": "done",
        "result": "Vendor B is cheapest at $4,200/yr.",
        "summary": "Compared three vendors; B is cheapest at $4,200/yr.",
        "session_id": "sess-abc",
        "attempts": 1,
    }
    fields.update(overrides)
    conn = sqlite3.connect(db)
    try:
        columns = ",".join(fields)
        marks = ",".join("?" * len(fields))
        cur = conn.execute(
            f"INSERT INTO jobs ({columns}) VALUES ({marks})", tuple(fields.values())
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_a_report_can_carry_a_summary(db):
    """The summary is what voice reads. `result` runs to tens of kilobytes —
    a vendor comparison with three tables would dominate the context window
    and the latency budget for a question one sentence answers."""
    job_id = make_job(db)
    assert (
        rows(db, "SELECT summary FROM jobs WHERE id = ?", (job_id,))[0]["summary"]
        == "Compared three vendors; B is cheapest at $4,200/yr."
    )


# ── the summarizer ────────────────────────────────────────


class FakeResponse:
    def __init__(self, text: str):
        class Block:
            type = "text"

        block = Block()
        block.text = text
        self.content = [block]
        self.usage = None


@pytest.fixture
def fake_model(monkeypatch):
    """Stand in for Anthropic. Returns the dict of kwargs the call was made
    with, so tests can assert on the prompt as well as the answer."""
    from app import router

    sent = {}

    def reply_with(text: str = "Compared three vendors; B is cheapest."):
        class FakeMessages:
            def create(self, **kwargs):
                sent.update(kwargs)
                return FakeResponse(text)

        class FakeClient:
            messages = FakeMessages()

        monkeypatch.setattr(router, "_CLIENT", FakeClient())
        return sent

    return reply_with


def test_summarize_returns_the_models_prose(fake_model):
    from app import reports

    fake_model("Compared three vendors; B is cheapest at $4,200/yr.")
    assert reports.summarize("## Vendors\n| B | $4,200 |") == (
        "Compared three vendors; B is cheapest at $4,200/yr."
    )


def test_summarize_sends_the_report_and_bounds_the_call(fake_model):
    """A hung call must not hold the worker, which drains its queue on a
    30-second StartInterval."""
    from app import reports

    sent = fake_model()
    reports.summarize("the whole report text")

    assert "the whole report text" in str(sent["messages"])
    assert sent["timeout"] == 30.0


def test_summarize_swallows_a_model_failure(monkeypatch):
    """The report is already saved and the push is already owed. A summarizer
    that could take either down would be worse than no summarizer."""
    from app import reports, router

    class Exploding:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("upstream is down")

    monkeypatch.setattr(router, "_CLIENT", Exploding())
    assert reports.summarize("anything") is None


def test_summarize_does_not_call_the_model_for_an_empty_report(monkeypatch):
    from app import reports, router

    class Exploding:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise AssertionError("should not have been called")

    monkeypatch.setattr(router, "_CLIENT", Exploding())
    assert reports.summarize("   ") is None
