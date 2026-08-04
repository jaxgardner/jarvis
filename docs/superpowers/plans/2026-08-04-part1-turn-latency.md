# Part 1 — The turn: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut endpoint-to-first-syllable from ~3000ms to ~1500ms by measuring the whole turn, shortening the endpointer, and removing `query`'s second model call.

**Architecture:** Three independent levers. (1) A new client-measured `turn_ms` reported by a fire-and-forget `POST /turns`, because `latency_ms` times `/say` and misses ~1550ms of what the user feels. (2) Pre-retrieval: run the note/email search *before* the router call and hand it in as a `CONTEXT` block, so the router answers directly instead of routing to `query` and paying a second 660ms round trip. (3) Two small cuts — the endpointer timer and the first-chunk TTS floor.

**Tech Stack:** Python 3.12, FastAPI, SQLite/FTS5, Swift 6 / SwiftUI, `OSSignposter`.

## Global Constraints

- **Python 3.12**, pinned. Run everything with `uv run`.
- **Timestamps are ISO 8601 with offset.** `_at` is an instant, `_on` is a bare date.
- **`app.db.connect()` only** — never a raw `sqlite3.connect()`.
- **`reply` is one plain-text line.** No markdown, no newlines.
- **`CONTEXT` goes in the LIVE half of the system prompt, never the static half.** The static half is byte-stable and carries the `cache_control` marker; putting a question-derived block in it kills prompt caching silently and permanently. `tests/test_router_prompt.py` asserts this and must keep passing.
- **Commit after every task.**

## File Structure

| File | Responsibility | Action |
| :-- | :-- | :-- |
| `migrations/016_turn_timings.sql` | `turn_ms` + `timings` columns | Create |
| `app/handlers.py` | `context_block()` — pre-retrieval, reusing `_search_notes` | Modify |
| `app/router.py` | Carry `context` through `_live_half`/`system_blocks`/`route` | Modify |
| `app/main.py` | Build context, `POST /turns`, `/metrics` turn block, prefetch order | Modify |
| `speech/segment.py` | Lower the comma floor for the first chunk | Modify |
| `ios/Jarvis/SetupView.swift` | 0.45s pause option and default | Modify |
| `ios/Jarvis/TalkView.swift` | Start the turn clock, report it | Modify |
| `ios/Jarvis/JarvisAPI.swift` | `reportTurn(utteranceId:turnMs:)` | Modify |
| `ios/Jarvis/HealthView.swift` | Show turn p50/p95 | Modify |
| `tests/test_context_block.py` | Pre-retrieval, offline | Create |
| `tests/test_context_routing.py` | Pre-retrieval end-to-end + the safety property. Live | Create |
| `tests/test_turns_api.py` | `POST /turns`, `/metrics` turn block | Create |

**Test conventions in this repo, which differ from the pytest defaults:**
`tests/conftest.py` defines **no fixtures** — it only redirects `JARVIS_DB` to
a temp path. Each test file builds its own `client` fixture, and live tests
gate with a module-level `pytestmark = pytest.mark.skipif(...)` rather than a
custom marker. Follow `tests/test_utterances.py`.

---

### Task 1: Turn timing columns

**Files:**
- Create: `migrations/016_turn_timings.sql`
- Test: `tests/test_turns_api.py`

**Interfaces:**
- Produces: `utterances.turn_ms INTEGER` (nullable), `utterances.timings TEXT` (nullable JSON).

- [ ] **Step 1: Write the failing test**

Create `tests/test_turns_api.py`:

```python
"""The turn is the number the user feels; latency_ms is not it.

Offline for the schema and endpoint tests. There is no shared conftest
fixture in this repo — conftest.py only redirects JARVIS_DB — so each test
file builds its own client, following tests/test_utterances.py.
"""

import sqlite3

import pytest

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_turns_api.py -v`
Expected: FAIL — `assert 'turn_ms' in cols`

- [ ] **Step 3: Write the migration**

Create `migrations/016_turn_timings.sql`:

```sql
-- The turn, as opposed to /say.
--
-- latency_ms times the endpoint and misses roughly 1550ms of what the user
-- actually waits through: 800ms of endpointer before /say is called at all,
-- and ~640ms of synthesis after it returns. Measured 2026-08-04, a one-call
-- utterance is 1410ms of /say inside a ~3000ms turn. Optimising the smaller
-- number is how a system gets faster on paper and no faster in the room.
--
-- Both nullable, permanently. The Shortcut client has no microphone and so
-- has no turn to report; that is a client without a mic rather than a
-- measurement that went missing.
ALTER TABLE utterances ADD COLUMN turn_ms INTEGER;

-- Server-side hop breakdown as JSON: router, handler, synth-start. Written by
-- _say, returned as Server-Timing, and kept so a slow turn can be attributed
-- after the fact rather than reproduced.
ALTER TABLE utterances ADD COLUMN timings TEXT;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_turns_api.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add migrations/016_turn_timings.sql tests/test_turns_api.py
git commit -m "feat: turn_ms and timings columns on utterances"
```

