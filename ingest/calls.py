"""Import call history from CallHistory.storedata.

Same posture as ingest.messages: read-only, through helpers/tccread, and
bypassing the mutations helper because a sync is not a user action.

The one trap: `ZCALLRECORD.ZDATE` is Core Data epoch measured in SECONDS,
while chat.db's `message.date` is the same epoch in NANOSECONDS. Treating one
as the other is an error of 31 years that still produces a date a reviewer
would nod at.

`_run`, `sync` and `main` are deliberately copied from ingest.messages rather
than shared. The two importers diverging later is more likely than them
staying identical, and a private helper spanning both modules is how that
becomes painful.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import timeutil
from app.config import DB_PATH, REPO_ROOT
from app.db import transaction
from ingest import state

SOURCE = "calls"
HELPER = str(REPO_ROOT / "helpers" / "tccread" / "tccread")
AGENT = "com.jarvis.tccread-calls"
SPOOL = DB_PATH.parent / "spool"
TIMEOUT_S = 180

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


def _run(command: str, since: str, limit: int) -> list[dict]:
    """Ask launchd to run tccread, then read what it spooled.

    See `ingest.messages._run` for why this is not a plain subprocess: TCC
    attributes a child's file access to the responsible process, which under
    the LaunchAgent is python, which is denied and must stay denied.
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
        raw_rows = _run("calls", cursor, limit)
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
            newest = max(newest, row["occurred_at"])
        state.succeeded(conn, SOURCE, newest, f"stored={stored} seen={len(raw_rows)}")

    return {"ok": True, "stored": stored, "detail": f"stored={stored}"}


def main() -> int:
    result = sync()
    print(result["detail"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
