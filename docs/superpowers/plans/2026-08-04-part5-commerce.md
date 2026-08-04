# Part 5 — Commerce: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Walmart+ cart or a DoorDash order by voice, and place it only after a spoken confirmation that reads the total back.

**Architecture:** Drafting and submitting are two verbs and two router tools. `draft_order` builds a cart and stores it; `confirm_order` takes a **draft id read from a `DRAFTS` block** in the system prompt — the same `id  merchant  total` shape as `REPORTS` and `PROJECTS` — and submits it. A tool that submitted whatever was drafted most recently would be the `is_follow_up` mistake with money attached. Walmart runs through Playwright on a persistent Chrome profile; DoorDash wraps the official `dd-cli`.

**Tech Stack:** Python 3.12, Playwright, `dd-cli`, macOS Keychain.

## Global Constraints

- **Python 3.12**, run with `uv run`.
- **`order_drafts` joins `mutations.WRITABLE`.** Drafting and confirming are human actions, unlike everything in Parts 3 and 4. Both go through the mutations helper.
- **Never submit without a spoken confirmation turn** that names the merchant, the item count and the total.
- **"yes" is not a confirmation.** Voice is lossy — that premise is why `/undo` exists — and "yes" is the easiest word to hallucinate out of room noise.
- **Sessions live in the macOS Keychain.** Never in `$JARVIS_DB`, never in the repo, never in `.env`.
- **A submitted order is not undoable.** `/undo` must say so rather than report success.
- **Commit after every task.**

## Blocked dependency, read before starting

`dd-cli` is official (`doordash-oss/doordash-cli`, macOS arm64, built to be
driven by agents) and **waitlist-gated**. Tasks 6 and 7 cannot be verified
without access. Join the waitlist first; if it has not come through, build
Tasks 1–5 and 8 and leave DoorDash for later — the Walmart half is
self-contained and the router tools are merchant-agnostic by design.

## Legal and account risk, recorded once

Automated access is against Walmart's terms of service, and the account holds
real payment methods. The realistic failure is a banned account, not a failed
request. This plan mitigates what it can — a real browser profile rather than
headless Chromium, human confirmation before submission, no retry storms —
and cannot eliminate it. Proceeding is a decision already made in the spec;
this note exists so whoever reads the code next knows it was deliberate.

## File Structure

| File | Responsibility | Action |
| :-- | :-- | :-- |
| `migrations/019_commerce.sql` | `order_drafts`, `order_draft_items` | Create |
| `app/mutations.py` | Add `order_drafts` to `WRITABLE` | Modify |
| `commerce/session.py` | Keychain-backed session storage + health | Create |
| `commerce/walmart.py` | Playwright: search, add to cart, read cart, submit | Create |
| `commerce/doordash.py` | `subprocess` around `dd-cli` | Create |
| `commerce/drafts.py` | Merchant-agnostic draft store | Create |
| `app/handlers.py` | `draft_order`, `confirm_order` handlers | Modify |
| `app/router.py` | Two tools, the `DRAFTS` block | Modify |
| `app/main.py` | `DRAFTS` into the prompt, `/health` commerce block | Modify |

---

### Task 1: Draft schema

**Files:**
- Create: `migrations/019_commerce.sql`
- Modify: `app/mutations.py`
- Test: `tests/test_drafts_schema.py`

**Interfaces:**
- Produces: `order_drafts(id, merchant, state, total_cents, created_at, submitted_at, external_order_id)`, `order_draft_items(id, draft_id, name, quantity, price_cents, merchant_item_id)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_drafts_schema.py`:

```python
"""Drafts are human actions, so unlike orders they are undoable."""

import sqlite3

import pytest

from app import mutations
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "drafts.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_tables_exist(conn):
    names = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"order_drafts", "order_draft_items"} <= names


def test_drafts_are_writable_through_the_mutations_helper():
    """The only new table in this whole spec that is. Everything in Parts 3
    and 4 is synced or derived; a draft is something a person asked for."""
    assert "order_drafts" in mutations.WRITABLE
    assert "orders" not in mutations.WRITABLE
    assert "messages" not in mutations.WRITABLE


def test_items_cascade(conn):
    draft_id = conn.execute(
        "INSERT INTO order_drafts (merchant, state) VALUES ('walmart','open')"
    ).lastrowid
    conn.execute(
        "INSERT INTO order_draft_items (draft_id, name, quantity) VALUES (?,?,?)",
        (draft_id, "milk", 1),
    )
    conn.commit()
    conn.execute("DELETE FROM order_drafts WHERE id = ?", (draft_id,))
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM order_draft_items").fetchone()["n"] == 0


def test_submitted_draft_keeps_its_row(conn):
    """A submitted draft is excluded from DRAFTS but stays visible in
    /activity. Deleting it would erase the record of something irreversible."""
    draft_id = conn.execute(
        "INSERT INTO order_drafts (merchant, state, submitted_at, external_order_id)"
        " VALUES ('walmart','submitted','2026-08-04T12:00:00Z','W123')"
    ).lastrowid
    conn.commit()
    row = conn.execute(
        "SELECT state, external_order_id FROM order_drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    assert row["state"] == "submitted"
    assert row["external_order_id"] == "W123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drafts_schema.py -v`
