# Reports You Can Talk About Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the fast path name a specific report — to continue it, or to answer questions about what it said.

**Architecture:** The router's system prompt gains a `REPORTS` block listing the last ten finished jobs by id, built the same way the existing `CALENDAR` block is. `escalate` gains `job_id` (replacing `is_follow_up`) and routes to the `handlers.reply_to_job` already used by the reply box; `query` gains `job_id` and adds one `REPORT (...)` context line for `router.answer()`. The text in that line is a Haiku-written summary stored on `jobs.summary` when the worker finishes a run, falling back to a truncated `result` when absent.

**Tech Stack:** Python 3.12, FastAPI, SQLite (raw `sqlite3`), the Anthropic SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-08-03-reports-in-conversation-design.md`

## Global Constraints

- Python is pinned to **3.12**. Run everything through `uv run`.
- **No new dependencies.** The Anthropic SDK, FastAPI and stdlib are already present.
- Migrations are plain numbered `.sql` applied by `migrate.py`. The next number is **011**.
- **`router.py` must not touch the database.** It formats prompts and makes model calls; callers hand it rows. This is why `recent_reports` lives in `handlers.py` and the reports list is a parameter.
- **The reports read must not add a transaction to `/say`.** Fold it into the transaction that already inserts the utterance. CLAUDE.md records that the four existing transactions are inside the noise; do not make it five.
- **No test may reach the network.** Stub the model by monkeypatching `router._CLIENT` with a fake, the pattern `tests/test_receipt_extract.py:38-41` already uses.
- Model id is `claude-haiku-4-5`, already exported as `router.MODEL`. Do not hardcode it again.
- Replies handed to `/say` are plain text for a TTS engine — no markdown, no lists, no emoji.
- Summaries are **not** recorded in `/metrics`. `usage.record` is a no-op outside a tally scope, so simply do not call it from the worker.

---

## File Structure

| File | Responsibility |
| :-- | :-- |
| `migrations/011_job_summary.sql` | **Create.** Adds `jobs.summary`. |
| `app/reports.py` | **Create.** `summarize(result)` — the one model call, its prompt, its failure policy. |
| `worker/run.py` | **Modify.** Call `summarize` after a successful finish, safely. |
| `app/handlers.py` | **Modify.** `recent_reports()`; `query` learns `job_id`; delete `resume_latest_job`. |
| `app/router.py` | **Modify.** `REPORTS` block; `escalate`/`query` tool schemas; `system_prompt`/`route` signatures. |
| `app/main.py` | **Modify.** Fetch reports in the utterance transaction; escalate on `job_id`. |
| `tests/test_reports_voice.py` | **Create.** The prompt block, escalate-by-id, query-by-id, the summarizer. |
| `tests/test_job_reply.py` | **Modify.** Its `is_follow_up` tests move to `job_id`. |
| `CLAUDE.md` | **Modify.** Record the decisions, including the metered call. |

---

## Task 1: The summary column

**Files:**
- Create: `migrations/011_job_summary.sql`
- Test: `tests/test_reports_voice.py`

**Interfaces:**
- Produces: `jobs.summary TEXT`, NULL by default.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reports_voice.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_reports_voice.py -v`
Expected: FAIL — `sqlite3.OperationalError: table jobs has no column named summary`

- [ ] **Step 3: Write the migration**

Create `migrations/011_job_summary.sql`:

```sql
-- A summary of a report, for talking about it out loud.
--
-- Reports run to tens of kilobytes. Handing one to the answering model would
-- spend the whole context window and the latency budget on a question a
-- sentence answers, so voice reads this instead.
--
-- Written by one Haiku call when the run finishes. NULL is a normal and
-- permanent state, not a pending one: every job that finished before this
-- shipped has no summary and never will, and a summarization call that fails
-- leaves NULL behind. `handlers.query` falls back to the first 1500
-- characters of `result`, which is what makes a backfill script unnecessary.
--
-- The trade is accepted and worth restating: a question about a detail the
-- summary dropped is answered "it didn't say". The detail is in `result`, on
-- screen, which is where detail was always going to live.

ALTER TABLE jobs ADD COLUMN summary TEXT;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_reports_voice.py -v`
Expected: PASS

- [ ] **Step 5: Apply it and confirm**

Run: `uv run migrate.py && uv run migrate.py --status | tail -2`
Expected: `applied 011_job_summary.sql`, then `[x] 011_job_summary.sql`

