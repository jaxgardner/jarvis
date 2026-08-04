# Part 3 — Messages and calls: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the assistant answer "did Sarah text back?" and "did I miss a call?" from the Mac's own Messages and call-history databases.

**Architecture:** Both databases are blocked by TCC — verified on this machine, `chat.db` returns `authorization denied` and `CallHistoryDB/` returns `Operation not permitted` even to `ls`. A tiny compiled helper holds Full Disk Access and does the **minimum possible**: read rows, emit bytes as NDJSON. Every piece of parsing — the typedstream blob, the Core Data epoch — happens unprivileged in Python, where it is testable with fixtures. Import follows the existing `sync_state` cursor pattern and bypasses the mutations helper, exactly as Calendar and Gmail do.

**Tech Stack:** Python 3.12, Swift (helper binary), SQLite/FTS5.

## Global Constraints

- **Python 3.12**, run with `uv run`.
- **Timestamps are ISO 8601 with offset.** `_at` is an instant.
- **`app.db.connect()` only.**
- **Synced writes bypass `app/mutations.py`.** The log exists to make *voice* input reversible; a few thousand imported texts would bury the user's last real action and make `/undo` useless for what it was built for. This is the rule Calendar and Gmail already follow.
- **Read-only, one direction.** Nothing is ever written back to Messages.
- **The privileged binary parses nothing.** Its whole surface is two queries and base64. Any bug in parsing must be a bug in unprivileged code.
- **Commit after every task.**

## File Structure

| File | Responsibility | Action |
| :-- | :-- | :-- |
| `migrations/017_messages_calls.sql` | `messages`, `messages_fts` + triggers, `calls` | Create |
| `ingest/typedstream.py` | Decode `attributedBody` → text. Pure, no I/O | Create |
| `helpers/tccread/main.swift` | The FDA binary. Reads rows, emits NDJSON | Create |
| `helpers/tccread/build.sh` | `swiftc` invocation + signing | Create |
| `ingest/messages.py` | Importer: run helper, decode, store, cursor | Create |
| `ingest/calls.py` | Importer: Core Data epoch, store, cursor | Create |
| `app/handlers.py` | `search_messages`, `query` kinds, missed-call line | Modify |
| `tests/test_typedstream.py` | Blob decoding against real captured bytes | Create |
| `tests/test_messages_ingest.py` | Import, dedupe, cursor, absent-helper | Create |

---

### Task 1: Schema

**Files:**
- Create: `migrations/017_messages_calls.sql`
- Test: `tests/test_messages_ingest.py`

**Interfaces:**
- Produces: `messages(id, external_id UNIQUE, handle, direction, body, service, sent_at, person_id)`, `messages_fts`, `calls(id, external_id UNIQUE, handle, direction, answered, duration_s, occurred_at, person_id)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_messages_ingest.py`:

```python
"""Texts and missed calls, imported read-only from the Mac's own databases."""

import sqlite3

import pytest

from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "msg.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def test_tables_exist(conn):
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"messages", "calls"} <= names


def test_external_id_dedupes(conn):
    conn.execute(
        "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
        " VALUES ('m1','+15551234','in','hello','2026-08-04T10:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
            " VALUES ('m1','+15551234','in','hello again','2026-08-04T10:01:00Z')"
        )


def test_fts_finds_a_message(conn):
    conn.execute(
        "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
        " VALUES ('m2','+15551234','in','the landlord replied about the fence',"
        "'2026-08-04T10:00:00Z')"
    )
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'fence'"
    ).fetchall()
    assert len(hits) == 1


def test_hard_delete_leaves_the_index_clean(conn):
    """Unlike notes, messages are hard-deleted when they age out, so the FTS
    index needs no join-and-filter to stay honest."""
    conn.execute(
        "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
        " VALUES ('m3','+1','in','disposable','2026-08-04T10:00:00Z')"
    )
    conn.commit()
    conn.execute("DELETE FROM messages WHERE external_id = 'm3'")
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'disposable'"
    ).fetchall()
    assert hits == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_messages_ingest.py -v`
Expected: FAIL — `messages` is not in `sqlite_master`

- [ ] **Step 3: Write the migration**

Create `migrations/017_messages_calls.sql`:

```sql
-- Texts and calls, read-only from the Mac's own databases.
--
-- Both sources are TCC-protected and reached through helpers/tccread, a
-- compiled binary that holds Full Disk Access. Nothing is ever written back:
-- this is an importer, in one direction, like Calendar and Gmail.

CREATE TABLE messages (
  id           INTEGER PRIMARY KEY,
  -- chat.db's message ROWID, stringified. Unique so a re-import is a no-op
  -- rather than a duplicate — the same posture as idx_events_ext.
  external_id  TEXT NOT NULL UNIQUE,
  handle       TEXT NOT NULL,          -- phone number or Apple ID as stored
  direction    TEXT NOT NULL,          -- in|out
  body         TEXT NOT NULL,
  service      TEXT,                   -- iMessage|SMS
  sent_at      TEXT NOT NULL,          -- ISO 8601 with offset
  -- Nullable: most handles will not match anyone in `people`, and a text from
  -- a number you have never named is still a text worth having.
  person_id    INTEGER REFERENCES people(id) ON DELETE SET NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_messages_sent ON messages(sent_at DESC);
CREATE INDEX idx_messages_handle ON messages(handle, sent_at DESC);

-- External-content FTS5 with the three sync triggers, matching notes_fts.
--
-- Unlike notes, messages are HARD-deleted when they age out, so the delete
-- trigger fires and the index stays honest on its own. Search needs no join
-- back to `messages` to filter tombstones — the subtlety that applies to
-- notes deliberately does not apply here.
CREATE VIRTUAL TABLE messages_fts USING fts5(body, content='messages', content_rowid='id');

CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, body) VALUES ('delete', old.id, old.body);
  INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;

CREATE TABLE calls (
  id           INTEGER PRIMARY KEY,
  external_id  TEXT NOT NULL UNIQUE,   -- ZCALLRECORD.Z_PK, stringified
  handle       TEXT NOT NULL,
  direction    TEXT NOT NULL,          -- in|out
  -- Whether it was picked up. A missed call is the whole point of this table;
  -- an answered one is context for it.
  answered     INTEGER NOT NULL DEFAULT 0,
  duration_s   INTEGER NOT NULL DEFAULT 0,
  occurred_at  TEXT NOT NULL,          -- ISO 8601, converted from Core Data epoch
  person_id    INTEGER REFERENCES people(id) ON DELETE SET NULL,
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX idx_calls_occurred ON calls(occurred_at DESC);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_messages_ingest.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add migrations/017_messages_calls.sql tests/test_messages_ingest.py
git commit -m "feat: messages and calls schema"
```

---

### Task 2: Decode `attributedBody`

**Files:**
- Create: `ingest/typedstream.py`, `tests/test_typedstream.py`

**Interfaces:**
- Produces: `typedstream.decode(blob: bytes) -> str | None`.

**Why this exists:** modern macOS stores message text in `attributedBody`, an NSArchiver typedstream, and leaves `message.text` NULL. An importer reading `text` alone appears to work and silently drops most of the corpus. This is the single most likely way this feature ships broken.

- [ ] **Step 1: Capture a real fixture**

This needs Task 3's binary to exist, so **do Task 3 first if you are working strictly in order** — or capture the blob now if you already have Full Disk Access on a terminal:

```bash
sqlite3 ~/Library/Messages/chat.db \
  "SELECT hex(attributedBody) FROM message WHERE attributedBody IS NOT NULL LIMIT 1;" \
  > tests/fixtures/attributed_body.hex
```

If that returns `authorization denied`, grant your terminal Full Disk Access
temporarily (System Settings → Privacy & Security → Full Disk Access), capture
the fixture, then **revoke it again** — the whole point of Task 3 is that the
grant lives on a single-purpose binary rather than on a shell.

- [ ] **Step 2: Write the failing test**

Create `tests/test_typedstream.py`:

```python
"""attributedBody carries the message text; `text` is usually NULL.

Real captured bytes, not a hand-written approximation — a fixture you wrote
yourself only proves the decoder matches what you imagined the format was.
"""

from pathlib import Path

import pytest

from ingest import typedstream

FIXTURE = Path(__file__).parent / "fixtures" / "attributed_body.hex"


@pytest.mark.skipif(not FIXTURE.exists(), reason="no captured attributedBody fixture")
def test_decodes_real_blob():
    blob = bytes.fromhex(FIXTURE.read_text().strip())
    text = typedstream.decode(blob)
    assert text
    assert "\x00" not in text


def test_empty_blob_is_none():
    assert typedstream.decode(b"") is None


def test_garbage_is_none_not_an_exception():
    """A blob this decoder does not understand must not take down an import
    of six thousand messages."""
    assert typedstream.decode(b"\x01\x02\x03not a typedstream") is None


def test_plain_marker_payload():
    """The shape the decoder keys on: an NSString marker, a length byte, then
    UTF-8. Synthetic, so the parser's contract is pinned even when the real
    fixture is absent on a fresh clone."""
    blob = (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84"
        b"\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92"
        b"\x84\x84\x84\x08NSString\x01\x94\x84\x01\x2b\x05hello\x86"
    )
    assert typedstream.decode(blob) == "hello"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_typedstream.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest.typedstream'`

- [ ] **Step 4: Implement**

Create `ingest/typedstream.py`:

```python
"""Pull the text out of a Messages `attributedBody` blob.

Modern macOS stores message text as an NSArchiver typedstream in
`message.attributedBody` and leaves `message.text` NULL. An importer that
reads `text` alone appears to work on old rows and silently drops most of the
corpus — which is the failure you notice months later, when the assistant
insists someone never texted you.

This is a deliberately narrow reader, not a general typedstream parser. It
finds the NSString payload and returns it. Anything it does not recognise
returns None, because the alternative — raising — would take down an import of
several thousand messages over one malformed row.

Unprivileged by design: `helpers/tccread` holds Full Disk Access and does
nothing but read bytes, so every parsing bug lives here, where it is testable
against a fixture and cannot be reached with elevated permissions.
"""

_MARKER = b"NSString"
# The archive uses 0x81 to introduce a two-byte little-endian length for
# strings longer than 0x80; shorter ones carry their length in one byte.
_LONG = 0x81


def decode(blob: bytes | None) -> str | None:
    if not blob:
        return None

    index = blob.find(_MARKER)
    if index == -1:
        return None

    # Skip the class name, then the small run of type/version bytes that
    # separates it from the payload. The '+' (0x2b) is the type code for the
    # string that follows.
    cursor = blob.find(b"+", index)
    if cursor == -1:
        return None
    cursor += 1

    if cursor >= len(blob):
        return None

    length = blob[cursor]
    cursor += 1
    if length == _LONG:
        if cursor + 2 > len(blob):
            return None
        length = int.from_bytes(blob[cursor : cursor + 2], "little")
        cursor += 2

    payload = blob[cursor : cursor + length]
    if len(payload) != length:
        return None

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None

    text = text.strip("\x00").strip()
    return text or None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_typedstream.py -v`
Expected: PASS. If `test_decodes_real_blob` fails against your captured
fixture, **the fixture is right and the decoder is wrong** — adjust the
decoder, never the fixture.

- [ ] **Step 6: Commit**

```bash
git add ingest/typedstream.py tests/test_typedstream.py tests/fixtures/
git commit -m "feat: decode Messages attributedBody blobs"
```

---

### Task 3: The privileged helper

**Files:**
- Create: `helpers/tccread/main.swift`, `helpers/tccread/build.sh`

**Interfaces:**
- Produces: `helpers/tccread/tccread`, a binary supporting
  `tccread messages --since <iso8601> --limit <n>` and
  `tccread calls --since <iso8601> --limit <n>`, both emitting NDJSON on stdout,
  exit 0 on success, exit 2 when TCC denies access.

**Why a separate binary:** Full Disk Access is granted to an executable, and granting it to `.venv/bin/python` would grant it to every script that interpreter ever runs — including anything a deep job decides to execute. This binary's entire surface is two SELECTs and base64.

- [ ] **Step 1: Write the helper**

Create `helpers/tccread/main.swift`:

```swift
// Reads the two TCC-protected databases and prints NDJSON. Nothing else.
//
// This binary is the only thing in the system holding Full Disk Access, so it
// is deliberately the least interesting program here: it parses nothing,
// interprets nothing, and writes nothing. attributedBody goes out as base64
// and is decoded in Python, where a bug is a failed test rather than a
// privileged crash.
//
// Exit codes: 0 ok, 2 TCC denied, 3 usage, 4 sqlite error.

import Foundation
import SQLite3

let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

func fail(_ message: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

func openReadOnly(_ path: String) -> OpaquePointer {
    var db: OpaquePointer?
    // mode=ro because chat.db is live and WAL. We are a reader and must never
    // be anything else.
    let uri = "file:\(path)?mode=ro"
    let rc = sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil)
    guard rc == SQLITE_OK, let handle = db else {
        // TCC surfaces as a plain open failure, so distinguish it by asking
        // the filesystem whether the file is there at all.
        if !FileManager.default.isReadableFile(atPath: path) {
            fail("tcc-denied: \(path)", 2)
        }
        fail("sqlite-open-failed: \(path)", 4)
    }
    return handle
}

func rows(_ db: OpaquePointer, _ sql: String, _ bind: [Any]) -> [[String: Any]] {
    var stmt: OpaquePointer?
    guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
        fail("sqlite-prepare-failed: \(String(cString: sqlite3_errmsg(db)))", 4)
    }
    defer { sqlite3_finalize(stmt) }

    for (offset, value) in bind.enumerated() {
        let index = Int32(offset + 1)
        switch value {
        case let text as String:
            sqlite3_bind_text(stmt, index, text, -1, SQLITE_TRANSIENT)
        case let number as Int:
            sqlite3_bind_int64(stmt, index, Int64(number))
        case let number as Double:
            sqlite3_bind_double(stmt, index, number)
        default:
            sqlite3_bind_null(stmt, index)
        }
    }

    var out: [[String: Any]] = []
    while sqlite3_step(stmt) == SQLITE_ROW {
        var row: [String: Any] = [:]
        for column in 0..<sqlite3_column_count(stmt) {
            let name = String(cString: sqlite3_column_name(stmt, column))
            switch sqlite3_column_type(stmt, column) {
            case SQLITE_INTEGER:
                row[name] = sqlite3_column_int64(stmt, column)
            case SQLITE_FLOAT:
                row[name] = sqlite3_column_double(stmt, column)
            case SQLITE_TEXT:
                row[name] = String(cString: sqlite3_column_text(stmt, column))
            case SQLITE_BLOB:
                if let bytes = sqlite3_column_blob(stmt, column) {
                    let count = Int(sqlite3_column_bytes(stmt, column))
                    row[name] = Data(bytes: bytes, count: count).base64EncodedString()
                }
            default:
                break
            }
        }
        out.append(row)
    }
    return out
}

func emit(_ rows: [[String: Any]]) {
    for row in rows {
        guard let data = try? JSONSerialization.data(withJSONObject: row) else { continue }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}

// ── arguments ────────────────────────────────────────────────────────────

var args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else { fail("usage: tccread <messages|calls> [--since ISO] [--limit N]", 3) }
args = Array(args.dropFirst())

var since = "1970-01-01T00:00:00Z"
var limit = 2000
var index = 0
while index < args.count - 1 {
    if args[index] == "--since" { since = args[index + 1] }
    if args[index] == "--limit" { limit = Int(args[index + 1]) ?? limit }
    index += 2
}

let home = FileManager.default.homeDirectoryForCurrentUser.path

// Apple's epochs. Messages stores nanoseconds since 2001-01-01; CallHistory
// stores seconds since the same instant. Both are converted in Python — this
// only needs to filter, so it converts the *bound* value, not the rows.
let appleEpoch = Date(timeIntervalSince1970: 978_307_200)
let formatter = ISO8601DateFormatter()
formatter.formatOptions = [.withInternetDateTime]
let sinceDate = formatter.date(from: since) ?? Date(timeIntervalSince1970: 0)
let sinceApple = sinceDate.timeIntervalSince(appleEpoch)

switch command {
case "messages":
    let db = openReadOnly("\(home)/Library/Messages/chat.db")
    emit(rows(db, """
        SELECT m.ROWID           AS external_id,
               h.id              AS handle,
               m.is_from_me      AS is_from_me,
               m.text            AS text,
               m.attributedBody  AS attributed_body,
               m.service         AS service,
               m.date            AS apple_date
          FROM message m
          LEFT JOIN handle h ON h.ROWID = m.handle_id
         WHERE m.date > ?
         ORDER BY m.date ASC
         LIMIT ?
        """, [sinceApple * 1_000_000_000, limit]))

case "calls":
    let db = openReadOnly("\(home)/Library/Application Support/CallHistoryDB/CallHistory.storedata")
    emit(rows(db, """
        SELECT Z_PK        AS external_id,
               ZADDRESS    AS handle,
               ZORIGINATED AS originated,
               ZANSWERED   AS answered,
               ZDURATION   AS duration,
               ZDATE       AS apple_date
          FROM ZCALLRECORD
         WHERE ZDATE > ?
         ORDER BY ZDATE ASC
         LIMIT ?
        """, [sinceApple, limit]))

default:
    fail("unknown command: \(command)", 3)
}
```

