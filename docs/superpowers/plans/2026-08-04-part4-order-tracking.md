# Part 4 — Order tracking: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer "when's my Amazon package arriving?" and "did the Walmart order go through?" from order emails.

**Architecture:** `ingest/gmail.py`'s existing pass keeps `format=metadata`, which is what makes "there is no path to storing bodies by accident" true. A **second, narrow pass** fetches `format=full` for an explicit sender allowlist only, stores the body in `email_bodies`, and runs one Haiku extraction into `orders`. This bends a stated invariant; the plan's job is to bend it in exactly one place, name it, and rewrite CLAUDE.md so the invariant is not left quietly false.

**Tech Stack:** Python 3.12, Gmail API, Claude Haiku, SQLite.

## Global Constraints

- **Python 3.12**, run with `uv run`.
- **Timestamps:** `_at` is an instant with offset. `eta_on` is a bare `YYYY-MM-DD` — a delivery estimate has no time of day, and `pantry_items.expires_on` settles the precedent.
- **`app.db.connect()` only.**
- **Synced and derived writes bypass `app/mutations.py`.** `email_bodies` and `orders` are both derived from a sync. Neither joins `mutations.WRITABLE`.
- **The allowlist is configuration, never inference.** Nothing is fetched in full because its subject looked like an order.
- **Bodies are stored for allowlisted senders only**, and that is the one place the metadata rule is bent.
- **Commit after every task.**

## File Structure

| File | Responsibility | Action |
| :-- | :-- | :-- |
| `migrations/018_orders.sql` | `email_bodies`, `orders`, `order_items` | Create |
| `app/config.py` | `ORDER_SENDER_ALLOWLIST`, `ORDER_BODY_RETENTION_DAYS` | Modify |
| `ingest/gmail.py` | `fetch_full`, `body_text`, `store_body` | Modify |
| `ingest/orders.py` | The second pass: select, fetch, extract, store, prune | Create |
| `app/handlers.py` | `query` kind `'order'`, delivery line in `today_block` | Modify |
| `tests/test_orders_schema.py` | Schema, cascade, retention | Create |
| `tests/test_orders_extract.py` | Extraction shape, offline with a stubbed model | Create |

---

### Task 1: Schema

**Files:**
- Create: `migrations/018_orders.sql`
- Test: `tests/test_orders_schema.py`

**Interfaces:**
- Produces: `email_bodies(id, email_message_id UNIQUE, body_text, fetched_at)`, `orders(id, merchant, external_order_id, placed_at, status, eta_on, total_cents, email_message_id)`, `order_items(id, order_id, name, quantity, price_cents)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_orders_schema.py`:

```python
"""Order tracking. The one place email bodies are stored."""

import sqlite3

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "orders.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def _email(conn, external_id="e1"):
    return conn.execute(
        "INSERT INTO email_messages (external_id, sender, subject, received_at)"
        " VALUES (?,?,?,?)",
        (external_id, "ship-confirm@amazon.com", "Your order has shipped",
         "2026-08-04T12:00:00Z"),
    ).lastrowid


def test_tables_exist(conn):
    names = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"email_bodies", "orders", "order_items"} <= names


def test_body_cascades_with_its_message(conn):
    """email_messages is hard-deleted on prune. A body outliving its message
    is a body nothing can ever reach again."""
    email_id = _email(conn)
    conn.execute(
        "INSERT INTO email_bodies (email_message_id, body_text, fetched_at)"
        " VALUES (?,?,?)",
        (email_id, "Your package arrives Tuesday.", "2026-08-04T12:00:00Z"),
    )
    conn.commit()
    conn.execute("DELETE FROM email_messages WHERE id = ?", (email_id,))
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM email_bodies").fetchone()["n"] == 0


def test_one_body_per_message(conn):
    email_id = _email(conn)
    conn.execute(
        "INSERT INTO email_bodies (email_message_id, body_text, fetched_at)"
        " VALUES (?,?,?)",
        (email_id, "first", "2026-08-04T12:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO email_bodies (email_message_id, body_text, fetched_at)"
            " VALUES (?,?,?)",
            (email_id, "second", "2026-08-04T12:00:00Z"),
        )


def test_order_survives_its_email(conn):
    """The email ages out long before the package stops mattering. An order
    whose source email was pruned is still an order."""
    email_id = _email(conn)
    conn.execute(
        "INSERT INTO orders (merchant, status, email_message_id) VALUES (?,?,?)",
        ("amazon", "shipped", email_id),
    )
    conn.commit()
    conn.execute("DELETE FROM email_messages WHERE id = ?", (email_id,))
    conn.commit()
    row = conn.execute("SELECT email_message_id FROM orders").fetchone()
    assert row["email_message_id"] is None


def test_items_cascade_with_the_order(conn):
    order_id = conn.execute(
        "INSERT INTO orders (merchant, status) VALUES ('amazon','shipped')"
    ).lastrowid
    conn.execute(
        "INSERT INTO order_items (order_id, name, quantity) VALUES (?,?,?)",
        (order_id, "USB-C cable", 2),
    )
    conn.commit()
    conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM order_items").fetchone()["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orders_schema.py -v`