- [ ] **Step 6: Commit**

```bash
git add migrations/011_job_summary.sql tests/test_reports_voice.py
git commit -m "feat(jobs): a summary column, for talking about a report out loud"
```

---

## Task 2: The summarizer

**Files:**
- Create: `app/reports.py`
- Test: `tests/test_reports_voice.py`

**Interfaces:**
- Consumes: `router._client()` and `router.MODEL`, both existing.
- Produces: `app.reports.summarize(result: str) -> str | None`. Returns None on empty input or any failure — it never raises.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reports_voice.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reports_voice.py -k summarize -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.reports'`

- [ ] **Step 3: Write the module**

Create `app/reports.py`:

```python
"""Summarizing a finished report, so it can be talked about out loud.

One Haiku call per finished deep job. This is the one place the deep path
spends API credit rather than riding the Claude Code subscription — small
money, but a real exception to how the two tiers are funded, so it lives in
its own module rather than hiding inside the worker.

Asking the deep agent to write its own summary would be free and would fail
silently the first time it forgot, which is the same reasoning that kept a
"needs input" marker out of the reply feature.
"""

from app import router

# Long enough to answer a follow-up question, short enough that ten of them
# would still fit in a router prompt if that ever became the design.
TARGET_CHARS = 1000

# A hung call must not hold the worker, which drains its queue on a 30-second
# StartInterval.
TIMEOUT_SECONDS = 30.0

_SYSTEM = f"""\
Summarize this report from a research assistant in about {TARGET_CHARS} \
characters of plain prose.

Cover what was asked, what was found, and any numbers, names or dates that \
someone would ask a follow-up question about. Keep specifics over \
generalities — "vendor B, $4,200 a year" rather than "one vendor was \
cheaper".

The summary is read aloud, so write plain sentences: no markdown, no lists, \
no headings, no emoji. Do not open with "This report" or "The assistant" — \
state what was found."""


def summarize(result: str) -> str | None:
    """One paragraph describing a finished report, or None.

    Never raises. A missing summary is a normal state that `handlers.query`
    already handles by falling back to the report text.
    """
    if not result or not result.strip():
        return None
    try:
        response = router._client().messages.create(
            model=router.MODEL,
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": result}],
            timeout=TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — see docstring; nothing here is fatal
        return None
    # Deliberately no usage.record(): there is no utterance behind a summary,
    # so it stays out of /metrics the same way receipt extraction does.
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reports_voice.py -k summarize -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/reports.py tests/test_reports_voice.py
git commit -m "feat(reports): summarize a finished report in one Haiku call"
```

---

## Task 3: The worker writes summaries

**Files:**
- Modify: `worker/run.py` — the success branch of `run_job`, after `_finish(job["id"], "done", result, None)`
- Test: `tests/test_reports_voice.py`

**Interfaces:**
- Consumes: `app.reports.summarize` (Task 2), `jobs.summary` (Task 1).
- Produces: `worker._store_summary(job_id: int, result: str) -> None`.

Note: the worker process itself keeps `ANTHROPIC_API_KEY` — only the *child* environment is stripped (`worker/run.py:74-91`). So this call authenticates normally.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reports_voice.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reports_voice.py -k "finished_job_gets or failed_summary or summary_that_raises" -v`
Expected: FAIL — `AttributeError: module 'worker.run' has no attribute '_store_summary'`

- [ ] **Step 3: Implement it**

In `worker/run.py`, change the import line:

```python
from app import notify, reports, timeutil
```

Add above `run_job`:

```python
def _store_summary(job_id: int, result: str) -> None:
    """Write the spoken-answer summary, or leave the column NULL.

    Belt and braces: reports.summarize already swallows its own failures, but
    this runs after the report is saved and before the push is sent, and
    neither of those may be taken down by the least important step in the
    sequence.
    """
    try:
        summary = reports.summarize(result)
    except Exception as exc:  # noqa: BLE001
        print(f"job {job_id}: summary failed: {exc}", file=sys.stderr)
        return
    if not summary:
        return
    with transaction() as conn:
        conn.execute("UPDATE jobs SET summary = ? WHERE id = ?", (summary, job_id))
```

In `run_job`, immediately after `_finish(job["id"], "done", result, None)`:

```python
    _finish(job["id"], "done", result, None)
    _store_summary(job["id"], result)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reports_voice.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add worker/run.py tests/test_reports_voice.py