- [ ] **Step 2: Write the build script**

Create `helpers/tccread/build.sh`:

```bash
#!/usr/bin/env bash
# Build and sign tccread.
#
# The signature must be STABLE across rebuilds, or macOS treats each build as
# a new binary and the Full Disk Access grant silently stops applying — the
# grant is keyed on the code signature, not the path. Ad-hoc signing (`-`)
# regenerates the identity every time, so a real signing identity is required
# if you rebuild often. Set TCCREAD_IDENTITY to use one.
set -euo pipefail

cd "$(dirname "$0")"
swiftc -O -o tccread main.swift

IDENTITY="${TCCREAD_IDENTITY:--}"
codesign --force --sign "$IDENTITY" tccread

if [ "$IDENTITY" = "-" ]; then
  echo "WARNING: ad-hoc signed. Re-granting Full Disk Access will be needed" >&2
  echo "         after every rebuild. Set TCCREAD_IDENTITY to avoid that."   >&2
fi

echo "built: $(pwd)/tccread"
```

- [ ] **Step 3: Build it**

```bash
chmod +x helpers/tccread/build.sh
./helpers/tccread/build.sh
```

- [ ] **Step 4: Grant Full Disk Access — this is a manual step and cannot be scripted**

1. System Settings → Privacy & Security → Full Disk Access
2. `+`, then ⌘⇧G, then paste the absolute path printed by the build
3. Select `tccread`, enable the toggle

- [ ] **Step 5: Verify the grant**

```bash
./helpers/tccread/tccread messages --limit 1
```

Expected: one line of JSON.
Exit 2 with `tcc-denied` means the grant did not take — the usual cause is
granting the *folder* rather than the binary, or a rebuild after granting.

- [ ] **Step 6: Commit**

```bash
git add helpers/tccread/main.swift helpers/tccread/build.sh
echo "helpers/tccread/tccread" >> .gitignore
git add .gitignore
git commit -m "feat: tccread, a minimal Full Disk Access helper"
```

---

### Task 4: Import messages

**Files:**
- Create: `ingest/messages.py`
- Test: `tests/test_messages_ingest.py`

**Interfaces:**
- Consumes: `ingest.typedstream.decode`, `ingest.state` (`start`/`succeeded`/`failed`/`token`), the `tccread` binary.
- Produces: `messages.to_row(raw: dict) -> dict | None`, `messages.store(conn, row) -> None`, `messages.sync(limit: int = 2000) -> dict`, `messages.main() -> int`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_messages_ingest.py`:

```python
import base64

from ingest import messages