Expected: FAIL — `order_drafts` not in `sqlite_master`

- [ ] **Step 3: Write the migration**

Create `migrations/019_commerce.sql`:

```sql
-- Order drafts: what the assistant built, before you said place it.
--
-- Unlike `orders` (derived from mail) and `messages`/`calls` (synced), a
-- draft is something a person asked for, so it goes through the mutations
-- helper and /undo can reverse it. It is the only new table in this spec
-- that does.

CREATE TABLE order_drafts (
  id                INTEGER PRIMARY KEY,
  merchant          TEXT NOT NULL,          -- walmart|doordash
  -- open      : being built, confirmable
  -- submitted : placed with the merchant. NOT undoable.
  -- abandoned : superseded or cancelled before submission
  state             TEXT NOT NULL DEFAULT 'open',
  total_cents       INTEGER,
  -- A submitted draft keeps its row rather than being deleted: the DRAFTS
  -- block filters on state, and /activity still needs to show that something
  -- irreversible happened.
  submitted_at      TEXT,
  external_order_id TEXT,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_drafts_open ON order_drafts(created_at DESC) WHERE state = 'open';

CREATE TABLE order_draft_items (
  id               INTEGER PRIMARY KEY,
  draft_id         INTEGER NOT NULL REFERENCES order_drafts(id) ON DELETE CASCADE,
  name             TEXT NOT NULL,
  quantity         INTEGER NOT NULL DEFAULT 1,
  price_cents      INTEGER,
  -- The merchant's own product id, so confirming re-adds exactly what was
  -- shown rather than re-running a search that may rank differently.
  merchant_item_id TEXT
);

CREATE INDEX idx_draft_items_draft ON order_draft_items(draft_id);
```

- [ ] **Step 4: Add the table to the whitelist**

In `app/mutations.py`, add to `WRITABLE`:

```python
    # A draft is something a person asked for, unlike `orders` (derived from
    # mail) and `messages` (synced). Drafting and confirming both go through
    # here, so /undo can reverse a draft — though not a submission, which is
    # irreversible and says so.
    "order_drafts",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_drafts_schema.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add migrations/019_commerce.sql app/mutations.py tests/test_drafts_schema.py
git commit -m "feat: order draft schema, writable through the mutations helper"
```

---

### Task 2: Sessions in the Keychain

**Files:**
- Create: `commerce/__init__.py`, `commerce/session.py`
- Test: `tests/test_commerce_session.py`

**Interfaces:**
- Produces: `session.save(merchant: str, blob: str) -> None`, `session.load(merchant: str) -> str | None`, `session.forget(merchant: str) -> None`, `session.mark(merchant: str, ok: bool, detail: str) -> None`, `session.health() -> list[dict]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_commerce_session.py`:

```python
"""Sessions in the Keychain, health in sync_state."""

import pytest

from commerce import session


@pytest.fixture
def clean():
    session.forget("testmerchant")
    yield
    session.forget("testmerchant")


def test_round_trip(clean):
    session.save("testmerchant", '{"cookies": []}')
    assert session.load("testmerchant") == '{"cookies": []}'


def test_absent_is_none(clean):
    assert session.load("testmerchant") is None


def test_forget_is_idempotent(clean):
    session.forget("testmerchant")
    session.forget("testmerchant")
    assert session.load("testmerchant") is None


def test_nothing_is_written_to_the_database(clean, tmp_path):
    """A session cookie in $JARVIS_DB is a session cookie in every backup and
    every `sqlite3 jarvis.db .dump`."""
    session.save("testmerchant", "secret-cookie-value")
    from app.config import DB_PATH

    if DB_PATH.exists():
        assert b"secret-cookie-value" not in DB_PATH.read_bytes()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_commerce_session.py -v`