git commit -m "feat(worker): store a summary when a report finishes"
```

---

## Task 4: The router can see your reports

**Files:**
- Modify: `app/handlers.py` (append `recent_reports` near `reply_to_job`)
- Modify: `app/router.py:319-345` (`_SYSTEM`), `:395-403` (`system_prompt`), `:439-456` (`route`)
- Modify: `app/main.py` — the utterance-insert transaction and the `router.route` call
- Test: `tests/test_reports_voice.py`

**Interfaces:**
- Produces:
  - `handlers.recent_reports(conn, limit: int = 10) -> list[dict]` — `[{"id": int, "prompt": str}]`, newest first.
  - `router.reports_table(reports) -> str` — the block body, or `""` when empty.
  - `router.system_prompt(tz_name: str, reports=()) -> str`
  - `router.route(text: str, tz_name: str, reports=()) -> tuple[str, dict]`
- Both new parameters default to empty, so every existing caller and test keeps working.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reports_voice.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reports_voice.py -k "recent_reports or prompt_lists or truncates or block_is_omitted" -v`
Expected: FAIL — `AttributeError: module 'app.handlers' has no attribute 'recent_reports'`

- [ ] **Step 3: Add the query**

In `app/handlers.py`, directly above `def reply_to_job`:

```python
def recent_reports(conn, limit: int = 10) -> list[dict]:
    """The reports the router is shown, newest first.

    Finished only: a queued or running job cannot be resumed and has nothing
    to say yet. Ten is a guess, and it is the number that decides what is
    reachable by voice — everything older is still on the Reports screen with
    a reply box.
    """
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, prompt FROM jobs
                 WHERE status IN ('done','failed')
                 ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ]
```

- [ ] **Step 4: Add the block to the router**

In `app/router.py`, add above `def system_prompt`:

```python
# Sixty characters is enough to tell two reports apart and short enough that
# ten rows stay under about 200 tokens — which keeps the whole prompt under
# Haiku's 4096-token minimum cacheable prefix, so the "caching does not fire
# here" note in CLAUDE.md stays true.
_REPORT_PROMPT_CHARS = 60


def reports_table(reports) -> str:
    """The REPORTS block body. Empty string when there is nothing to list."""
    lines = []
    for report in reports:
        ask = " ".join(str(report["prompt"]).split())
        if len(ask) > _REPORT_PROMPT_CHARS:
            ask = ask[: _REPORT_PROMPT_CHARS - 1].rstrip() + "…"
        lines.append(f"  {report['id']:<5} {ask}")
    return "\n".join(lines)
```

Change `system_prompt`:

```python
def system_prompt(tz_name: str, reports=()) -> str:
    local = timeutil.now(tz_name)
    table = reports_table(reports)
    # Omitted entirely rather than rendered empty — an empty table invites the
    # model to invent an id.
    block = (
        "\nREPORTS — the user's recent deep reports. Refer to one by its id.\n"
        f"{table}\n"
        if table
        else ""
    )
    return _SYSTEM.format(
        now_iso=local.isoformat(timespec="seconds"),
        tz_name=tz_name,
        weekday=local.strftime("%A, %B %-d, %Y"),
        calendar=calendar_table(local),
        reports=block,
    )
```

In `_SYSTEM`, add `{reports}` immediately after the calendar block. The lines currently reading:

```
CALENDAR — copy dates from this table. Do not calculate them yourself.
{calendar}

Resolving times is the most important thing you do:
```

become:

```
CALENDAR — copy dates from this table. Do not calculate them yourself.
{calendar}
{reports}
Resolving times is the most important thing you do:
```

- [ ] **Step 5: Thread it through `route` and `/say`**

In `app/router.py`, change `route`:

```python
def route(text: str, tz_name: str, reports=()) -> tuple[str, dict]:
    """Classify one utterance. Returns (tool_name, tool_input).

    `reports` is passed in rather than read here: this module makes model
    calls and formats prompts, and giving it a database connection would
    make it impossible to test either without one.
    """
    response = _client().messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt(tz_name, reports),
        tools=TOOLS,
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": text}],
    )
```

In `app/main.py`, the `/say` handler currently opens a transaction to insert the utterance and then calls the router. Read the reports inside that same transaction — not a new one:

```python
    with transaction() as conn:
        utterance_id = int(
            conn.execute(
                "INSERT INTO utterances (text, client) VALUES (?,?)",
                (req.text, req.client),
            ).lastrowid
        )
        # Same transaction as the insert: the router needs these, and CLAUDE.md
        # records that /say's four SQLite transactions are inside the noise.
        # Making it five to fetch ten rows would not be.
        reports = handlers.recent_reports(conn)

    try:
        tool, args = router.route(req.text, tz_name, reports)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_reports_voice.py -v && uv run pytest tests/test_core.py -q`
Expected: PASS. `test_core.py` is included because it exercises `/say` and the router prompt.

- [ ] **Step 7: Commit**

```bash
git add app/router.py app/handlers.py app/main.py tests/test_reports_voice.py
git commit -m "feat(router): show the last ten reports in the system prompt"
```

---

## Task 5: Escalate by id

**Files:**
- Modify: `app/router.py` — the `escalate` entry in `TOOLS`
- Modify: `app/main.py` — the `if tool == "escalate":` block
- Modify: `app/handlers.py` — delete `resume_latest_job`
- Modify: `tests/test_job_reply.py` — its three `is_follow_up` tests
- Test: `tests/test_reports_voice.py`

**Interfaces:**
- Consumes: `handlers.reply_to_job` (existing, returns `"ok" | "missing" | "live"`), `recent_reports` (Task 4).
- Produces: `escalate` takes `restated_task` and optional `job_id: int`. `is_follow_up` and `handlers.resume_latest_job` cease to exist.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reports_voice.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reports_voice.py -k escalate -v`
Expected: FAIL — the resume test inserts a third job instead of re-queueing the named one.

- [ ] **Step 3: Change the tool schema**

In `app/router.py`, replace the `is_follow_up` property of `escalate` with:

```python
                "job_id": {
                    "type": "integer",
                    "description": (
                        "The id of an existing report from the REPORTS list, "
                        "when the user is answering it or asking it to carry "
                        "on — 'go with the second one', 'answer the vendor "
                        "report with B', 'go deeper on that'. Omit for new "
                        "work. Use an id only if it appears in REPORTS."
                    ),
                },
```

- [ ] **Step 4: Rewrite the escalate block**

In `app/main.py`, replace everything from `if tool == "escalate":` down to and
including the `reply = (...)` assignment. The four lines after it — `latency =
_finish(...)`, `synth.prefetch(reply)`, and the `return {...}` — stay exactly
as they are.

```python
    if tool == "escalate":
        task = args.get("restated_task", req.text)
        named = args.get("job_id")
        outcome = "missing"
        with transaction() as conn:
            # A named report is answered through the same helper the reply box
            # and the notification action use, so a spoken answer and a typed
            # one are indistinguishable by the time they reach the database.
            if named is not None:
                outcome = handlers.reply_to_job(conn, int(named), task)
            if outcome in ("ok", "live"):
                # Both mean the named report is the answer to "which job is
                # this about" — one because we re-queued it, one because it
                # was already working.
                job_id = int(named)
            else:
                job_id = int(
                    conn.execute(
                        "INSERT INTO jobs (utterance_id, prompt) VALUES (?,?)",
                        (utterance_id, task),
                    ).lastrowid
                )
        reply = {
            "ok": "Picking up where we left off. I'll ping you.",
            "live": "That one's still working. I'll leave it be.",
        }.get(outcome, "On it. I'll ping you when it's done.")
```

- [ ] **Step 5: Delete the guesser**

In `app/handlers.py`, delete `resume_latest_job` entirely. Nothing calls it once Task 5 lands — it existed only to guess which report was meant.

- [ ] **Step 6: Update the tests that assumed guessing**

In `tests/test_job_reply.py`, the `── the voice path ──` section tests `is_follow_up`, which no longer exists. Replace its `spoken` fixture and three tests with:

```python
@pytest.fixture
def spoken(client, monkeypatch):
    """A /say client whose router answer is ours to choose."""
    from app import router

    def route_as(tool, args):
        monkeypatch.setattr(router, "route", lambda text, tz, reports=(): (tool, args))

    return client, route_as


