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
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import timeutil
from app.config import DB_PATH, REPO_ROOT
from app.db import transaction
from ingest import state, typedstream

SOURCE = "messages"
HELPER = str(REPO_ROOT / "helpers" / "tccread" / "tccread")
AGENT = "com.jarvis.tccread-messages"
SPOOL = DB_PATH.parent / "spool"
# Reading a hundred thousand rows out of a live chat.db is seconds, not
# minutes; this is a stuck-job ceiling, not a budget.
TIMEOUT_S = 180

# Apple's epoch: 2001-01-01T00:00:00Z. chat.db counts nanoseconds from it,
# CallHistory counts seconds. Neither is Unix time, and mistaking one for the
# other is an error of 31 years that still produces a plausible-looking date.
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def apple_ns_to_iso(nanoseconds: int) -> str:
    return timeutil.to_utc_iso(
        _APPLE_EPOCH + timedelta(seconds=nanoseconds / 1_000_000_000)
    )


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
    """Ask launchd to run tccread, then read what it spooled.

    Deliberately not a plain subprocess, and the reason is macOS TCC. A
    process's file access is attributed to the **responsible process**, which
    for a child is the top-level program of whatever launchd started. Spawned
    from here under the LaunchAgent, that is `.venv/bin/python` — which holds
    no Full Disk Access and must never be given any, because granting the
    interpreter grants it to every script it runs, including deep-path
    `claude -p` jobs. That is the whole reason tccread exists.

    Measured on this machine: the identical binary reads chat.db from a
    Terminal that holds the grant, and fails `tcc-denied` under the python
    agent. Started as its own launchd job it is its own responsible process,
    the grant on it is the one consulted, and it works.

    A launchd job's arguments are fixed in its plist, so the cursor goes
    through a file. tccread writes `<out>.done` last; that marker is what
    makes reading a half-written spool impossible.
    """
    if not Path(HELPER).exists():
        raise FileNotFoundError(
            f"tccread not built at {HELPER} — run helpers/tccread/build.sh"
        )

    SPOOL.mkdir(parents=True, exist_ok=True)
    args_file = SPOOL / f"{command}.args"
    out_file = SPOOL / f"{command}.ndjson"
    done_file = SPOOL / f"{command}.ndjson.done"
    err_file = SPOOL / f"{command}.ndjson.err"

    # Clear the previous run's leavings first: a stale .done would be read as
    # this run finishing instantly, with the previous run's rows behind it.
    for stale in (out_file, done_file, err_file):
        stale.unlink(missing_ok=True)
    args_file.write_text(f"{since}\n{limit}\n")

    proc = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{AGENT}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"tccread agent {AGENT} would not start — run "
            f"deploy/install-agents.sh ({proc.stderr.strip()})"
        )

    deadline = time.monotonic() + TIMEOUT_S
    while not done_file.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"tccread agent {AGENT} did not finish within {TIMEOUT_S}s"
            )
        time.sleep(0.05)

    code = done_file.read_text().strip()
    if code == "2":
        raise PermissionError(
            "tccread has no Full Disk Access — grant it in System Settings "
            "(Privacy & Security -> Full Disk Access) and re-run. A rebuild "
            "of an ad-hoc signed tccread also invalidates an existing grant."
        )
    if code != "0":
        detail = err_file.read_text().strip() if err_file.exists() else ""
        raise RuntimeError(f"tccread exited {code}: {detail}")

    return [
        json.loads(line)
        for line in out_file.read_text().splitlines()
        if line.strip()
    ]


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