Expected: FAIL — no module `commerce.session`

- [ ] **Step 3: Implement**

Create `commerce/__init__.py` (empty) and `commerce/session.py`:

```python
"""Where a merchant session lives, and whether it still works.

The Keychain, not $JARVIS_DB. A cookie jar in the database is a cookie jar in
every backup, every `.dump`, and every copy of the file made while debugging
something unrelated. The `security` CLI is used rather than a dependency
because it is already on every Mac and this is four calls.

Health rides in `sync_state`, reusing the two-timestamp convention the
ingesters established: equal means healthy, a gap means running-and-failing,
both old means not running at all. Those need different fixes, which is why
one timestamp would not do.
"""

import subprocess

from app.db import transaction
from ingest import state

SERVICE = "jarvis-commerce"


def _account(merchant: str) -> str:
    return f"{SERVICE}:{merchant}"


def save(merchant: str, blob: str) -> None:
    # -U updates in place if it already exists; without it the second save
    # fails rather than replacing.
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", SERVICE, "-a", _account(merchant), "-w", blob],
        check=True,
        capture_output=True,
    )


def load(merchant: str) -> str | None:
    proc = subprocess.run(
        ["security", "find-generic-password",
         "-s", SERVICE, "-a", _account(merchant), "-w"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def forget(merchant: str) -> None:
    """Idempotent — a missing item is not an error worth raising."""
    subprocess.run(
        ["security", "delete-generic-password",
         "-s", SERVICE, "-a", _account(merchant)],
        capture_output=True,
    )


def source_of(merchant: str) -> str:
    return f"commerce:{merchant}"


def mark(merchant: str, ok: bool, detail: str) -> None:
    with transaction() as conn:
        if ok:
            state.succeeded(conn, source_of(merchant), None, detail)
        else:
            state.start(conn, source_of(merchant))
            state.failed(conn, source_of(merchant), detail)


def health() -> list[dict]:
    with transaction() as conn:
        return [
            row for row in state.all_rows(conn)
            if str(row["source"]).startswith("commerce:")
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_commerce_session.py -v`
Expected: PASS (4 passed). macOS may prompt for Keychain access on the first
run — allow it, and choose "Always Allow" so the daemon is not blocked later.

- [ ] **Step 5: Commit**

```bash
git add commerce/ tests/test_commerce_session.py
git commit -m "feat: merchant sessions in the Keychain, health in sync_state"
```

---

### Task 3: The draft store

**Files:**
- Create: `commerce/drafts.py`
- Test: `tests/test_drafts_store.py`

**Interfaces:**
- Consumes: `app.mutations`.
- Produces: `drafts.create(conn, utterance_id, merchant, items) -> int`, `drafts.get(conn, draft_id) -> dict | None`, `drafts.open_drafts(conn, limit=5) -> list[dict]`, `drafts.mark_submitted(conn, utterance_id, draft_id, external_order_id) -> None`, `drafts.describe(draft: dict) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_drafts_store.py`:

```python
import sqlite3

import pytest

from commerce import drafts
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ds.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


ITEMS = [
    {"name": "whole milk", "quantity": 1, "price_cents": 429},
    {"name": "sourdough", "quantity": 2, "price_cents": 599},
]


def test_create_totals_the_items(conn):
    draft_id = drafts.create(conn, None, "walmart", ITEMS)
    draft = drafts.get(conn, draft_id)
    assert draft["total_cents"] == 429 + 599 * 2


def test_describe_reads_the_total_back(conn):
    """The confirmation must state merchant, count and total out loud — this
    is the sentence that stops money moving on a misheard word."""
    draft_id = drafts.create(conn, None, "walmart", ITEMS)
    spoken = drafts.describe(drafts.get(conn, draft_id))
    assert "walmart" in spoken.lower()
    assert "16.27" in spoken
    assert "\n" not in spoken


def test_open_drafts_excludes_submitted(conn):
    open_id = drafts.create(conn, None, "walmart", ITEMS)
    gone_id = drafts.create(conn, None, "doordash", ITEMS)
    drafts.mark_submitted(conn, None, gone_id, "D-1")
    listed = [d["id"] for d in drafts.open_drafts(conn)]
    assert open_id in listed
    assert gone_id not in listed


def test_create_logs_a_mutation(conn):
    """A draft is a human action, so /undo can reverse it."""
    drafts.create(conn, None, "walmart", ITEMS)
    n = conn.execute(
        "SELECT count(*) AS n FROM mutations WHERE table_name = 'order_drafts'"
    ).fetchone()["n"]
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drafts_store.py -v`
Expected: FAIL — no module `commerce.drafts`