def test_apple_nanosecond_epoch_converts():
    """chat.db stores nanoseconds since 2001-01-01, not Unix seconds. Getting
    this wrong is off by 31 years and plausible enough to survive review."""
    # 2026-08-04T12:00:00Z == 807537600 seconds after the Apple epoch
    row = messages.to_row(
        {
            "external_id": 1,
            "handle": "+15551234",
            "is_from_me": 0,
            "text": "hello",
            "service": "iMessage",
            "apple_date": 807_537_600 * 1_000_000_000,
        }
    )
    assert row["sent_at"].startswith("2026-08-04T12:00:00")


def test_attributed_body_is_used_when_text_is_null():
    """The case that matters: modern rows leave `text` NULL."""
    blob = (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84"
        b"\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92"
        b"\x84\x84\x84\x08NSString\x01\x94\x84\x01\x2b\x05hello\x86"
    )
    row = messages.to_row(
        {
            "external_id": 2,
            "handle": "+15551234",
            "is_from_me": 0,
            "text": None,
            "attributed_body": base64.b64encode(blob).decode(),
            "apple_date": 807_537_600 * 1_000_000_000,
        }
    )
    assert row["body"] == "hello"


def test_row_with_no_recoverable_text_is_skipped():
    """An attachment-only message has neither. Skipped, not stored empty —
    an empty body is a row that pollutes search and answers nothing."""
    assert (
        messages.to_row(
            {
                "external_id": 3,
                "handle": "+1",
                "is_from_me": 0,
                "text": None,
                "attributed_body": None,
                "apple_date": 807_537_600 * 1_000_000_000,
            }
        )
        is None
    )


def test_store_is_idempotent(conn):
    row = {
        "external_id": "9",
        "handle": "+1",
        "direction": "in",
        "body": "twice",
        "service": "SMS",
        "sent_at": "2026-08-04T12:00:00+00:00",
    }
    messages.store(conn, row)
    messages.store(conn, row)
    conn.commit()
    count = conn.execute(
        "SELECT count(*) AS n FROM messages WHERE external_id = '9'"
    ).fetchone()["n"]
    assert count == 1


def test_missing_helper_is_not_fatal(conn, monkeypatch):
    """No binary, or no grant, marks the source stale and leaves every other
    ingester running. An importer that raises here takes the 15-minute tick
    down with it."""
    monkeypatch.setattr(messages, "HELPER", "/nonexistent/tccread")
    result = messages.sync()
    assert result["ok"] is False
    assert "tccread" in result["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_messages_ingest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ingest.messages'`

- [ ] **Step 3: Implement**

Create `ingest/messages.py`:

```python
"""Import texts from the Mac's own Messages database.

Read-only, one direction, and through `helpers/tccread` — the databases are
TCC-protected and the grant lives on that binary rather than on the Python
interpreter, so a deep job cannot inherit the ability to read your texts.

Synced writes bypass the mutations helper, as Calendar and Gmail do: the log
exists to make voice input reversible, and a few thousand imported texts would
bury the user's last real action and make /undo useless for exactly what it
was built for.
"""

import base64
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import timeutil
from app.config import REPO_ROOT
from app.db import transaction
from ingest import state, typedstream

SOURCE = "messages"
HELPER = str(REPO_ROOT / "helpers" / "tccread" / "tccread")

# Apple's epoch: 2001-01-01T00:00:00Z. chat.db counts nanoseconds from it,
# CallHistory counts seconds. Neither is Unix time, and mistaking one for the
# other is an error of 31 years that still produces a plausible-looking date.
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def apple_ns_to_iso(nanoseconds: int) -> str:
    return timeutil.to_utc_iso(_APPLE_EPOCH + timedelta(seconds=nanoseconds / 1_000_000_000))


def to_row(raw: dict) -> dict | None:
    """One helper row to one `messages` row, or None to skip it.

    Skipped rather than stored empty when there is no recoverable text: an
    attachment-only message has neither `text` nor a decodable body, and a
    blank row pollutes search while answering nothing.
    """
    body = (raw.get("text") or "").strip()
    if not body:
        encoded = raw.get("attributed_body")
        if encoded:
            body = (typedstream.decode(base64.b64decode(encoded)) or "").strip()
    if not body:
        return None

    return {
        "external_id": str(raw["external_id"]),
        "handle": raw.get("handle") or "unknown",
        "direction": "out" if raw.get("is_from_me") else "in",
        "body": body,
        "service": raw.get("service"),
        "sent_at": apple_ns_to_iso(int(raw["apple_date"])),
    }


def store(conn, row: dict) -> None:
    """Insert, or do nothing if this message is already here.

    Deliberately not through the mutations helper — see the module docstring.
    """
    conn.execute(
        """INSERT INTO messages (external_id, handle, direction, body, service, sent_at)
             VALUES (:external_id, :handle, :direction, :body, :service, :sent_at)
             ON CONFLICT(external_id) DO NOTHING""",
        row,
    )


def _run(command: str, since: str, limit: int) -> list[dict]:
    if not Path(HELPER).exists():
        raise FileNotFoundError(
            f"tccread not built at {HELPER} — run helpers/tccread/build.sh"
        )
    proc = subprocess.run(
        [HELPER, command, "--since", since, "--limit", str(limit)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode == 2:
        raise PermissionError(
            "tccread has no Full Disk Access — grant it in System Settings "
            "(Privacy & Security -> Full Disk Access) and re-run"
        )
    if proc.returncode != 0:
        raise RuntimeError(f"tccread exited {proc.returncode}: {proc.stderr.strip()}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def sync(limit: int = 2000) -> dict:
    """One pass. Never raises: a broken importer must not take down the tick
    that runs every other importer beside it."""
    with transaction() as conn:
        state.start(conn, SOURCE)
        cursor = state.token(conn, SOURCE) or "1970-01-01T00:00:00Z"

    try:
        raw_rows = _run("messages", cursor, limit)
    except Exception as exc:  # noqa: BLE001 — see docstring
        with transaction() as conn:
            state.failed(conn, SOURCE, f"tccread: {exc}")
        return {"ok": False, "stored": 0, "detail": f"tccread: {exc}"}

    stored = 0
    newest = cursor
    with transaction() as conn:
        for raw in raw_rows:
            row = to_row(raw)
            if row is None:
                continue
            store(conn, row)
            stored += 1
            newest = max(newest, row["sent_at"])
        state.succeeded(conn, SOURCE, newest, f"stored={stored} seen={len(raw_rows)}")

    return {"ok": True, "stored": stored, "detail": f"stored={stored}"}


def main() -> int:
    result = sync()
    print(result["detail"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_messages_ingest.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Run it against the real database**

```bash
uv run python -m ingest.messages
curl -s localhost:8000/health -H "Authorization: Bearer $JARVIS_TOKEN" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['ingest'])"
```

Expected: a `messages` source with `last_run_at` equal to `last_ok_at`. The
`/health` block reads `sync_state` generically, so it picks the new source up
with no change.

- [ ] **Step 6: Commit**

```bash
git add ingest/messages.py tests/test_messages_ingest.py
git commit -m "feat: import texts from chat.db"
```

---

### Task 5: Import calls

**Files:**
- Create: `ingest/calls.py`
- Test: `tests/test_calls_ingest.py`

**Interfaces:**
- Consumes: `ingest.state`, the `tccread` binary.
- Produces: `calls.to_row(raw) -> dict | None`, `calls.store(conn, row)`, `calls.sync(limit=2000) -> dict`, `calls.main() -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_calls_ingest.py`:

```python
"""Call history. ZDATE is Core Data epoch — seconds, not nanoseconds."""

