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