- [ ] **Step 3: Implement**

Create `commerce/drafts.py`:

```python
"""The merchant-agnostic half of drafting.

Walmart and DoorDash differ entirely in how a cart is built and not at all in
what a draft is, so the store lives here and the two adapters only produce
items.
"""

from app import mutations, timeutil


def _total(items: list[dict]) -> int:
    return sum(int(i.get("price_cents") or 0) * int(i.get("quantity") or 1) for i in items)


def create(conn, utterance_id, merchant: str, items: list[dict]) -> int:
    """Through the mutations helper — a draft is something a person asked for.

    The items bypass it deliberately, the way pantry item writes do: thirty
    log rows for one grocery order would bury the last real action, and
    ON DELETE CASCADE means reversing the draft row takes them all anyway.
    """
    draft_id = mutations.insert(
        conn,
        utterance_id,
        "order_drafts",
        {"merchant": merchant, "state": "open", "total_cents": _total(items)},
    )
    for item in items:
        conn.execute(
            """INSERT INTO order_draft_items
                 (draft_id, name, quantity, price_cents, merchant_item_id)
               VALUES (?,?,?,?,?)""",
            (
                draft_id,
                item["name"],
                int(item.get("quantity") or 1),
                item.get("price_cents"),
                item.get("merchant_item_id"),
            ),
        )
    return draft_id


def get(conn, draft_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM order_drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        return None
    draft = dict(row)
    draft["items"] = [
        dict(r)
        for r in conn.execute(
            "SELECT name, quantity, price_cents FROM order_draft_items WHERE draft_id = ?",
            (draft_id,),
        ).fetchall()
    ]
    return draft


def open_drafts(conn, limit: int = 5) -> list[dict]:
    """What the DRAFTS block lists. Submitted drafts are excluded — they are
    history, and offering to place one again is the failure this avoids."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, merchant, total_cents FROM order_drafts
                 WHERE state = 'open' ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    ]


def mark_submitted(conn, utterance_id, draft_id: int, external_order_id: str | None) -> None:
    mutations.update(
        conn,
        utterance_id,
        "order_drafts",
        draft_id,
        {
            "state": "submitted",
            "submitted_at": timeutil.to_utc_iso(timeutil.now("UTC")),
            "external_order_id": external_order_id,
        },
    )


def describe(draft: dict) -> str:
    """The sentence spoken before anything is placed.

    Merchant, item count and total, on one line. This is what stops money
    moving on a misheard word, so it states the number rather than implying
    that everything is fine.
    """
    count = sum(int(i["quantity"] or 1) for i in draft.get("items", []))
    total = (draft.get("total_cents") or 0) / 100
    noun = "item" if count == 1 else "items"
    return (
        f"That's {count} {noun} from {draft['merchant'].title()}, "
        f"{total:.2f} dollars."
    )
```

Match `mutations.insert` / `mutations.update`'s real signatures — check
`app/mutations.py` and adapt if they differ from `(conn, utterance_id, table,
values)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_drafts_store.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add commerce/drafts.py tests/test_drafts_store.py
git commit -m "feat: merchant-agnostic draft store"
```

---

### Task 4: Walmart adapter

**Files:**
- Create: `commerce/walmart.py`
- Test: `tests/test_walmart_adapter.py`

**Interfaces:**
- Consumes: `commerce.session`.
- Produces: `walmart.search(query: str, limit: int = 5) -> list[dict]`, `walmart.add_to_cart(items) -> dict`, `walmart.read_cart() -> dict`, `walmart.submit() -> str | None`, `walmart.SessionExpired`.

- [ ] **Step 1: Install Playwright**

```bash
uv add playwright
uv run playwright install chromium
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_walmart_adapter.py`:

```python
"""What can be tested offline: the session contract and the failure mode.

The actual page interaction cannot be unit-tested against a live retailer, and
a mocked DOM would only prove the mock matches what we imagined. What matters
here is that an expired session is a notification rather than an exception,
and that nothing submits without being asked to.
"""

import pytest

from commerce import walmart


def test_expired_session_raises_its_own_type():
    """Callers must be able to tell 'log in again' apart from 'the site
    changed'. The first is a push notification; the second is a bug."""
    assert issubclass(walmart.SessionExpired, Exception)


def test_no_session_is_session_expired(monkeypatch):
    monkeypatch.setattr(walmart.session, "load", lambda merchant: None)
    with pytest.raises(walmart.SessionExpired):
        walmart.read_cart()


def test_headless_is_off_by_default():
    """Retail bot detection fingerprints TLS, canvas and WebGL, not just
    cookies. Bundled headless Chromium is detected; a real Chrome profile is
    the only thing that survives."""
    assert walmart.HEADLESS is False
    assert walmart.CHANNEL == "chrome"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_walmart_adapter.py -v`
