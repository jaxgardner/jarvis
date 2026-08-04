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


# ── the worker writes them ────────────────────────────────


@pytest.fixture
def worker_db(db, monkeypatch):
    """The worker with its push stubbed, pointed at the test database."""
    from worker import run as worker

    monkeypatch.setattr(worker.notify, "push", lambda *a, **k: True)
    return worker


def test_a_finished_job_gets_a_summary(worker_db, db, fake_model):
    fake_model("Compared three vendors; B is cheapest at $4,200/yr.")
    job_id = make_job(db, summary=None)

    worker_db._store_summary(job_id, "## Vendors\n| B | $4,200 |")

    assert (
        rows(db, "SELECT summary FROM jobs WHERE id = ?", (job_id,))[0]["summary"]
        == "Compared three vendors; B is cheapest at $4,200/yr."
    )


def test_a_failed_summary_leaves_the_column_null_and_does_not_raise(
    worker_db, db, monkeypatch
):
    """The report is saved and the push is owed. Summarizing is the least
    important thing happening at this moment and must behave like it."""
    from app import reports

    monkeypatch.setattr(reports, "summarize", lambda result: None)
    job_id = make_job(db, summary=None)

    worker_db._store_summary(job_id, "anything")

    assert rows(db, "SELECT summary FROM jobs WHERE id = ?", (job_id,))[0][
        "summary"
    ] is None


def test_a_summary_that_raises_still_does_not_reach_the_caller(
    worker_db, db, monkeypatch
):
    from app import reports

    def explode(result):
        raise RuntimeError("upstream is down")

    monkeypatch.setattr(reports, "summarize", explode)
    job_id = make_job(db, summary=None)

    worker_db._store_summary(job_id, "anything")  # must not raise

    assert rows(db, "SELECT summary FROM jobs WHERE id = ?", (job_id,))[0][
        "summary"
    ] is None


def test_a_failing_job_is_never_summarized(worker_db, db, monkeypatch):
    """A failed run has no result to summarize, and paying for a model call
    to describe nothing is the kind of waste that hides in a retry loop."""
    called = []

    monkeypatch.setattr(
        worker_db, "_store_summary", lambda job_id, result: called.append(job_id)
    )
    monkeypatch.setattr(worker_db, "MAX_ATTEMPTS", 1)

    worker_db._handle_failure(
        {"id": make_job(db, status="running"), "attempts": 1}, "timed out"
    )

    assert called == []


# ── the prompt block ──────────────────────────────────────


def test_recent_reports_are_newest_first(db):
    from app import handlers
    from app.db import transaction

    make_job(db, prompt="older")
    make_job(db, prompt="newer")

    with transaction() as conn:
        listed = handlers.recent_reports(conn)

    assert [r["prompt"] for r in listed] == ["newer", "older"]


def test_recent_reports_excludes_unfinished_work(db):
    """A queued or running report cannot be resumed and has nothing to say."""
    from app import handlers
    from app.db import transaction

    make_job(db, prompt="running one", status="running")
    make_job(db, prompt="done one")

    with transaction() as conn:
        assert [r["prompt"] for r in handlers.recent_reports(conn)] == ["done one"]


def test_recent_reports_caps_at_ten(db):
    from app import handlers
    from app.db import transaction

    for n in range(12):
        make_job(db, prompt=f"report {n}")

    with transaction() as conn:
        assert len(handlers.recent_reports(conn)) == 10


def test_the_prompt_lists_reports_by_id():
    from app import router

    prompt = router.system_prompt(
        "America/Denver",
        [{"id": 27, "prompt": "Compare the three vendors"}],
    )

    assert "REPORTS" in prompt
    assert "27" in prompt
    assert "Compare the three vendors" in prompt


def test_the_prompt_truncates_a_long_ask():
    from app import router

    table = router.reports_table([{"id": 3, "prompt": "x" * 200}])
    assert len(table.splitlines()[0]) < 80


def test_the_block_is_omitted_when_there_are_no_reports():
    """A fresh install should not spend tokens being told about nothing."""
    from app import router

    assert "REPORTS" not in router.system_prompt("America/Denver", [])


def test_the_calendar_block_still_survives():
    """Regression: the reports block is inserted into the same template."""
    from app import router

    assert "CALENDAR" in router.system_prompt("America/Denver", [])


# ── escalate by id ────────────────────────────────────────


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {SHARED}"})
    return c


@pytest.fixture
def spoken(client, monkeypatch):
    """A /say client whose router answer is ours to choose."""
    from app import router

    def route_as(tool, args):
        monkeypatch.setattr(router, "route", lambda text, tz, reports=(): (tool, args))

    return client, route_as


