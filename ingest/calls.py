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