Expected: FAIL — `email_bodies` not in `sqlite_master`

- [ ] **Step 3: Write the migration**

Create `migrations/018_orders.sql`:

```sql
-- Order tracking, and the one place a message body is stored.
--
-- The general Gmail pass stays at format=metadata, which is what makes "there
-- is no path to storing bodies by accident" true. This adds a second, narrow
-- pass restricted to ORDER_SENDER_ALLOWLIST. The invariant becomes "no path
-- except this one", and CLAUDE.md is rewritten to say so — a documented
-- exception is worth more than an invariant that has quietly become false.

CREATE TABLE email_bodies (
  id               INTEGER PRIMARY KEY,
  -- CASCADE because email_messages is HARD-deleted on prune. A body that
  -- outlived its message is a body nothing can reach and nothing will ever
  -- delete.
  email_message_id INTEGER NOT NULL UNIQUE
                     REFERENCES email_messages(id) ON DELETE CASCADE,
  body_text        TEXT NOT NULL,
  fetched_at       TEXT NOT NULL
);

CREATE TABLE orders (
  id                INTEGER PRIMARY KEY,
  merchant          TEXT NOT NULL,            -- amazon|walmart
  -- The merchant's own order number when the mail carries one. Not unique:
  -- one order produces a confirmation and then a shipping mail, and both are
  -- worth extracting, but a NULL here must not collide with another NULL.
  external_order_id TEXT,
  placed_at         TEXT,                     -- ISO 8601 with offset
  status            TEXT NOT NULL,            -- placed|shipped|delivered|cancelled
  -- A bare date. A delivery estimate has no time of day, and inventing a
  -- midnight offset would be a lie the rest of the system would reason about.
  -- The same call pantry_items.expires_on made.
  eta_on            TEXT,
  total_cents       INTEGER,
  -- SET NULL, not CASCADE: the email ages out in weeks and the order still
  -- matters. An order that deletes itself when its receipt is pruned is an
  -- order you cannot ask about.
  email_message_id  INTEGER REFERENCES email_messages(id) ON DELETE SET NULL,
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  updated_at        TEXT
);

CREATE INDEX idx_orders_eta ON orders(eta_on) WHERE eta_on IS NOT NULL;
CREATE INDEX idx_orders_status ON orders(status, created_at DESC);

-- Partial unique index, the shape idx_events_ext already established: dedupe
-- real order numbers while allowing many NULLs for mail that carries none.
CREATE UNIQUE INDEX idx_orders_ext ON orders(merchant, external_order_id)
  WHERE external_order_id IS NOT NULL;

CREATE TABLE order_items (
  id           INTEGER PRIMARY KEY,
  order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  quantity     INTEGER NOT NULL DEFAULT 1,
  price_cents  INTEGER
);

CREATE INDEX idx_order_items_order ON order_items(order_id);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orders_schema.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add migrations/018_orders.sql tests/test_orders_schema.py
git commit -m "feat: orders schema, and the one table that holds email bodies"
```

---

### Task 2: Configuration

**Files:**
- Modify: `app/config.py`, `.env.example`

**Interfaces:**
- Produces: `config.ORDER_SENDER_ALLOWLIST: tuple[str, ...]`, `config.ORDER_BODY_RETENTION_DAYS: int`, `config.ORDER_EXTRACT_MODEL: str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_orders_config.py`:

```python
from app import config


def test_allowlist_is_a_tuple_of_lowercase_domains(monkeypatch):
    monkeypatch.setenv("ORDER_SENDER_ALLOWLIST", "Amazon.com, WALMART.com ,")
    import importlib

    importlib.reload(config)
    assert config.ORDER_SENDER_ALLOWLIST == ("amazon.com", "walmart.com")


def test_empty_allowlist_means_nothing_is_fetched(monkeypatch):
    """The default must be empty. A default that fetched bodies from somewhere
    would make the metadata rule false on a fresh checkout, silently."""
    monkeypatch.delenv("ORDER_SENDER_ALLOWLIST", raising=False)
    import importlib

    importlib.reload(config)
    assert config.ORDER_SENDER_ALLOWLIST == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orders_config.py -v`
Expected: FAIL — `AttributeError: module 'app.config' has no attribute 'ORDER_SENDER_ALLOWLIST'`

- [ ] **Step 3: Implement**

In `app/config.py`, beside the other feature settings:

```python
# Whose mail may be fetched in full.
#
# The general Gmail pass is format=metadata and stays that way. This list is
# the only thing that opens a body, and it is configuration rather than
# inference on purpose: nothing is fetched in full because its subject looked
# like an order. Empty by default, so a fresh checkout stores no bodies at all
# and the metadata rule holds until someone deliberately relaxes it.
ORDER_SENDER_ALLOWLIST: tuple[str, ...] = tuple(
    part.strip().lower()
    for part in os.getenv("ORDER_SENDER_ALLOWLIST", "").split(",")
    if part.strip()
)

# Bodies are kept longer than the metadata prune keeps messages, because the
# whole reason they are retained is re-parsing as the extractor improves.
ORDER_BODY_RETENTION_DAYS = int(os.getenv("ORDER_BODY_RETENTION_DAYS", "180"))

ORDER_EXTRACT_MODEL = os.getenv("ORDER_EXTRACT_MODEL", "claude-haiku-4-5").strip()
```

Add to `.env.example`:

```bash
# Order tracking. Empty means no message body is ever fetched or stored —
# which is the default posture and the one CLAUDE.md describes. Adding a
# domain here opens bodies from that sender ONLY.
ORDER_SENDER_ALLOWLIST=amazon.com,walmart.com
ORDER_BODY_RETENTION_DAYS=180
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orders_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/config.py .env.example tests/test_orders_config.py
git commit -m "feat: ORDER_SENDER_ALLOWLIST, empty by default"
```

---

### Task 3: Fetch a full body, for allowlisted senders only

**Files:**
- Modify: `ingest/gmail.py`
- Test: `tests/test_order_bodies.py`

**Interfaces:**
- Consumes: `gmail.get`, `gmail.BASE`, `config.ORDER_SENDER_ALLOWLIST`.
- Produces: `gmail.is_allowlisted(sender: str | None) -> bool`, `gmail.fetch_full(message_id: str) -> dict | None`, `gmail.body_text(message: dict) -> str`, `gmail.store_body(conn, email_message_id: int, text: str) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_order_bodies.py`:

```python
"""format=full, for an allowlist and nothing else."""

import base64
import sqlite3

import pytest

from ingest import gmail
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "bodies.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_allowlist_matches_on_domain(monkeypatch):
    monkeypatch.setattr(gmail.config, "ORDER_SENDER_ALLOWLIST", ("amazon.com",))
    assert gmail.is_allowlisted("Amazon.com <ship-confirm@amazon.com>")
    assert gmail.is_allowlisted("ship-confirm@AMAZON.COM")


def test_lookalike_domain_is_not_allowlisted(monkeypatch):
    """The check is on the domain, not a substring. `amazon.com.evil.example`
    must not match, or the allowlist is decorative."""
    monkeypatch.setattr(gmail.config, "ORDER_SENDER_ALLOWLIST", ("amazon.com",))
    assert not gmail.is_allowlisted("noreply@amazon.com.evil.example")
    assert not gmail.is_allowlisted("noreply@notamazon.com")


def test_empty_allowlist_matches_nothing(monkeypatch):
    monkeypatch.setattr(gmail.config, "ORDER_SENDER_ALLOWLIST", ())
    assert not gmail.is_allowlisted("ship-confirm@amazon.com")


def test_body_text_prefers_plain_over_html():
    message = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<p>html</p>").decode()},
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"plain text").decode()},
                },
            ],
        }
    }
    assert gmail.body_text(message) == "plain text"


def test_body_text_falls_back_to_html_stripped():
    message = {
        "payload": {
            "mimeType": "text/html",
            "body": {
                "data": base64.urlsafe_b64encode(
                    b"<html><body><p>Arrives Tuesday</p></body></html>"
                ).decode()
            },
        }
    }
    assert "Arrives Tuesday" in gmail.body_text(message)
    assert "<p>" not in gmail.body_text(message)


def test_store_body_is_idempotent(conn):
    email_id = conn.execute(
        "INSERT INTO email_messages (external_id, sender, received_at)"
        " VALUES ('e1','ship@amazon.com','2026-08-04T12:00:00Z')"
    ).lastrowid
    gmail.store_body(conn, email_id, "first")
    gmail.store_body(conn, email_id, "second")
    conn.commit()
    rows = conn.execute("SELECT body_text FROM email_bodies").fetchall()
    assert len(rows) == 1
    assert rows[0]["body_text"] == "second"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_order_bodies.py -v`