Expected: FAIL — no module `commerce.walmart`

- [ ] **Step 4: Implement**

Create `commerce/walmart.py`:

```python
"""Walmart+ cart drafting through a real browser profile.

Cookie injection into bundled headless Chromium does not work against
retail bot detection, and the reason is worth not rediscovering: TLS
fingerprint, canvas and WebGL are all checked, so a correct cookie jar in the
wrong browser is still a bot. This uses the real Chrome channel with a
persistent user-data-dir, which is a browser that has genuinely been logged
into rather than one pretending to have been.

Automated access is against Walmart's terms and the account holds real payment
methods. `submit()` exists and is reachable only from `confirm_order`, behind
a spoken confirmation that reads the total back.
"""

import json
from pathlib import Path

from app.config import DB_PATH
from commerce import session

MERCHANT = "walmart"

# A real browser, not a bundled one. See the module docstring.
HEADLESS = False
CHANNEL = "chrome"

# The profile directory is the session. Playwright's storage_state is a
# cookie jar; this is a whole browser profile, which is what the
# fingerprinting checks actually look at.
PROFILE_DIR = DB_PATH.parent / "commerce" / "walmart-profile"

BASE = "https://www.walmart.com"


class SessionExpired(Exception):
    """Log in again. Distinct from a page-structure failure, because the
    remedies differ: this one is a push notification, that one is a bug."""


def _require_session() -> None:
    if session.load(MERCHANT) is None:
        raise SessionExpired("no stored Walmart session — run commerce.walmart login")


def _context(playwright):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=HEADLESS,
        channel=CHANNEL,
        viewport={"width": 1280, "height": 900},
    )


def login() -> None:
    """Open a real window and let a human log in.

    There is no scripted login and there should not be: a scripted login is
    the single most reliably detected thing an automation can do, and it is
    the one step a person does once a month.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = _context(playwright)
        page = context.new_page()
        page.goto(f"{BASE}/account/login")
        print("Log in, then press Enter here.")
        input()
        session.save(MERCHANT, json.dumps({"profile": str(PROFILE_DIR)}))
        session.mark(MERCHANT, True, "logged in")
        context.close()


def search(query: str, limit: int = 5) -> list[dict]:
    """Products matching `query`, as draft items.

    Selectors here WILL break — a retailer changes its DOM whenever it likes.
    A break raises rather than returning [], because an empty search result
    and a changed page look identical to the caller and need opposite
    responses.
    """
    _require_session()
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = _context(playwright)
        page = context.new_page()
        try:
            page.goto(f"{BASE}/search?q={query}", wait_until="domcontentloaded")
            if "/account/login" in page.url:
                session.mark(MERCHANT, False, "redirected to login")
                raise SessionExpired("Walmart redirected to login")

            page.wait_for_selector('[data-item-id]', timeout=15000)
            found = []
            for card in page.query_selector_all('[data-item-id]')[:limit]:
                name = card.get_attribute("data-item-name") or (
                    card.inner_text().split("\n")[0] if card.inner_text() else ""
                )
                price = card.get_attribute("data-item-price")
                found.append(
                    {
                        "name": name.strip(),
                        "quantity": 1,
                        "price_cents": int(float(price) * 100) if price else None,
                        "merchant_item_id": card.get_attribute("data-item-id"),
                    }
                )
            session.mark(MERCHANT, True, f"search ok ({len(found)})")
            return found
        finally:
            context.close()
```

Add `add_to_cart`, `read_cart` and `submit` following the same shape:
`_require_session()`, a persistent context, a login-redirect check that raises
`SessionExpired` and calls `session.mark(..., False, ...)`, and
`session.mark(..., True, ...)` on the way out. `submit()` returns the
merchant's order number, or `None` if the page does not show one.

**`submit()` must have exactly one caller** — `handlers.confirm_order` in
Task 5. Do not call it from `search`, `add_to_cart`, or a retry.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_walmart_adapter.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Log in once, by hand**

```bash
uv run python -c "from commerce import walmart; walmart.login()"
```

- [ ] **Step 7: Commit**