---

### Task 2: `POST /turns`

**Files:**
- Modify: `app/main.py` (add endpoint near `/metrics`)
- Test: `tests/test_turns_api.py`

**Interfaces:**
- Consumes: `utterances.turn_ms` from Task 1.
- Produces: `POST /turns` accepting `{"utterance_id": int, "turn_ms": int}`, returning 204.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_turns_api.py`:

```python
from app import config
from app.db import connect


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_turns_api.py::test_turn_is_recorded -v`
Expected: FAIL — 404, no route for `/turns`

- [ ] **Step 3: Implement the endpoint**

In `app/main.py`, add the request model beside the other Pydantic models:

```python
class TurnReport(BaseModel):
    utterance_id: int
    turn_ms: int
```

And the endpoint, next to `/metrics`:

```python
@app.post("/turns", status_code=204)
def report_turn(report: TurnReport, _: Principal = Depends(require_token)) -> Response:
    """What the turn actually cost, measured on the phone.

    From the endpointer firing to the first audio buffer being scheduled —
    the whole thing, including the 800ms of endpointer and the synthesis
    after /say returns, neither of which latency_ms can see.

    Fire-and-forget, and deliberately reported after playback has started so
    it cannot sit on the critical path it is measuring. An unknown id is
    ignored rather than raised: this arrives after the user has been spoken
    to, so there is nothing a failure could usefully change.
    """
    with transaction() as conn:
        conn.execute(
            "UPDATE utterances SET turn_ms = ? WHERE id = ?",
            (report.turn_ms, report.utterance_id),
        )
    return Response(status_code=204)
```

Add `Response` to the `fastapi` imports if it is not already there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_turns_api.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_turns_api.py
git commit -m "feat: POST /turns records client-measured turn latency"
```

---

### Task 3: Turn block in `/metrics`

**Files:**
- Modify: `app/main.py:1002` (`metrics`)
- Test: `tests/test_turns_api.py`

**Interfaces:**
- Consumes: `utterances.turn_ms`.
- Produces: `metrics()["turn"]` → `{"count": int, "p50": int, "p95": int, "max": int}` or `{"count": 0}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_turns_api.py`:

```python
def test_metrics_reports_turn(client):
    for ms in (1000, 1500, 2000):
        client.post("/turns", json={"utterance_id": _utterance(client), "turn_ms": ms})
    body = client.get("/metrics").json()
    assert body["turn"]["count"] == 3
    assert body["turn"]["p50"] == 1500
    assert body["turn"]["max"] == 2000


def test_metrics_turn_counts_only_reported(client):
    """Counted only over utterances that reported one. A Shortcut has no
    microphone, and folding its silence in as a zero would report a headline
    number nobody experienced."""
    before = client.get("/metrics").json()["turn"]["count"]
    _utterance(client)  # written, never reported
    after = client.get("/metrics").json()["turn"]["count"]
    assert after == before
```