Expected: FAIL — `AttributeError: module 'ingest.gmail' has no attribute 'is_allowlisted'`

- [ ] **Step 3: Implement**

In `ingest/gmail.py`, below `fetch_metadata`:

```python
def is_allowlisted(sender: str | None) -> bool:
    """Whether this sender's body may be fetched.

    Matches on the domain, not a substring: `amazon.com.evil.example` must not
    pass a list containing `amazon.com`, or the allowlist is decorative.
    """
    if not sender or not config.ORDER_SENDER_ALLOWLIST:
        return False
    at = sender.rfind("@")
    if at == -1:
        return False
    domain = sender[at + 1 :].strip().strip(">").strip().lower()
    return any(
        domain == allowed or domain.endswith("." + allowed)
        for allowed in config.ORDER_SENDER_ALLOWLIST
    )


def fetch_full(message_id: str) -> dict | None:
    """One message, body included.

    THE exception to the metadata rule. Every caller must check
    `is_allowlisted` first; this function does not check, because a function
    that silently returns None for a non-allowlisted sender is one whose
    callers stop thinking about the question.
    """
    try:
        return get(f"{BASE}/messages/{message_id}", {"format": "full"})
    except ApiError as exc:
        if exc.status == 404:
            return None
        raise


def _walk_parts(part: dict):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


def body_text(message: dict) -> str:
    """Readable text out of a Gmail payload.

    Plain text wins when both are present — order confirmations carry the same
    information in both, and the HTML half is mostly layout that would triple
    the extractor's input tokens for nothing.
    """
    plain, html = "", ""
    for part in _walk_parts(message.get("payload", {}) or {}):
        data = (part.get("body", {}) or {}).get("data")
        if not data:
            continue
        try:
            decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — a malformed part is not a failed sync
            continue
        mime = part.get("mimeType", "")
        if mime == "text/plain" and not plain:
            plain = decoded
        elif mime == "text/html" and not html:
            html = decoded

    if plain.strip():
        return plain.strip()
    if html.strip():
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return html_module.unescape(re.sub(r"\s+", " ", text)).strip()
    return ""


def store_body(conn, email_message_id: int, text: str) -> None:
    """Upsert. Re-fetching a message must not accumulate rows.

    Not through the mutations helper: this is derived from a sync, and a sync
    is not a user action.
    """
    conn.execute(
        """INSERT INTO email_bodies (email_message_id, body_text, fetched_at)
             VALUES (?,?,?)
             ON CONFLICT(email_message_id) DO UPDATE SET
               body_text  = excluded.body_text,
               fetched_at = excluded.fetched_at""",
        (email_message_id, text, timeutil.to_utc_iso(timeutil.now("UTC"))),
    )
```

Add the imports this needs at the top of `ingest/gmail.py`:

```python
import base64
import html as html_module
import re
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_order_bodies.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add ingest/gmail.py tests/test_order_bodies.py
git commit -m "feat: fetch full bodies for allowlisted senders only"
```

---

### Task 4: Extract orders

**Files:**
- Create: `ingest/orders.py`
- Test: `tests/test_orders_extract.py`