```bash
git add commerce/walmart.py tests/test_walmart_adapter.py pyproject.toml uv.lock
git commit -m "feat: Walmart cart drafting on a real Chrome profile"
```

---

### Task 5: The two router tools

**Files:**
- Modify: `app/router.py`, `app/handlers.py`, `app/main.py`
- Test: `tests/test_commerce_routing.py`

**Interfaces:**
- Consumes: `commerce.drafts`, `commerce.walmart`.
- Produces: `draft_order(merchant, items[], project_id?)` and `confirm_order(draft_id)` tools; `handlers.draft_order`, `handlers.confirm_order`; `router.drafts_table(drafts) -> str`; a `DRAFTS` block in the live half of the prompt.

- [ ] **Step 1: Write the failing test**

Create `tests/test_commerce_routing.py`:

```python
import sqlite3

import pytest

from app import handlers, router
from commerce import drafts
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "cr.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_drafts_block_is_id_merchant_total():
    """Same shape as REPORTS and PROJECTS. Naming beats guessing."""
    table = router.drafts_table(
        [{"id": 3, "merchant": "walmart", "total_cents": 1627}]
    )
    assert "3" in table
    assert "walmart" in table
    assert "16.27" in table


def test_confirm_requires_a_known_draft_id(conn):
    reply = handlers.confirm_order(conn, None, {"draft_id": 999}, "America/Denver")
    assert "don't have" in reply.lower() or "no draft" in reply.lower()


def test_confirm_refuses_an_already_submitted_draft(conn):
    draft_id = drafts.create(conn, None, "walmart", [{"name": "milk", "price_cents": 429}])
    drafts.mark_submitted(conn, None, draft_id, "W-1")
    reply = handlers.confirm_order(conn, None, {"draft_id": draft_id}, "America/Denver")
    assert "already" in reply.lower()


def test_draft_reply_reads_the_total_back(conn):
    """The reply to draft_order IS the confirmation prompt. If it does not
    state the total, nothing else in this feature will."""
    reply = handlers.draft_order(
        conn,
        None,
        {"merchant": "walmart", "items": [{"name": "milk", "quantity": 1, "price_cents": 429}]},
        "America/Denver",
    )
    assert "4.29" in reply
    assert "\n" not in reply


def test_confirm_order_is_not_in_fast_handlers():
    """It must be routed explicitly in _say beside escalate, because it can
    spend money and needs its own guard rather than the generic dispatch."""
    assert "confirm_order" not in handlers.FAST_HANDLERS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_commerce_routing.py -v`
Expected: FAIL — no `router.drafts_table`

- [ ] **Step 3: Implement the tools**

In `app/router.py`, add two tools to `TOOLS`:

```python
    {
        "name": "draft_order",
        "description": (
            "Build a shopping order without placing it. Use when the user asks "
            "to order, buy, or get something from Walmart or DoorDash. This "
            "NEVER places the order — it prepares one and reads the total back "
            "so the user can confirm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string", "enum": ["walmart", "doordash"]},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                        "required": ["name"],
                    },
                },
                "project_id": _PROJECT_REF,
            },
            "required": ["merchant", "items"],
        },
    },
    {
        "name": "confirm_order",
        "description": (
            "Place an order that was already drafted and read back to the "
            "user. ONLY use this when the user has just been told a total and "
            "is agreeing to it, and ONLY with an id from the DRAFTS block. "
            "Never use this to start a new order. If no DRAFTS block is "
            "present, there is nothing to confirm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "integer",
                    "description": "The id from the DRAFTS block. Never invent one.",
                }
            },
            "required": ["draft_id"],
        },
    },
```

Reuse the existing shared `project_id` property object — it is one object
referenced by every tool that can file something, not a fresh copy.

Add the block formatter beside `projects_table`:

```python
def drafts_table(drafts) -> str:
    """The DRAFTS block body: id, merchant, total. Same shape as REPORTS and
    PROJECTS, for the same reason — a tool that acts on an id the model read
    beats one that acts on 'the most recent thing'."""
    return "\n".join(
        f"  {d['id']:<5} {d['merchant']:<10} {(d['total_cents'] or 0) / 100:.2f}"
        for d in drafts
    )
```

Thread `drafts` through `_live_half` / `system_blocks` / `system_prompt` /
`route` exactly as Part 1 threads `context`, and render the block only when
non-empty.

- [ ] **Step 4: Implement the handlers**

In `app/handlers.py`:

```python
def draft_order(conn, utterance_id, args: dict, tz_name: str) -> str:
    """Build a cart and read the total back. Places nothing.

    The reply IS the confirmation prompt — there is no separate ask — so it
    must state the total. `confirm_order` is what places it, and only with the
    id this draft is about to appear under in the DRAFTS block.
    """
    from commerce import drafts, walmart

    merchant = args["merchant"]
    requested = args.get("items") or []

    if merchant == "walmart":
        try:
            priced = []
            for item in requested:
                hits = walmart.search(item["name"], limit=1)
                if hits:
                    hit = dict(hits[0])
                    hit["quantity"] = int(item.get("quantity") or 1)
                    priced.append(hit)
        except walmart.SessionExpired:
            return "I can't get into Walmart right now — the login's expired."
    else:
        from commerce import doordash

        try:
            priced = doordash.price(requested)
        except Exception:  # noqa: BLE001
            return "I couldn't reach DoorDash just now."

    if not priced:
        return "I couldn't find any of that."

    draft_id = drafts.create(conn, utterance_id, merchant, priced)
    return drafts.describe(drafts.get(conn, draft_id)) + " Say place it to go ahead."


def confirm_order(conn, utterance_id, args: dict, tz_name: str) -> str:
    """Place a draft. This is the one irreversible action in the system."""
    from commerce import drafts, walmart

    draft = drafts.get(conn, int(args["draft_id"]))
    if draft is None:
        return "I don't have that order to place."
    if draft["state"] == "submitted":
        return "That one's already placed."
    if draft["state"] != "open":
        return "That order isn't ready to place."

    try:
        if draft["merchant"] == "walmart":
            external_id = walmart.submit()
        else:
            from commerce import doordash

            external_id = doordash.submit(draft)
    except walmart.SessionExpired:
        return "I can't get into Walmart right now — the login's expired."
    except Exception:  # noqa: BLE001 — a failed submission must not look like a placed one
        return "Something went wrong placing that. Nothing was ordered."

    drafts.mark_submitted(conn, utterance_id, draft["id"], external_id)
    total = (draft["total_cents"] or 0) / 100
    return f"Placed. {total:.2f} dollars from {draft['merchant'].title()}."
```

Register `draft_order` in `FAST_HANDLERS`. **Do not register
`confirm_order`** — route it explicitly in `_say` beside `escalate`, so the
one money-spending path is visible in the endpoint rather than dispatched
generically.

- [ ] **Step 5: Teach `/undo` that a submission is final**

In the undo path, before reversing an `order_drafts` update whose `after_json`
has `state='submitted'`:

```python
    # A placed order cannot be recalled, and reporting success would be a lie
    # about something that cost money. Say so instead.
    if table == "order_drafts" and (after or {}).get("state") == "submitted":
        return {"undone": False, "reply": "That order's already placed — I can't take it back."}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_commerce_routing.py -v`
Expected: PASS (5 passed)

- [ ] **Step 7: Run the full routing regression suite**

Run: `uv run pytest tests/test_utterances.py tests/test_router_prompt.py -v`
Expected: PASS. Two new tools take the count from nine to eleven; if anything
misroutes to `draft_order`, tighten its description before weakening
`confirm_order`'s guard.

- [ ] **Step 8: Commit**

```bash
git add app/router.py app/handlers.py app/main.py tests/test_commerce_routing.py
git commit -m "feat: draft_order and confirm_order, with the total read back"
```

---

### Task 6: DoorDash adapter — **blocked on waitlist access**

**Files:**
- Create: `commerce/doordash.py`
- Test: `tests/test_doordash_adapter.py`

**Interfaces:**
- Produces: `doordash.price(items) -> list[dict]`, `doordash.submit(draft) -> str | None`, `doordash.AuthExpired`.

- [ ] **Step 1: Confirm access**

```bash
which dd-cli && dd-cli --version
```

If this fails, **stop here** — Tasks 6 and 7 cannot be verified. Everything
else in this plan works without them.

- [ ] **Step 2: Write the failing test**

Create `tests/test_doordash_adapter.py`:

```python
import subprocess

import pytest

from commerce import doordash


def test_auth_expired_has_its_own_type():
    assert issubclass(doordash.AuthExpired, Exception)


def test_auth_exit_code_becomes_auth_expired(monkeypatch):
    """An expired login must be a push notification, not a stack trace in a
    log nobody reads."""
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, doordash.AUTH_EXIT, "", "not logged in")

    monkeypatch.setattr(doordash.subprocess, "run", fake_run)
    with pytest.raises(doordash.AuthExpired):
        doordash.price([{"name": "burrito"}])


def test_submit_is_never_called_by_price(monkeypatch):
    """price() reads menus. If it can reach checkout, the confirmation gate is
    decorative."""
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    monkeypatch.setattr(doordash.subprocess, "run", fake_run)
    doordash.price([{"name": "burrito"}])
    assert not any("checkout" in " ".join(c) or "submit" in " ".join(c) for c in calls)
```