import sqlite3

import pytest

from ingest import calls
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "calls.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_core_data_epoch_is_seconds_not_nanoseconds():
    """The classic off-by-31-years. CallHistory counts SECONDS from
    2001-01-01; chat.db counts nanoseconds. Same epoch, different units."""
    row = calls.to_row(
        {
            "external_id": 1,
            "handle": "+15551234",
            "originated": 0,
            "answered": 0,
            "duration": 0,
            "apple_date": 807_537_600,
        }
    )
    assert row["occurred_at"].startswith("2026-08-04T12:00:00")


def test_missed_call_is_recorded_as_unanswered():
    row = calls.to_row(
        {
            "external_id": 2,
            "handle": "+1",
            "originated": 0,
            "answered": 0,
            "duration": 0,
            "apple_date": 807_537_600,
        }
    )
    assert row["direction"] == "in"
    assert row["answered"] == 0


def test_outgoing_call_direction():
    row = calls.to_row(
        {
            "external_id": 3,
            "handle": "+1",
            "originated": 1,
            "answered": 1,
            "duration": 42,
            "apple_date": 807_537_600,
        }
    )
    assert row["direction"] == "out"
    assert row["duration_s"] == 42


def test_store_is_idempotent(conn):
    row = {
        "external_id": "7",
        "handle": "+1",
        "direction": "in",
        "answered": 0,
        "duration_s": 0,
        "occurred_at": "2026-08-04T12:00:00+00:00",
    }
    calls.store(conn, row)
    calls.store(conn, row)
    conn.commit()
    n = conn.execute("SELECT count(*) AS n FROM calls").fetchone()["n"]
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_calls_ingest.py -v`
Expected: FAIL — no module `ingest.calls`

- [ ] **Step 3: Implement**

Create `ingest/calls.py`, mirroring `ingest/messages.py`. The differences are the epoch unit and the row shape:

```python
"""Import call history from CallHistory.storedata.

Same posture as ingest.messages: read-only, through helpers/tccread, and
bypassing the mutations helper because a sync is not a user action.

The one trap: `ZCALLRECORD.ZDATE` is Core Data epoch measured in SECONDS,
while chat.db's `message.date` is the same epoch in NANOSECONDS. Treating one
as the other is an error of 31 years that still produces a date a reviewer
would nod at.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import timeutil
from app.config import REPO_ROOT
from app.db import transaction
from ingest import state

SOURCE = "calls"
HELPER = str(REPO_ROOT / "helpers" / "tccread" / "tccread")

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def apple_s_to_iso(seconds: float) -> str:
    return timeutil.to_utc_iso(_APPLE_EPOCH + timedelta(seconds=float(seconds)))


def to_row(raw: dict) -> dict | None:
    handle = (raw.get("handle") or "").strip()
    if not handle:
        return None
    return {
        "external_id": str(raw["external_id"]),
        "handle": handle,
        "direction": "out" if raw.get("originated") else "in",
        "answered": 1 if raw.get("answered") else 0,
        "duration_s": int(raw.get("duration") or 0),
        "occurred_at": apple_s_to_iso(raw["apple_date"]),
    }


def store(conn, row: dict) -> None:
    conn.execute(
        """INSERT INTO calls
             (external_id, handle, direction, answered, duration_s, occurred_at)
           VALUES
             (:external_id, :handle, :direction, :answered, :duration_s, :occurred_at)
           ON CONFLICT(external_id) DO NOTHING""",
        row,
    )
```

Then copy `_run`, `sync` and `main` from `ingest/messages.py`, changing the
subcommand to `"calls"`, `SOURCE` to `"calls"`, and the cursor field to
`occurred_at`. Do not import them from `messages` — the two importers
diverging later is more likely than them staying identical, and a shared
private helper across two modules is how that becomes painful.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_calls_ingest.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run it for real, then commit**

```bash
uv run python -m ingest.calls
git add ingest/calls.py tests/test_calls_ingest.py
git commit -m "feat: import call history"
```

---

### Task 6: Make them answerable

**Files:**
- Modify: `app/handlers.py`, `app/router.py` (the `query` tool's `kind` enum)
- Test: `tests/test_messages_context.py`

**Interfaces:**
- Consumes: `messages_fts`, `calls`.
- Produces: `handlers.search_messages(conn, question, limit=6) -> list[dict]`; `query` accepts `kind='message'` and `kind='call'`; `context_block` (Part 1) includes `TEXT:` lines; `today_block` gains a missed-call line.

**Depends on Part 1** for `context_block`. If Part 1 is not done, implement the
`query` kinds and skip the `context_block` step; the two are independent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_messages_context.py`:

```python
import sqlite3

import pytest

from app import handlers
from tests.helpers import apply_migrations


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ctx.db"
    apply_migrations(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _message(conn, body, handle="+15551234", sent_at="2026-08-04T12:00:00+00:00"):
    conn.execute(
        "INSERT INTO messages (external_id, handle, direction, body, sent_at)"
        " VALUES (?,?,?,?,?)",
        (body[:8] + sent_at, handle, "in", body, sent_at),
    )
    conn.commit()


def test_search_messages_finds_one(conn):
    _message(conn, "the landlord wrote back about the fence")
    hits = handlers.search_messages(conn, "what did the landlord say about the fence")
    assert hits
    assert "landlord" in hits[0]["body"]


def test_context_block_includes_texts(conn):
    _message(conn, "the landlord wrote back about the fence")
    block = handlers.context_block(conn, "did the landlord write back")
    assert "TEXT:" in block


def test_missed_calls_appear_in_today(conn):
    conn.execute(
        "INSERT INTO calls (external_id, handle, direction, answered, occurred_at)"
        " VALUES ('c1','+15551234','in',0,?)",
        (handlers.timeutil.to_utc_iso(handlers.timeutil.now("America/Denver")),),
    )
    conn.commit()
    block = handlers.today_block(conn, "America/Denver")
    assert "missed call" in block.lower()


def test_answered_call_is_not_a_missed_call(conn):
    conn.execute(
        "INSERT INTO calls (external_id, handle, direction, answered, occurred_at)"
        " VALUES ('c2','+15551234','in',1,?)",
        (handlers.timeutil.to_utc_iso(handlers.timeutil.now("America/Denver")),),
    )
    conn.commit()
    assert "missed call" not in handlers.today_block(conn, "America/Denver").lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_messages_context.py -v`
Expected: FAIL — no `handlers.search_messages`

- [ ] **Step 3: Implement**

In `app/handlers.py`, beside `search_email`:

```python
def search_messages(conn, question: str, limit: int = 6) -> list[dict]:
    """Search imported texts. Same two-stage shape as _search_notes.

    Messages are hard-deleted when they age out, so — unlike notes — the FTS
    index needs no join back to filter tombstones. The join is only here for
    the columns FTS does not store.
    """
    terms = [
        w for w in "".join(c if c.isalnum() else " " for c in question).split()
        if len(w) > 2
    ]
    if not terms:
        return []
    try:
        rows = conn.execute(
            """SELECT m.handle, m.body, m.sent_at, m.direction
                 FROM messages_fts f
                 JOIN messages m ON m.id = f.rowid
                WHERE messages_fts MATCH ?
                ORDER BY rank LIMIT ?""",
            (" OR ".join(terms), limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]
```

Add a `TEXT:` line to `context_block` (Part 1), after the email loop:

```python
    for text in search_messages(conn, text_query, limit):
        body = " ".join(str(text["body"]).split())
        who = text["handle"]
        lines.append(f"TEXT: {'to' if text['direction'] == 'out' else 'from'} {who} — {body}")
```

Rename `context_block`'s parameter from `text` to `text_query` first, or the
loop variable shadows it. Do this rename in one edit across the function.

Add a missed-call line to `_needs_doing` or `today_block`:

```python
    # A missed call is a fact about the day in the way an appointment is.
    # Answered calls are not — you already dealt with those.
    missed = conn.execute(
        """SELECT handle, occurred_at FROM calls
             WHERE direction = 'in' AND answered = 0 AND occurred_at >= ?
             ORDER BY occurred_at DESC LIMIT 5""",
        (start_of_day_iso,),
    ).fetchall()
    for call in missed:
        lines.append(f"MISSED CALL: from {call['handle']}")
```

Use whatever `today_block` already computes as the start of the local day; do
not compute a second one, or the two will disagree across a DST boundary.

In `app/router.py`, extend the `query` tool's `kind` enum with `"message"` and
`"call"`, and add one clause to its description:

```
A question about a text message or a phone call is kind='message' or
kind='call'.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_messages_context.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the routing regression suite**

Run:
```bash
uv run pytest tests/test_utterances.py tests/test_router_prompt.py -v
```
Expected: PASS. Two new `kind` values and a longer `TODAY` both grow the
prompt; `test_router_prompt.py` is what catches the static block losing
byte-stability.

- [ ] **Step 6: Commit**

```bash
git add app/handlers.py app/router.py tests/test_messages_context.py
git commit -m "feat: answer questions about texts and missed calls"
```

---

### Task 7: Schedule it, and write it down

**Files:**
- Modify: `deploy/` launchd plist for ingestion, `CLAUDE.md`

- [ ] **Step 1: Add both importers to the ingestion schedule**

Find the existing launchd job that runs Calendar and Gmail under `deploy/`
and add `ingest.messages` and `ingest.calls` beside them. Both are local
reads with no network and no API cost, so they can run on the same tick.

**LaunchAgent, not LaunchDaemon.** TCC grants are per-user, and a daemon
running outside the user session does not inherit the Full Disk Access you
granted in System Settings. If the existing ingestion job is a daemon, these
two need their own agent.

- [ ] **Step 2: Verify after a real tick**

```bash
curl -s localhost:8000/health -H "Authorization: Bearer $JARVIS_TOKEN" \
  | python3 -m json.tool | grep -A3 -E '"messages"|"calls"'
```

Expected: `last_run_at` equal to `last_ok_at` for both.

- [ ] **Step 3: Write it up in CLAUDE.md**

Add to the Ingestion section: that both sources are TCC-protected and reached
through `helpers/tccread`; that the grant is on the binary rather than the
interpreter and why; that the binary parses nothing so parsing bugs stay
unprivileged; that `attributedBody` carries the text and `message.text` is
usually NULL; that chat.db counts nanoseconds and CallHistory counts seconds
from the same 2001 epoch; and that these must run from a LaunchAgent because
TCC is per-user.

- [ ] **Step 4: Commit**

```bash
git add deploy/ CLAUDE.md
git commit -m "docs: record the TCC helper and the two Apple epochs"
```

---

## Self-review

Checked against the spec's Part 3:

- FDA on a purpose-built binary, not the interpreter — Task 3.
- Read-only, one direction — enforced in `main.swift` via `mode=ro` and
  `SQLITE_OPEN_READONLY`; no write path exists anywhere in this plan.
- `attributedBody` typedstream decoding — Task 2.
- Core Data epoch on `ZDATE` — Task 5, with the nanoseconds/seconds distinction
  called out in both importers because it is the same epoch in different units.
- Synced writes bypass the mutations helper — stated in both module docstrings
  and never contradicted; neither table joins `mutations.WRITABLE`.
- `messages`/`calls` schema with FTS — Task 1.
- Hard delete, so no join-and-filter — asserted by
  `test_hard_delete_leaves_the_index_clean`.
- No new router tool; `query` gains two kinds — Task 6.
- Missed calls in `today_block` — Task 6.
- Spec's three named tests all appear: `test_attributed_body_decodes` (as
  `test_decodes_real_blob` and `test_attributed_body_is_used_when_text_is_null`),
  `test_core_data_epoch` (as
  `test_core_data_epoch_is_seconds_not_nanoseconds`),
  `test_tccread_absent_is_not_fatal` (as `test_missing_helper_is_not_fatal`).

**One deviation from the spec, deliberate:** the spec put `person_id` matching
in scope. No task resolves a handle to a `people` row — the column exists and
stays NULL. Matching a phone number to a person needs a normalisation rule
(`+1555…` vs `(555) …`) that nothing in the repo has yet, and guessing it
wrong attributes a text to the wrong person, which is worse than an unnamed
number. Add it when `people` has phone numbers worth matching against.

## Next

**Go to [`2026-08-04-part4-order-tracking.md`](2026-08-04-part4-order-tracking.md).**

Part 4 relaxes the `format=metadata` rule for an explicit sender allowlist and
adds the `orders` table. Part 5 depends on it, so it comes before commerce.