**Interfaces:**
- Consumes: `gmail.is_allowlisted`, `gmail.fetch_full`, `gmail.body_text`, `gmail.store_body`, `ingest.state`, `config.ORDER_EXTRACT_MODEL`.
- Produces: `orders.EXTRACT_TOOL: dict`, `orders.extract(body: str, subject: str, received_at: str) -> dict | None`, `orders.store(conn, merchant, extracted, email_message_id) -> int | None`, `orders.sync(limit: int = 40) -> dict`, `orders.main() -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_orders_extract.py`:

```python
"""Extraction is one Haiku call over one body, with a forced tool.

Offline: the model is stubbed. What is asserted is the shape the rest of the
system depends on, not the model's judgement.
"""

import sqlite3

import pytest

from ingest import orders
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "extract.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_tool_forbids_inventing_an_eta():
    """eta_on is nullable in the schema and must be nullable in the tool. An
    extractor that always produces a date produces a wrong date."""
    props = orders.EXTRACT_TOOL["input_schema"]["properties"]
    assert "eta_on" in props
    assert "eta_on" not in orders.EXTRACT_TOOL["input_schema"].get("required", [])


def test_status_is_a_closed_enum():
    props = orders.EXTRACT_TOOL["input_schema"]["properties"]
    assert set(props["status"]["enum"]) == {"placed", "shipped", "delivered", "cancelled"}


def test_store_writes_order_and_items(conn):
    email_id = conn.execute(
        "INSERT INTO email_messages (external_id, sender, received_at)"
        " VALUES ('e1','ship@amazon.com','2026-08-04T12:00:00Z')"
    ).lastrowid
    order_id = orders.store(
        conn,
        "amazon",
        {
            "external_order_id": "111-2223334",
            "status": "shipped",
            "eta_on": "2026-08-06",
            "total_cents": 2599,
            "items": [{"name": "USB-C cable", "quantity": 2, "price_cents": 1299}],
        },
        email_id,
    )
    conn.commit()
    assert order_id
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert row["eta_on"] == "2026-08-06"
    assert row["status"] == "shipped"
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchall()
    assert len(items) == 1


def test_second_mail_updates_the_same_order(conn):
    """A confirmation and then a shipping mail are one order, not two. This
    is what the partial unique index on (merchant, external_order_id) is for."""
    email_id = conn.execute(
        "INSERT INTO email_messages (external_id, sender, received_at)"
        " VALUES ('e2','ship@amazon.com','2026-08-04T12:00:00Z')"
    ).lastrowid
    first = orders.store(
        conn, "amazon", {"external_order_id": "111-999", "status": "placed"}, email_id
    )
    second = orders.store(
        conn,
        "amazon",
        {"external_order_id": "111-999", "status": "shipped", "eta_on": "2026-08-07"},
        email_id,
    )
    conn.commit()
    assert first == second
    row = conn.execute("SELECT status, eta_on FROM orders").fetchone()
    assert row["status"] == "shipped"
    assert row["eta_on"] == "2026-08-07"


def test_order_with_no_number_still_stores(conn):
    """Some shipping mail carries no order number. It is still a delivery
    arriving on a day, which is the question this feature answers."""
    email_id = conn.execute(
        "INSERT INTO email_messages (external_id, sender, received_at)"
        " VALUES ('e3','ship@walmart.com','2026-08-04T12:00:00Z')"
    ).lastrowid
    assert orders.store(
        conn, "walmart", {"status": "shipped", "eta_on": "2026-08-05"}, email_id
    )
    conn.commit()
    assert conn.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orders_extract.py -v`
Expected: FAIL — no module `ingest.orders`

- [ ] **Step 3: Implement**

Create `ingest/orders.py`:

```python
"""The second Gmail pass: order mail, in full, from an allowlist.

This is the one place in the system that stores a message body, and it does so
only for senders named in ORDER_SENDER_ALLOWLIST. The general pass in
ingest.gmail stays at format=metadata.

Bodies are retained rather than discarded after extraction, because the
extractor will improve and refetching a body Gmail has aged out is not
available. The cost is disk and a wider blast radius on the allowlisted
senders; FileVault is the machine's answer to the second.

Derived from a sync, so nothing here goes through the mutations helper.
"""

import json

import anthropic

from app import config, timeutil
from app.db import transaction
from ingest import gmail, state

SOURCE = "orders"

_CLIENT: anthropic.Anthropic | None = None

# Forced tool use, the same posture as the router: never free text to parse.
EXTRACT_TOOL = {
    "name": "record_order",
    "description": (
        "Record the order described by this email. Only fields the email "
        "actually states."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "external_order_id": {
                "type": "string",
                "description": "The merchant's order number, exactly as printed.",
            },
            "status": {
                "type": "string",
                "enum": ["placed", "shipped", "delivered", "cancelled"],
                "description": "What this email says has happened.",
            },
            "eta_on": {
                "type": "string",
                "description": (
                    "Estimated delivery date as YYYY-MM-DD, ONLY if the email "
                    "states one. Omit it entirely otherwise — never estimate, "
                    "never infer from the send date."
                ),
            },
            "total_cents": {
                "type": "integer",
                "description": "Order total in cents, if the email states one.",
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "integer"},
                        "price_cents": {"type": "integer"},
                    },
                    "required": ["name"],
                },
            },
        },
        # Only status is required. An email that names no order number, no
        # date and no total is still a status change worth recording, and a
        # schema that demanded more would push the model into inventing it.
        "required": ["status"],
    },
}


def _client() -> anthropic.Anthropic:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=config.anthropic_api_key())
    return _CLIENT


def extract(body: str, subject: str, received_at: str) -> dict | None:
    """One Haiku call over one body. Returns None on anything unusable.

    Capped at 6000 characters of body: order confirmations put everything that
    matters in the first screenful and the rest is footer, unsubscribe links
    and legal text.
    """
    try:
        response = _client().messages.create(
            model=config.ORDER_EXTRACT_MODEL,
            max_tokens=1024,
            system=(
                "You extract order details from retail email. Record only what "
                "the email states. If it does not give a delivery date, omit "
                "eta_on rather than guessing — a wrong date is worse than none. "
                f"This email arrived at {received_at}."
            ),
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "any"},
            messages=[
                {"role": "user", "content": f"Subject: {subject}\n\n{body[:6000]}"}
            ],
        )
    except Exception:  # noqa: BLE001 — a failed extraction is not a failed sync
        return None

    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)
    return None


def store(conn, merchant: str, extracted: dict, email_message_id: int) -> int | None:
    """Upsert one order and replace its items.

    Keyed on (merchant, external_order_id) via the partial unique index, so a
    confirmation followed by a shipping notice updates one row rather than
    producing two. Mail with no order number always inserts — there is nothing
    to match it against, and a delivery arriving on a day is still worth
    having.
    """
    status = extracted.get("status")
    if not status:
        return None

    external_id = extracted.get("external_order_id")
    now = timeutil.to_utc_iso(timeutil.now("UTC"))

    existing = None
    if external_id:
        existing = conn.execute(
            "SELECT id FROM orders WHERE merchant = ? AND external_order_id = ?",
            (merchant, external_id),
        ).fetchone()

    if existing:
        order_id = int(existing["id"])
        conn.execute(
            """UPDATE orders SET status = ?,
                                 eta_on = coalesce(?, eta_on),
                                 total_cents = coalesce(?, total_cents),
                                 email_message_id = ?,
                                 updated_at = ?
                 WHERE id = ?""",
            (
                status,
                extracted.get("eta_on"),
                extracted.get("total_cents"),
                email_message_id,
                now,
                order_id,
            ),
        )
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
    else:
        order_id = int(
            conn.execute(
                """INSERT INTO orders
                     (merchant, external_order_id, status, eta_on, total_cents,
                      email_message_id, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    merchant,
                    external_id,
                    status,
                    extracted.get("eta_on"),
                    extracted.get("total_cents"),
                    email_message_id,
                    now,
                ),
            ).lastrowid
        )

    for item in extracted.get("items", []) or []:
        if not item.get("name"):
            continue
        conn.execute(
            "INSERT INTO order_items (order_id, name, quantity, price_cents)"
            " VALUES (?,?,?,?)",
            (order_id, item["name"], int(item.get("quantity") or 1), item.get("price_cents")),
        )

    return order_id


def _merchant_of(sender: str) -> str:
    lowered = (sender or "").lower()
    if "walmart" in lowered:
        return "walmart"
    if "amazon" in lowered:
        return "amazon"
    return "other"


def sync(limit: int = 40) -> dict:
    """One pass over unexamined allowlisted mail. Never raises."""
    if not config.ORDER_SENDER_ALLOWLIST:
        return {"ok": True, "stored": 0, "detail": "allowlist empty — nothing fetched"}

    with transaction() as conn:
        state.start(conn, SOURCE)
        candidates = [
            dict(r)
            for r in conn.execute(
                """SELECT id, external_id, sender, subject, received_at
                     FROM email_messages
                    WHERE id NOT IN (SELECT email_message_id FROM email_bodies)
                    ORDER BY received_at DESC LIMIT ?""",
                (limit * 4,),
            ).fetchall()
        ]

    # Filter in Python rather than SQL: the allowlist is a domain rule, and
    # a LIKE '%amazon.com%' would match amazon.com.evil.example — the exact
    # bug is_allowlisted exists to prevent.
    #
    # The LIMIT above is deliberately 4x, then narrowed here. sync_proposals
    # applied its LIMIT before intersecting with its narrow query and returned
    # a hundred non-matching candidates every run, examined zero, and never
    # advanced its window. Do not repeat that.
    wanted = [row for row in candidates if gmail.is_allowlisted(row["sender"])][:limit]

    stored = 0
    for row in wanted:
        message = gmail.fetch_full(row["external_id"])
        if message is None:
            continue
        text = gmail.body_text(message)
        if not text:
            continue
        with transaction() as conn:
            gmail.store_body(conn, row["id"], text)
        extracted = extract(text, row["subject"] or "", row["received_at"])
        if not extracted:
            continue
        with transaction() as conn:
            if store(conn, _merchant_of(row["sender"]), extracted, row["id"]):
                stored += 1

    with transaction() as conn:
        state.succeeded(
            conn, SOURCE, None, f"examined={len(wanted)} stored={stored}"
        )
    return {"ok": True, "stored": stored, "detail": f"examined={len(wanted)} stored={stored}"}


def prune(conn) -> int:
    """Drop bodies past the retention window. Orders are untouched."""
    cutoff = timeutil.to_utc_iso(
        timeutil.now("UTC") - timeutil.timedelta(days=config.ORDER_BODY_RETENTION_DAYS)
    )
    cur = conn.execute("DELETE FROM email_bodies WHERE fetched_at < ?", (cutoff,))
    return cur.rowcount


def main() -> int:
    result = sync()
    print(result["detail"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

If `timeutil` has no `timedelta` re-export, import `datetime.timedelta`
directly in this module rather than adding one.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orders_extract.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add ingest/orders.py tests/test_orders_extract.py
git commit -m "feat: extract orders from allowlisted mail"
```