def test_escalate_with_a_job_id_resumes_that_report(spoken, db):
    client, route_as = spoken
    older = make_job(db, prompt="older")
    make_job(db, prompt="newer")
    route_as("escalate", {"restated_task": "go with B", "job_id": older})

    body = client.post("/say", json={"text": "go with B", "client": "ios"}).json()

    assert body["job_id"] == older
    assert rows(db, "SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 2
    resumed = rows(db, "SELECT status, pending_input FROM jobs WHERE id = ?", (older,))[0]
    assert resumed["status"] == "queued"
    assert resumed["pending_input"] == "go with B"


def test_escalate_without_a_job_id_starts_new_work(spoken, db):
    client, route_as = spoken
    existing = make_job(db)
    route_as("escalate", {"restated_task": "research desks"})

    body = client.post("/say", json={"text": "research desks", "client": "ios"}).json()

    assert body["job_id"] != existing
    assert rows(db, "SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 2


def test_escalate_on_a_live_report_says_so_and_changes_nothing(spoken, db):
    """Answering something already working should tell you, not quietly start
    a second piece of work you did not ask for."""
    client, route_as = spoken
    job_id = make_job(db, status="running")
    route_as("escalate", {"restated_task": "go with B", "job_id": job_id})

    body = client.post("/say", json={"text": "go with B", "client": "ios"}).json()

    assert "still working" in body["reply"]
    assert rows(db, "SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 1
    assert rows(db, "SELECT pending_input FROM jobs WHERE id = ?", (job_id,))[0][
        "pending_input"
    ] is None


def test_escalate_with_an_unknown_job_id_starts_new_work(spoken, db):
    client, route_as = spoken
    route_as("escalate", {"restated_task": "research desks", "job_id": 999})

    body = client.post("/say", json={"text": "research desks", "client": "ios"}).json()

    assert rows(db, "SELECT prompt FROM jobs WHERE id = ?", (body["job_id"],))[0][
        "prompt"
    ] == "research desks"


# ── asking what a report said ─────────────────────────────


@pytest.fixture
def captured_answer(monkeypatch):
    """Capture the context handed to router.answer instead of calling it."""
    from app import router

    seen = {}

    def fake_answer(question, context, tz_name):
        seen["question"] = question
        seen["context"] = context
        return "It said vendor B."

    monkeypatch.setattr(router, "answer", fake_answer)
    return seen


def test_query_with_a_job_id_puts_the_summary_in_context(db, captured_answer):
    from app import handlers
    from app.db import transaction

    job_id = make_job(db)
    with transaction() as conn:
        handlers.query(
            conn,
            None,
            {"question": "what did it say about pricing", "job_id": job_id},
            "America/Denver",
        )

    assert "REPORT (Compare the three vendors):" in captured_answer["context"]
    assert "B is cheapest at $4,200/yr" in captured_answer["context"]


def test_query_falls_back_to_the_report_when_there_is_no_summary(db, captured_answer):
    """Old reports and failed summarizations both leave NULL. Falling back is
    what makes a backfill script unnecessary."""
    from app import handlers
    from app.db import transaction

    job_id = make_job(db, summary=None, result="The raw report body.")
    with transaction() as conn:
        handlers.query(
            conn, None, {"question": "what did it say", "job_id": job_id}, "America/Denver"
        )

    assert "The raw report body." in captured_answer["context"]


def test_query_truncates_a_long_report_it_falls_back_to(db, captured_answer):
    from app import handlers
    from app.db import transaction

    job_id = make_job(db, summary=None, result="x" * 5000)
    with transaction() as conn:
        handlers.query(
            conn, None, {"question": "what did it say", "job_id": job_id}, "America/Denver"
        )

    assert len(captured_answer["context"]) < 2500


def test_query_with_an_unknown_job_id_still_answers(db, captured_answer):
    from app import handlers
    from app.db import transaction

    with transaction() as conn:
        handlers.query(
            conn, None, {"question": "what did it say", "job_id": 999}, "America/Denver"
        )

    assert "REPORT" not in captured_answer["context"]


def test_query_omits_the_line_for_a_report_with_nothing_in_it(db, captured_answer):
    """A failed job has neither summary nor result. An empty REPORT () line
    would invite the model to invent what belongs there."""
    from app import handlers
    from app.db import transaction

    job_id = make_job(db, status="failed", summary=None, result=None)
    with transaction() as conn:
        handlers.query(
            conn, None, {"question": "what did it say", "job_id": job_id}, "America/Denver"
        )

    assert "REPORT" not in captured_answer["context"]