def test_a_spoken_answer_reaches_the_same_helper_as_a_typed_one(spoken, db):
    """The point of routing voice through reply_to_job: one mechanic, so the
    database cannot tell how you answered."""
    client, route_as = spoken
    job_id = make_job(db)
    route_as("escalate", {"restated_task": "go with B", "job_id": job_id})

    client.post("/say", json={"text": "go with B", "client": "ios"})

    job = rows(db, "SELECT status, pending_input FROM jobs WHERE id = ?", (job_id,))[0]
    assert job["status"] == "queued"
    assert job["pending_input"] == "go with B"


def test_a_new_deep_task_still_inserts_its_own_job(spoken, db):
    """Only a named report resumes. A fresh task must not overwrite the last
    report you asked for."""
    client, route_as = spoken
    existing = make_job(db)
    route_as("escalate", {"restated_task": "research desks"})

    body = client.post("/say", json={"text": "research desks", "client": "ios"}).json()

    assert body["job_id"] != existing
    assert rows(db, "SELECT COUNT(*) AS n FROM jobs")[0]["n"] == 2
```

Delete `test_a_spoken_follow_up_resumes_the_report_it_belongs_to` and
`test_a_spoken_follow_up_with_nothing_to_resume_starts_a_job` — the behaviour
they pinned (guess the most recent) is the behaviour this task removes, and
`tests/test_reports_voice.py` covers the replacement.

Also delete the three `resume_latest_job` tests from `tests/test_job_reply.py`:
`test_resume_latest_picks_the_most_recent_finished_job`,
`test_resume_latest_skips_a_job_with_no_session`, and
`test_resume_latest_returns_none_when_there_is_nothing_to_resume`.

- [ ] **Step 7: Run both suites**

Run: `uv run pytest tests/test_reports_voice.py tests/test_job_reply.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/router.py app/main.py app/handlers.py tests/test_reports_voice.py tests/test_job_reply.py
git commit -m "feat(say): answer a named report instead of guessing at the newest"
```

---

## Task 6: Asking what a report said

**Files:**
- Modify: `app/router.py` — the `query` entry in `TOOLS`
- Modify: `app/handlers.py:500-560` (`query`)
- Test: `tests/test_reports_voice.py`

**Interfaces:**
- Consumes: `jobs.summary` (Task 1).
- Produces: `query` accepts optional `job_id: int`; `handlers._report_line(conn, job_id) -> str | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_reports_voice.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reports_voice.py -k query_with -v`
Expected: FAIL — no `REPORT (` line in the captured context.

- [ ] **Step 3: Add the tool parameter**

In `app/router.py`, add to the `query` tool's `properties`:

```python
                "job_id": {
                    "type": "integer",
                    "description": (
                        "The id of a report from REPORTS, when the question is "
                        "about what one of them found — 'what did that report "
                        "say about pricing', 'what did you find out'. Omit "
                        "otherwise. Use an id only if it appears in REPORTS."
                    ),
                },
```

- [ ] **Step 4: Add the context line**

In `app/handlers.py`, add above `def query`:

```python
# Enough to answer a follow-up, bounded so a 14 KB report cannot crowd out the
# notes and mail that the same question may also need.
_REPORT_FALLBACK_CHARS = 1500


def _report_line(conn, job_id) -> str | None:
    """One context line about a report, or None when there is nothing to say.

    Prefers the stored summary and falls back to the head of the report
    itself, which is what lets reports that finished before summaries existed
    still answer questions.
    """
    if job_id is None:
        return None
    row = conn.execute(
        "SELECT prompt, summary, result FROM jobs WHERE id = ?", (int(job_id),)
    ).fetchone()
    if row is None:
        return None
    body = (row["summary"] or "").strip() or (row["result"] or "").strip()
    if not body:
        return None
    return f"REPORT ({row['prompt']}): {body[:_REPORT_FALLBACK_CHARS]}"
```

In `query`, immediately after the templated short-circuit block and before
`days = max(8, ...)`, seed the context lines with the report:

```python
    lines: list[str] = []
    report = _report_line(conn, args.get("job_id"))
    if report:
        lines.append(report)
```

`query` already declares `lines: list[str] = []` further down, immediately
before the `for e in agenda["events"]:` loop. **Delete that second
declaration** — one line — so the events, notes and email lines append to the
list that already holds the report instead of replacing it. Leaving both
declarations in place is the bug this step exists to avoid: the report line
would be built, discarded, and never reach the model.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_reports_voice.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/router.py app/handlers.py tests/test_reports_voice.py
git commit -m "feat(query): answer questions from a report's summary"
```

---

## Task 7: Record the decisions

**Files:**
- Modify: `CLAUDE.md` — the fast-path router tool table, the `Replying to a report` section, and the deep-path funding note

- [ ] **Step 1: Update the router tool table**

In `CLAUDE.md`, under `### Fast-path router`, change the two affected rows:

```markdown
| `query` | `question`, `window_days?`, `job_id?` |
| `escalate` | `restated_task`, `job_id?` — routes to the deep path |
```

- [ ] **Step 2: Extend the reply section**

In `CLAUDE.md`, in `## Replying to a report`, replace the bullet beginning
**"A spoken follow-up takes the same path."** with:

```markdown
- **A spoken answer names its report.** The router's system prompt carries a
  `REPORTS` block — the last ten finished jobs as `id  original ask` — and
  `escalate` returns a `job_id`, which routes to the same `handlers.reply_to_job`
  the reply box calls. It briefly used an `is_follow_up` boolean resolving to
  "the most recent finished job", which was right often enough to feel fine and
  wrong in the case you would care about: answering this morning's report after
  asking for something else at lunch. Naming beats guessing, so the boolean and
  `resume_latest_job` were both deleted rather than kept alongside.
- **Answering a report that is already running says so.** `reply_to_job`
  returns `live`, and the templated reply is "That one's still working. I'll
  leave it be." Quietly starting a second piece of work you did not ask for is
  the failure this avoids.
- **`query` can read a report too.** With a `job_id` it adds one
  `REPORT (<ask>): <summary>` line beside the `NOTE:` and `EMAIL:` lines, and
  `router.answer` speaks from it — so reports are another thing the assistant
  knows about rather than a mode it enters, and one question can draw on a
  report and your mail together.
- **Voice reads the summary, not the report.** `jobs.summary` is written by one
  Haiku call in `app/reports.py` when a run finishes. A report runs to tens of
  kilobytes and would spend the whole context and latency budget on a question
  a sentence answers. **The accepted cost: a question about a detail the
  summary dropped is answered "it didn't say"** — the detail is in `result`, on
  screen. NULL is normal and permanent, not pending; `query` falls back to the
  first 1500 characters of `result`, which is why there was no backfill.
- **The summary is the one metered call in the deep path.** Everything else
  there rides the Claude Code subscription; this is Haiku against a few
  thousand tokens, billed to API credit. Fractions of a cent per job and deep
  jobs are rare, but it is a real exception to how the two tiers are funded.
  It stays out of `/metrics`, which is per-utterance, and a summary has no
  utterance behind it. It cannot fail a job: `reports.summarize` swallows its
  own errors and `worker._store_summary` swallows them again, because the
  report is already saved and the push is already owed.
- **Ten reports is the reach of voice.** Older ones are unreachable by voice and
  still repliable on the Reports screen. It is one constant,
  `handlers.recent_reports`'s `limit`.
```

- [ ] **Step 3: Correct the deep-path funding claim**

In `CLAUDE.md`, the `/metrics` paragraph under `### Supporting endpoints` ends
"the deep path runs on the Claude Code subscription, not API credits." Append
to that sentence:

```markdown
— with one exception, the per-report summary call described under
[Replying to a report](#replying-to-a-report).
```

- [ ] **Step 4: Full verification**

Run:
```bash
uv run pytest -q
```
Expected: every test passes except the pre-existing
`test_pantry_inventory_reports_days_left_not_iso_dates`, which fails on a clean
tree for unrelated reasons (a UTC/local off-by-one in the pantry day count).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: naming a report by voice, and the summary that makes it speakable"
```

---

## Manual verification

The router's judgment cannot be unit-tested — every test above stubs it. On the
Mini, after `uv run migrate.py` and restarting the API and worker:

1. Ask for two different deep tasks and let both finish.
2. Say *"what did the first one find?"* — expect a spoken answer in about a
   second and a half, drawn from the summary, with **no** new report appearing.
3. Say *"answer the vendor one, go with B"* — expect "Picking up where we left
   off" and that specific report going back to running, not the newest one.
4. While it runs, say the same thing again — expect "That one's still working."
5. Check `sqlite3 "$JARVIS_DB" "SELECT id, length(summary) FROM jobs ORDER BY id DESC LIMIT 3"`
   to confirm summaries are being written and are roughly 1000 characters.