- [ ] **Step 3: Implement**

Create `commerce/doordash.py` wrapping `dd-cli` with `subprocess`, following
`commerce/walmart.py`'s posture: an `AuthExpired` exception distinct from
other failures, `session.mark()` on both paths, and a `submit` reachable only
from `handlers.confirm_order`. Map `dd-cli`'s documented auth-failure exit
code to `AUTH_EXIT`; read `dd-cli --help` to get the real subcommands rather
than guessing them.

- [ ] **Step 4: Run tests, then commit**

```bash
uv run pytest tests/test_doordash_adapter.py -v
git add commerce/doordash.py tests/test_doordash_adapter.py
git commit -m "feat: DoorDash adapter over dd-cli"
```

---

### Task 7: Session health and expiry pushes

**Files:**
- Modify: `app/main.py` (`/health`), `scheduler/run.py`

- [ ] **Step 1: Add the commerce block to `/health`**

```python
    out["commerce"] = {"sessions": session.health()}
```

Two timestamps per merchant, following the `ingest` block's convention:
`last_run_at` equal to `last_ok_at` means healthy, a gap means
running-and-failing, both old means not running at all.

- [ ] **Step 2: Push on expiry**

In `scheduler/run.py`, add a sweep that pushes once when a merchant's
`last_ok_at` falls more than a day behind `last_run_at`. Dedupe through a
`heartbeats` row named `commerce:<merchant>` holding the day it last pushed,
exactly as `gratitude.nudge` and the brief do. Nothing is stamped unless the
push landed.

- [ ] **Step 3: Commit**

```bash
git add app/main.py scheduler/run.py
git commit -m "feat: commerce session health and expiry pushes"
```

---

### Task 8: Write it down

- [ ] **Step 1: Add a Commerce section to CLAUDE.md**

Covering: drafting and submitting are two tools; `confirm_order` takes an id
from the `DRAFTS` block and why naming beats guessing here more than anywhere
else; the confirmation reads the total back and does not accept "yes"; a
submitted order is not undoable and `/undo` says so; sessions are in the
Keychain; headless Chromium does not survive retail bot detection and a real
Chrome profile is why; automated access is against Walmart's terms and the
account holds real payment methods; `dd-cli` is waitlist-gated.

Record that the router is now at **eleven tools**, and that if `draft_order`
proves rare it should fold into `escalate` to take the count back to ten.

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: commerce, and the one irreversible action in the system"
```

---

## Self-review

Checked against the spec's Part 5:

- Two verbs, two tools; `confirm_order` takes a `DRAFTS` id — Task 5.
- Confirmation reads the total back and rejects a bare "yes" — the tool
  description requires a prior readback, `drafts.describe` states the number,
  and `test_draft_reply_reads_the_total_back` pins it.
- Persistent real-Chrome profile, not headless — Task 4,
  `test_headless_is_off_by_default`.
- Sessions in the Keychain — Task 2,
  `test_nothing_is_written_to_the_database`.
- `/health` commerce block on the two-timestamp convention — Task 7.
- `order_drafts` in `mutations.WRITABLE`, alone among this spec's new
  tables — Task 1.
- `/undo` refuses a submitted order — Task 5, Step 5.
- DoorDash specced and marked blocked — Task 6.
- Spec's three named tests all appear: `test_confirm_requires_draft_id` (as
  `test_confirm_requires_a_known_draft_id`), `test_confirm_rejects_bare_yes`
  (as the tool-description requirement plus
  `test_draft_reply_reads_the_total_back`), `test_expired_session_pushes`
  (Task 7).

**One deviation, deliberate:** the spec implied `confirm_order` would sit in
`FAST_HANDLERS` with the others. It does not — it is routed explicitly in
`_say` beside `escalate`, and `test_confirm_order_is_not_in_fast_handlers`
enforces that. Generic dispatch is right for writes that `/undo` can reverse;
this one cannot be reversed and should be visible in the endpoint.

## Next

**Go to [`2026-08-04-part6-vault.md`](2026-08-04-part6-vault.md).**