---

### Task 5: Make deliveries answerable

**Files:**
- Modify: `app/handlers.py`, `app/router.py`
- Test: `tests/test_orders_context.py`

**Interfaces:**
- Produces: `handlers.open_orders(conn, limit=10) -> list[dict]`; `query` accepts `kind='order'`; `today_block` gains an arriving-today line.

- [ ] **Step 1: Write the failing test**

Create `tests/test_orders_context.py`:

```python
import sqlite3

import pytest

from app import handlers, timeutil
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "octx.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _order(conn, status="shipped", eta_on=None, merchant="amazon"):
    conn.execute(
        "INSERT INTO orders (merchant, status, eta_on) VALUES (?,?,?)",
        (merchant, status, eta_on),
    )
    conn.commit()


def test_arriving_today_appears_in_today_block(conn):
    today = timeutil.now("America/Denver").date().isoformat()
    _order(conn, eta_on=today)
    assert "arriv" in handlers.today_block(conn, "America/Denver").lower()


def test_delivered_order_is_not_arriving(conn):
    today = timeutil.now("America/Denver").date().isoformat()
    _order(conn, status="delivered", eta_on=today)
    assert "arriv" not in handlers.today_block(conn, "America/Denver").lower()


def test_open_orders_excludes_delivered(conn):
    _order(conn, status="shipped", eta_on="2026-08-06")
    _order(conn, status="delivered", eta_on="2026-08-01", merchant="walmart")
    open_ones = handlers.open_orders(conn)
    assert len(open_ones) == 1
    assert open_ones[0]["merchant"] == "amazon"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orders_context.py -v`
Expected: FAIL — no `handlers.open_orders`

- [ ] **Step 3: Implement**

In `app/handlers.py`:

```python
def open_orders(conn, limit: int = 10) -> list[dict]:
    """Orders that have not arrived. Delivered and cancelled are history."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, merchant, external_order_id, status, eta_on, total_cents
                 FROM orders
                WHERE status IN ('placed','shipped')
                ORDER BY coalesce(eta_on, '9999-12-31'), id DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    ]
```

In `today_block`, beside the missed-call line (Part 3) or the existing agenda
lines, using the local date `today_block` already computes:

```python
    # A delivery arriving today is a fact about the day in the way an
    # appointment is. Delivered ones are not — that is history.
    arriving = conn.execute(
        """SELECT merchant FROM orders
             WHERE status IN ('placed','shipped') AND eta_on = ?""",
        (local_date_iso,),
    ).fetchall()
    for order in arriving:
        lines.append(f"DELIVERY: a {order['merchant']} order is arriving today")
```

In `app/router.py`, add `"order"` to the `query` tool's `kind` enum and one
clause to its description:

```
A question about a package, a delivery or something ordered is kind='order'.
```

In `handlers.query`, add an `order` branch before the generic search:

```python
    if kind == "order":
        for order in open_orders(conn):
            when = order["eta_on"] or "no date given"
            lines.append(
                f"ORDER: {order['merchant']} — {order['status']}, arriving {when}"
            )
        if not lines:
            return "You've got nothing on the way."
        return router.answer(args["question"], "\n".join(lines), tz_name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orders_context.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the routing regression suite**

Run: `uv run pytest tests/test_utterances.py tests/test_router_prompt.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/handlers.py app/router.py tests/test_orders_context.py
git commit -m "feat: answer questions about deliveries"
```

---

### Task 6: Schedule it, and rewrite the invariant

**Files:**
- Modify: `deploy/` ingestion job, `CLAUDE.md`

- [ ] **Step 1: Add `ingest.orders` to the ingestion schedule**

Beside Calendar and Gmail. It makes a metered Haiku call per new order email,
so run it on the same tick as Gmail rather than more often.

- [ ] **Step 2: Rewrite the CLAUDE.md claim that is now false**

The Ingestion section currently says:

> Message bodies are never stored. `format=metadata` means Gmail does not
> return them, so there is no path to storing them by accident.

Replace it with a statement that is true: the general pass is still
`format=metadata` and still cannot store a body; `ingest/orders.py` is a
second pass that fetches `format=full` for `ORDER_SENDER_ALLOWLIST` and
nothing else; the allowlist is empty by default so a fresh checkout behaves as
before; bodies live in `email_bodies`, cascade with their message, and are
pruned on their own longer window because re-parsing is the reason they are
kept.

Record that this is the **fourth** metered call in the system, after the
report summary, receipt extraction and the brief — and that like the others it
stays out of `/metrics`, which is per-utterance.

- [ ] **Step 3: Commit**

```bash
git add deploy/ CLAUDE.md
git commit -m "docs: the metadata rule now has exactly one documented exception"
```

---

## Self-review

Checked against the spec's Part 4:

- Second narrow pass at `format=full`, allowlist only — Tasks 2, 3, 4.
- Allowlist is configuration, not inference — Task 2, empty by default, and
  `test_lookalike_domain_is_not_allowlisted` pins the domain check.
- Bodies retained for re-parsing — `email_bodies` with its own longer
  retention in `orders.prune`.
- `email_bodies` cascades from `email_messages` — Task 1,
  `test_body_cascades_with_its_message`.
- `orders` uses `eta_on`, a bare date — Task 1, with the reasoning.
- `query` gains `kind='order'`, `today_block` gains an arriving line — Task 5.
- CLAUDE.md's invariant rewritten rather than left false — Task 6.

**One thing the spec did not anticipate, added here:** the `LIMIT`-before-filter
trap. `sync_proposals` applies its `LIMIT` before intersecting with its narrow
Gmail query, which is why it returned a hundred non-matching candidates every
run and examined zero. `orders.sync` over-fetches 4x and narrows in Python,
with a comment saying why. This is the specific bug that made the email review
queue dormant; repeating it here would be repeating it knowingly.

## Next

**Go to [`2026-08-04-part5-commerce.md`](2026-08-04-part5-commerce.md).**

Part 5 consumes the `orders` table and the allowlist established here.