**Ordering note:** `test_metrics_reports_turn` asserts an exact count, so it
must be the only test writing turns in its module. The module-scoped `client`
shares one database across the file. If you add another turn-writing test,
make both assertions relative like `test_metrics_turn_counts_only_reported`
rather than absolute.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_turns_api.py::test_metrics_reports_turn -v`
Expected: FAIL — `KeyError: 'turn'`

- [ ] **Step 3: Implement**

In `app/main.py`, inside `metrics()`, after the `for route_name in ("fast", "deep")` loop and before `out["spend"] = ...`:

```python
        # The turn, beside the endpoint. Counted only over utterances that
        # reported one — a Shortcut has no microphone, and folding its
        # silence in as a zero would report a headline number nobody
        # experienced.
        turns = [
            r["turn_ms"]
            for r in conn.execute(
                """SELECT turn_ms FROM utterances
                     WHERE turn_ms IS NOT NULL
                       AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)
                     ORDER BY turn_ms""",
                (window,),
            ).fetchall()
        ]
        out["turn"] = (
            {
                "count": len(turns),
                "p50": turns[len(turns) // 2],
                "p95": turns[min(len(turns) - 1, int(len(turns) * 0.95))],
                "max": turns[-1],
            }
            if turns
            else {"count": 0}
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_turns_api.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_turns_api.py
git commit -m "feat: /metrics reports turn p50/p95 beside latency"
```

---

### Task 4: `context_block()` — pre-retrieval

**Files:**
- Modify: `app/handlers.py` (add after `_search_notes`, around line 850)
- Test: `tests/test_context_block.py`

**Interfaces:**
- Consumes: `handlers._search_notes(conn, question, limit=10)` at `app/handlers.py:821` — `list[dict]` with a `body` key, FTS5 with a LIKE fallback, already filters soft-deleted notes.
- Consumes: `handlers.search_email(conn, question, limit=6)` at `app/handlers.py:853` — **already exists and is public.** Returns `list[dict]` with `sender, subject, snippet, received_at, is_unread`. Do not write a new one.
- Produces: `handlers.context_block(conn, text: str, limit: int = 5) -> str` — newline-joined `NOTE: …` / `EMAIL: …` lines, or `""`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_context_block.py`:

```python
"""Pre-retrieval: search before the router call, not after it.

query costs 2675ms against 1410ms for one call, and the whole difference is
that it searches *after* the model has decided to search. The search itself
is ~3ms. So we do it first and hand the result in.
"""

import sqlite3

import pytest

from app import handlers
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    """Every migration, so email_fts exists — the `conn` fixture in
    test_core.py applies 001_init.sql alone and search_email would fall
    through its OperationalError guard and silently return []."""
    path = tmp_path / "context.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def _note(conn, body):
    conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
    conn.commit()


def test_matching_note_appears(conn):
    _note(conn, "the back garden fence needs a new post on the left side")
    block = handlers.context_block(conn, "what did I say about the fence")
    assert "NOTE:" in block
    assert "fence" in block


def test_no_match_is_empty_string(conn):
    """Empty, not a bare heading. A CONTEXT: label with nothing under it
    invites the model to answer from a block that contains nothing."""
    _note(conn, "buy milk")
    block = handlers.context_block(conn, "what did I say about kubernetes")
    assert block == ""


def test_soft_deleted_note_is_excluded(conn):
    conn.execute("INSERT INTO notes (body) VALUES ('the fence is finished')")
    conn.execute("UPDATE notes SET deleted_at = '2026-08-04T00:00:00Z'")
    conn.commit()
    assert handlers.context_block(conn, "what about the fence") == ""


def test_limit_is_respected(conn):
    for i in range(12):
        _note(conn, f"the fence note number {i}")
    block = handlers.context_block(conn, "fence", limit=5)
    assert len([l for l in block.splitlines() if l.startswith("NOTE:")]) <= 5


def test_lines_are_single_line_each(conn):
    """A newline inside a context line would misalign the block the model
    reads, the same reason /say squeezes its reply to one line."""
    _note(conn, "fence plan:\nreplace posts\nthen paint")
    block = handlers.context_block(conn, "fence")
    assert block
    for line in block.splitlines():
        assert line.startswith(("NOTE:", "EMAIL:"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_block.py -v`
Expected: FAIL — `AttributeError: module 'app.handlers' has no attribute 'context_block'`

- [ ] **Step 3: Implement**

In `app/handlers.py`, after `_search_notes`:

```python
def context_block(conn, text: str, limit: int = 5) -> str:
    """What the archive has to say about this utterance, fetched before the
    router sees it.

    `query` costs a second model call — measured 2675ms against 1410ms — and
    the only reason is that it searches after the model has decided to
    search. The search is ~3ms. Doing it first lets the router answer through
    `answer` in the call it had to make anyway.

    Unlike TODAY, this block *is* derived from the user's words, and that is
    a real difference: TODAY is safe because nothing the user said can put a
    wrong row in it. The safety here comes from elsewhere — `query` stays
    reachable and the prompt calls these candidates rather than answers, so a
    miss degrades to the two-call path that exists today. The worst case is
    the current case.

    Returns "" when nothing matches, so the caller drops the heading entirely
    rather than showing an empty one.
    """
    # Both searches already exist and are already used by `query`. Reusing
    # them rather than writing a second pair is what keeps the block the
    # router sees and the block `query` builds from saying the same thing —
    # two formatters would drift, and the drift would surface as an answer
    # that changed depending on which path it took.
    lines: list[str] = []
    for note in _search_notes(conn, text, limit):
        body = " ".join(str(note["body"]).split())
        lines.append(f"NOTE: {body}")

    for mail in search_email(conn, text, limit):
        subject = " ".join(str(mail["subject"] or "").split())
        sender = " ".join(str(mail["sender"] or "").split())
        lines.append(f"EMAIL: from {sender} — {subject}")

    return "\n".join(lines[:limit])
```

`search_email` is defined below `context_block` in the file. Python resolves
it at call time, not definition time, so no reordering is needed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_context_block.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add app/handlers.py tests/test_context_block.py
git commit -m "feat: context_block pre-retrieves notes and mail before routing"
```

---

### Task 5: Carry `context` into the live half of the prompt

**Files:**
- Modify: `app/router.py:614` (`system_blocks`), `:632` (`system_prompt`), `:638` (`_live_half`), `:711` (`route`)
- Test: `tests/test_router_prompt.py`

**Interfaces:**
- Consumes: `handlers.context_block()` output from Task 4.
- Produces: `router.route(text, tz_name, reports=(), projects=(), today="", context="")` — `context` appended last and defaulted, so every existing caller and test keeps working unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_router_prompt.py`:

```python
def test_context_is_in_the_live_half_not_the_static_one():
    """The static block is byte-stable and carries the cache_control marker.
    A question-derived block inside it kills prompt caching silently and
    permanently — no error, both counters zero, and it reads as a working
    optimisation forever."""
    blocks = router.system_blocks(
        "America/Denver", context="NOTE: the fence needs a post"
    )
    static, live = blocks[0], blocks[1]
    assert "cache_control" in static
    assert "fence" not in static["text"]
    assert "fence" in live["text"]


def test_static_half_is_byte_identical_across_contexts():
    a = router.system_blocks("America/Denver", context="NOTE: one thing")
    b = router.system_blocks("Europe/London", context="NOTE: a different thing")
    assert a[0]["text"] == b[0]["text"]


def test_empty_context_omits_the_heading():
    """An empty CONTEXT: heading invites an answer from a block holding
    nothing."""
    live = router.system_blocks("America/Denver", context="")[1]["text"]
    assert "CONTEXT" not in live
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_router_prompt.py::test_context_is_in_the_live_half_not_the_static_one -v`
Expected: FAIL — `TypeError: system_blocks() got an unexpected keyword argument 'context'`

- [ ] **Step 3: Implement**

In `app/router.py`, thread `context` through all four functions. Signatures become:

```python
def system_blocks(tz_name: str, reports=(), projects=(), today: str = "", context: str = "") -> list[dict]:
def system_prompt(tz_name: str, reports=(), projects=(), today: str = "", context: str = "") -> str:
def _live_half(tz_name: str, reports=(), projects=(), today: str = "", context: str = "") -> str:
def route(text: str, tz_name: str, reports=(), projects=(), today: str = "", context: str = "") -> tuple[str, dict]:
```

Each passes `context` down. In `system_blocks` the second block becomes:

```python
        {"type": "text", "text": _live_half(tz_name, reports, projects, today, context)},
```

In `_live_half`, append the block at the end of the returned string, only when non-empty:

```python
    # CONTEXT last, and only when it has something in it.
    #
    # This is the one question-derived block in the prompt. TODAY above it is
    # built before the utterance is read, which is what makes TODAY safe;
    # this is not, so it is labelled as candidates rather than answers and
    # the rules below tell the model what to do when it does not contain
    # what was asked for.
    if context:
        parts.append(
            "\nCONTEXT — notes and mail that mention words from this "
            "utterance. These are candidates, not answers. If one of them "
            "answers the question, use `answer` and say it directly. If none "
            "of them does, call `query` — do not answer from this block by "
            "guessing, and never say the question cannot be answered just "
            "because it is not here.\n" + context
        )
```

Adapt `parts.append` to however `_live_half` accumulates its string; if it builds one f-string, append the same text to the end of it.

In `route`, pass it through:

```python
        system=system_blocks(tz_name, reports, projects, today, context),
```

- [ ] **Step 4: Run the full router prompt suite**

Run: `uv run pytest tests/test_router_prompt.py -v`
Expected: PASS, including the pre-existing cache tests. **If the byte-stability or cache-read test fails, stop** — `context` has leaked into the static block, and shipping that silently disables prompt caching forever.

- [ ] **Step 5: Commit**

```bash
git add app/router.py tests/test_router_prompt.py
git commit -m "feat: CONTEXT block in the live half of the router prompt"
```

---

### Task 6: Build context in `_say` and prove `query` stays reachable

**Files:**
- Modify: `app/main.py:253` (`_say`)
- Test: `tests/test_context_block.py`

**Interfaces:**
- Consumes: `handlers.context_block()`, `router.route(..., context=...)`.
- Produces: no new symbols; `_say` now passes `context` to `route`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_context_block.py`:

Create a **second** file, `tests/test_context_routing.py` — these need a live
model, and this repo gates that per-file with `pytestmark`, not with a marker:

```python
"""Pre-retrieval, end to end against live Haiku.

Costs a few cents per run. Skips automatically without an API key, like
tests/test_utterances.py, whose fixture shape this follows.
"""

import pytest

from app import config
from app.db import connect

pytestmark = pytest.mark.skipif(
    not config.configured()["anthropic_api_key"],
    reason="needs ANTHROPIC_API_KEY (live router calls)",
)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    import migrate
    from app.main import app

    assert migrate.migrate() == 0, "migrations failed"
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {config.jarvis_token()}"
        yield c


def say(client, text: str) -> dict:
    response = client.post("/say", json={"text": text, "client": "test"})
    assert response.status_code == 200, response.text
    return response.json()


def row_for(utterance_id: int):
    conn = connect()
    try:
        return conn.execute(
            "SELECT model_calls, intent FROM utterances WHERE id = ?", (utterance_id,)
        ).fetchone()
    finally:
        conn.close()


def test_context_answers_in_one_call(client):
    """The whole point: a question whose answer is in a note should cost one
    model call, not two."""
    say(client, "note that the fence posts are rotten on the left side")
    said = say(client, "what did I say about the fence")
    row = row_for(said["utterance_id"])
    assert row["model_calls"] == 1
    assert row["intent"] == "answer"


def test_query_still_reachable(client):
    """THE SAFETY PROPERTY. Do not delete this test.

    CONTEXT is question-derived, which TODAY deliberately is not. What makes
    that safe is that a miss falls through to `query` rather than producing a
    confident wrong answer out of a block that never held the answer."""
    said = say(client, "what did I say about the mortgage refinancing paperwork")
    assert row_for(said["utterance_id"])["intent"] == "query"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_context_routing.py -v`
Expected: FAIL — `model_calls == 2`, because nothing builds the context yet.
(If it *skips*, you have no `ANTHROPIC_API_KEY` set and this task cannot be
verified — get one before continuing, because this is the task whose whole
deliverable is a routing behaviour.)

- [ ] **Step 3: Implement**

In `app/main.py`, inside `_say`'s opening transaction, after `today = handlers.today_block(conn, tz_name)`:

```python
        # The archive's view of this utterance, fetched in the transaction
        # that is already open. ~3ms, and it is what lets a question about a
        # note be answered in this call instead of a second one.
        context = handlers.context_block(conn, req.text)
```

Then pass it to the router:

```python
        tool, args = router.route(
            req.text, tz_name, reports, active_projects, today, context
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_context_block.py tests/test_context_routing.py -v`
Expected: PASS (5 offline + 2 live)

- [ ] **Step 5: Run the full routing suites — this is the regression gate**

Run:
```bash
uv run pytest tests/test_router_prompt.py tests/test_gratitude_routing.py \
              tests/test_projects_routing.py tests/test_reports_voice.py -v
```
Expected: PASS. A misroute here means `CONTEXT` is outcompeting the blocks it sits beside — the same failure `query` already had when a generic note search beat the PROJECT line it was meant to support. If that happens, shorten the block before weakening the instruction.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_context_block.py
git commit -m "perf: answer archive questions in one model call via pre-retrieval"
```

---

### Task 7: Start synthesis before closing the utterance row

**Files:**
- Modify: `app/main.py:354` (`_say`, fast-path tail)

**Interfaces:**
- No signature changes.

- [ ] **Step 1: Move the call**

In `app/main.py`, the fast-path tail currently reads:

```python
    latency = _finish(utterance_id, "fast", tool, reply, started)
    synth.prefetch(reply)
```

Swap them, and extend the comment:

```python
    # Before _finish, not after. The reply has existed since the handler
    # returned, and _finish opens a transaction to close out the utterance
    # row — small, but it is time the synthesis thread could already be
    # running in. Non-blocking and unable to raise, so /say's budget is
    # untouched either way.
    synth.prefetch(reply)
    latency = _finish(utterance_id, "fast", tool, reply, started)
```

Do the same at the two other `synth.prefetch(reply)` sites in `_say` — the `escalate` branch (~line 307) and the `start_project` branch (~line 329).

- [ ] **Step 2: Run the suite**

Run: `uv run pytest tests/test_speech_api.py tests/test_core.py -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/main.py
git commit -m "perf: start synthesis before closing out the utterance row"
```

---

### Task 8: Lower the first chunk's comma floor

**Files:**
- Modify: `speech/segment.py`
- Test: `tests/test_speech_segment.py`

**Interfaces:**
- Produces: `_MIN_FIRST[_SOFT]` changes from 24 to 12.

**Note on what this actually is:** `segments()` already applies `_MIN_FIRST` to the first chunk only — `floor = _TARGET if chunks else _MIN_FIRST[kind]`. So this is tuning one number, not restructuring. Measured first chunk is 739–892ms and it is what gates first sound.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_speech_segment.py`:

```python
def test_short_leading_clause_is_its_own_chunk():
    """First sound is gated by chunk one. A 12-character clause behind a
    comma is worth cutting; 24 was leaving the listener waiting through the
    rest of the sentence."""
    chunks = segment.segments("Got it, I'll remind you at four this afternoon.")
    assert chunks[0] == "Got it,"


def test_very_short_fragment_still_not_cut():
    """Below the floor a fragment sounds clipped, which is the thing the
    floor exists to prevent."""
    chunks = segment.segments("Yes, that is right.")
    assert chunks[0] == "Yes, that is right."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_speech_segment.py::test_short_leading_clause_is_its_own_chunk -v`
Expected: FAIL — chunk 0 is the whole sentence, because "Got it," is 7 characters against a floor of 24.

- [ ] **Step 3: Implement**

In `speech/segment.py`:

```python
# How much text the first chunk has to be worth before a cut of each strength
# is taken. A sentence boundary needs none: "Noted." is a whole utterance and
# sounds like one. "Got it —" at eight characters is still comfortably a
# phrase. Behind a comma, a fragment needs to be long enough to carry itself.
#
# The comma floor was 24 and came down to 12 when the turn was measured end to
# end: first sound is gated by chunk one and nothing else, and the templated
# confirmations this system speaks most often — "Got it, I'll remind you at
# four" — all open with a short clause behind a comma that 24 refused to cut.
_MIN_FIRST = {_SENTENCE: 0, _BREAK: 8, _SOFT: 12}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_speech_segment.py -v`
Expected: PASS

- [ ] **Step 5: Listen to it**

Run: `uv run python -m speech.audition --text "Got it, I'll remind you at four this afternoon."`

This is the one change in Part 1 whose correctness is a question for ears, not for tests. If the seam is audible, put the floor back up. Record whichever way it goes.

- [ ] **Step 6: Commit**

```bash
git add speech/segment.py tests/test_speech_segment.py
git commit -m "perf: cut the first chunk at shorter comma clauses"
```

---

### Task 9: Endpointer down to 0.45s

**Files:**
- Modify: `ios/Jarvis/SetupView.swift:125-150` (`VoiceSettings`)
- Test: `ios/JarvisTests/EndpointerTests.swift`

**Interfaces:**
- Produces: `VoiceSettings.defaultPause == 0.45`, and a new picker entry.

- [ ] **Step 1: Write the failing test**

Append to `ios/JarvisTests/EndpointerTests.swift`, matching the existing fixtures' style:

```swift
@Test func fires_at_the_new_default_pause() {
    var fired = false
    let endpointer = Endpointer(pause: VoiceSettings.defaultPause) { fired = true }
    endpointer.ingest(speech(seconds: 1.0))
    endpointer.ingest(silence(seconds: 0.44))
    #expect(fired == false)
    endpointer.ingest(silence(seconds: 0.02))
    #expect(fired == true)
}

@Test func mid_sentence_breath_does_not_fire_at_045() {
    var fired = false
    let endpointer = Endpointer(pause: 0.45) { fired = true }
    endpointer.ingest(speech(seconds: 1.0))
    endpointer.ingest(silence(seconds: 0.3))   // a breath, not a stop
    endpointer.ingest(speech(seconds: 1.0))
    #expect(fired == false)
}
```

Reuse the existing `speech(seconds:)` and `silence(seconds:)` buffer helpers already in that file. If they are named differently, use the existing names.

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
xcodebuild test -project ios/Jarvis.xcodeproj -scheme Jarvis \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -only-testing:JarvisTests/EndpointerTests
```
Expected: FAIL — `defaultPause` is 0.8, so the 0.44s silence already fires.

- [ ] **Step 3: Implement**

In `ios/Jarvis/SetupView.swift`:

```swift
    /// A second of true silence is already a long gap in speech, and shorter
    /// than this an ordinary mid-sentence breath can send half a reminder.
    ///
    /// It started at 0.8 because measuring the whole path made the cost
    /// legible: this is the single largest fixed block in the round trip —
    /// larger than the model call — and every millisecond of it is spent
    /// waiting on someone who has already stopped talking.
    ///
    /// 0.45 because measuring the whole *turn* made it legible again. The
    /// turn is ~3000ms, of which 800 was this, and `rearm()` means a
    /// premature fire resumes listening rather than truncating the
    /// utterance — so the cost of being wrong here is one extra beat, not a
    /// lost sentence.
    ///
    /// Unlike the rest of the latency work this is a judgement about speech,
    /// not a measurement, so it is the one number here to re-decide by ear.
    /// The picker below changes it without a rebuild.
    static let defaultPause: Double = 0.45

    static let choices: [(label: String, seconds: Double)] = [
        ("Fast 0.45s", 0.45),
        ("Quick 0.8s", 0.8),
        ("Normal 1.2s", 1.2),
```

Keep the remaining entries as they are.

- [ ] **Step 4: Run tests to verify they pass**

Run the same `xcodebuild test` command.
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ios/Jarvis/SetupView.swift ios/JarvisTests/EndpointerTests.swift
git commit -m "perf: endpointer default 0.8s -> 0.45s"
```

---

### Task 10: Measure and report the turn from the phone

**Files:**
- Modify: `ios/Jarvis/TalkView.swift:245-270` (`finish`, `send`), `ios/Jarvis/JarvisAPI.swift`, `ios/Jarvis/Speaker.swift`
- Test: `ios/JarvisTests/ContractTests.swift`

**Interfaces:**
- Consumes: `POST /turns` from Task 2.
- Produces: `JarvisAPI.reportTurn(utteranceId: Int, turnMs: Int) async`, and `Speaker.speak(_:)` returning after the first buffer is scheduled.

- [ ] **Step 1: Write the failing test**

Append to `ios/JarvisTests/ContractTests.swift`:

```swift
@Test func turn_report_encodes_as_the_server_expects() throws {
    let body = TurnReport(utteranceId: 412, turnMs: 1840)
    let data = try JSONEncoder.jarvis.encode(body)
    let json = try #require(
        try JSONSerialization.jsonObject(with: data) as? [String: Any]
    )
    #expect(json["utterance_id"] as? Int == 412)
    #expect(json["turn_ms"] as? Int == 1840)
}
```

Use whatever the project's existing encoder is named; `ContractTests.swift` already establishes it.

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
xcodebuild test -project ios/Jarvis.xcodeproj -scheme Jarvis \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -only-testing:JarvisTests/ContractTests
```
Expected: FAIL — no type `TurnReport`.

- [ ] **Step 3: Add the request type and the call**

In `ios/Jarvis/JarvisAPI.swift`:

```swift
/// What the turn cost, measured where it is actually felt.
///
/// The server's `latency_ms` times /say and cannot see the endpointer before
/// it or the synthesis after it — together about 1550ms of a 3000ms turn.
struct TurnReport: Encodable {
    let utteranceId: Int
    let turnMs: Int
}

extension JarvisAPI {
    /// Fire-and-forget: sent after playback has started, so it cannot sit on
    /// the critical path it is measuring. Failure is ignored — a dropped
    /// measurement is not worth surfacing to someone who has already been
    /// answered.
    func reportTurn(utteranceId: Int, turnMs: Int) async {
        try? await post("/turns", body: TurnReport(utteranceId: utteranceId, turnMs: turnMs))
    }
}
```

Match the existing `post` helper's signature; if requests are built inline elsewhere in this file, follow that shape instead.

- [ ] **Step 4: Start the clock and stop it**

In `ios/Jarvis/TalkView.swift`, add state:

```swift
    /// When the endpointer decided you had stopped talking. Everything after
    /// this instant is latency the user is sitting through.
    @State private var turnStart: ContinuousClock.Instant?
```

Set it where `didEndpoint` latches — in the existing `.onChange(of: transcriber.didEndpoint)` handler, before `finish()` is called:

```swift
            if reached { turnStart = ContinuousClock.now }
```

And in `send(_:)`, after `await speaker.speak(response.reply)` returns:

```swift
            await speaker.speak(response.reply)
            // After the first buffer is scheduled, never before: this is a
            // measurement, and one that delayed the thing it measures would
            // be worse than none.
            if let started = turnStart {
                let elapsed = ContinuousClock.now - started
                await api.reportTurn(
                    utteranceId: response.utteranceId,
                    turnMs: Int(elapsed.components.seconds * 1000
                        + elapsed.components.attoseconds / 1_000_000_000_000_000)
                )
                turnStart = nil
            }
```

This requires `Speaker.speak(_:)` to return once the first buffer is scheduled rather than when playback finishes. Check `ios/Jarvis/Speaker.swift`: if it awaits completion, split it so `speak` returns at first audio and playback continues on its own task. If it already returns early, leave it alone.

- [ ] **Step 5: Add signposts**

In `TalkView.swift` and `Transcriber.swift`, wrap the hops with `OSSignposter` so the whole turn reads as one timeline in Instruments:

```swift
private let signposter = OSSignposter(subsystem: "com.jarvis", category: "turn")
```

Intervals: `endpoint→stop`, `stop→say-sent`, `say-sent→say-returned`, `say-returned→speech-requested`, `speech-requested→first-audio`.

- [ ] **Step 6: Run tests to verify they pass**

Run the `xcodebuild test` command for the full `JarvisTests` scheme.
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add ios/Jarvis/JarvisAPI.swift ios/Jarvis/TalkView.swift \
        ios/Jarvis/Speaker.swift ios/JarvisTests/ContractTests.swift
git commit -m "feat: measure and report the turn from the phone"
```

---

### Task 11: Show the turn on HealthView

**Files:**
- Modify: `ios/Jarvis/HealthView.swift`
- Test: `ios/JarvisTests/ContractTests.swift`

**Interfaces:**
- Consumes: `metrics()["turn"]` from Task 3.

- [ ] **Step 1: Re-capture the metrics fixture**

With the server running:

```bash
curl -s localhost:8000/metrics -H "Authorization: Bearer $JARVIS_TOKEN" \
  > ios/JarvisTests/Fixtures/metrics.json
```

Fixtures here decode **real captured responses** rather than hand-written approximations — a fixture you wrote yourself only proves the decoder matches what you imagined.

- [ ] **Step 2: Write the failing test**

Append to `ios/JarvisTests/ContractTests.swift`:

```swift
@Test func metrics_decodes_the_turn_block() throws {
    let data = try fixture("metrics.json")
    let metrics = try JSONDecoder.jarvis.decode(Metrics.self, from: data)
    #expect(metrics.turn != nil)
}
```

- [ ] **Step 3: Run test to verify it fails**

Run the `xcodebuild test` command for `ContractTests`.
Expected: FAIL — `Metrics` has no `turn`.

- [ ] **Step 4: Implement**

Add `turn` to the `Metrics` model mirroring the existing `fast`/`deep` route stats type, and render it in `HealthView` **above** the existing `/say` pair, labelled so the difference is legible:

```swift
Section("Turn") {
    LabeledContent("p50", value: "\(metrics.turn?.p50 ?? 0) ms")
    LabeledContent("p95", value: "\(metrics.turn?.p95 ?? 0) ms")
    Text("End of speech to first sound. The number the goal is stated in.")
        .font(.caption)
        .foregroundStyle(Theme.text3)
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run the full `JarvisTests` scheme.
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add ios/Jarvis/HealthView.swift ios/JarvisTests/ \
        ios/JarvisTests/Fixtures/metrics.json
git commit -m "feat: HealthView shows turn p50/p95 above /say latency"
```

---

### Task 12: Record what it bought

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Collect the numbers**

After a day of real use:

```bash
curl -s localhost:8000/metrics?days=1 -H "Authorization: Bearer $JARVIS_TOKEN" | python3 -m json.tool
```

- [ ] **Step 2: Write it up**

Update the fast-path latency section of `CLAUDE.md` with: the turn budget table, the before/after `turn_ms` p50 and p95, the `model_calls` distribution before and after pre-retrieval, and the fact that `CONTEXT` is the one question-derived block in the prompt and why that is safe.

State the endpointer number and that it was chosen by ear, so the next person knows it is re-decidable.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record what Part 1 bought"
```

---

## Self-review

Checked against the spec's Part 1:

- Measurement (`turn_ms`, `timings`, `Server-Timing`, signposts) — Tasks 1, 2, 3, 10.
  **Gap accepted:** the `timings` column is created in Task 1 but only
  populated if you also add `perf_counter` calls around the hops in `_say`.
  That is deliberate — `turn_ms` is the number the goal is stated in, and the
  server-side breakdown only earns its keep once the turn is known to be slow
  for a reason `turn_ms` alone cannot explain. Add it then.
- Endpointer 800→450 — Task 9.
- Pre-retrieval / collapsing `query`'s second call — Tasks 4, 5, 6.
- `prefetch` above `_finish` — Task 7.
- First-chunk floor — Task 8. Note the spec described this as restructuring;
  the code already applies floors to the first chunk only, so it is a
  one-number change.
- Testing section of the spec — all four named tests appear:
  `test_context_block` (Task 4), `test_context_is_not_today` (Task 5, as
  `test_static_half_is_byte_identical_across_contexts`),
  `test_query_still_reachable` (Task 6),
  `test_turn_report_ignores_unknown_id` (Task 2, as
  `test_unknown_id_is_not_an_error`).

**Three corrections made against the real code, after the spec was written:**

1. `handlers.search_email` already exists and is public. Task 4 reuses it
   rather than adding `_search_email`.
2. `segment.py` already applies its floors to the first chunk alone, so Task 8
   is a one-number change, not the restructuring the spec implied.
3. There is no shared `client`/`auth` fixture and no `live` marker in this
   repo. Every test block above uses the per-file fixture and `pytestmark`
   shape that `tests/test_utterances.py` establishes.

## Next

**Go to [`2026-08-04-part3-messages-calls.md`](2026-08-04-part3-messages-calls.md).**

Not Part 2. Part 2's ship/no-ship gate is stated in `turn_ms`, and running it
the afternoon Part 1 lands measures a cold MLX prompt cache against a warm API
connection and concludes the wrong thing. Let a few days of real turns
accumulate first.
