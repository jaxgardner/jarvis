"""The path from a photograph to real inventory.

A receipt moves through four states:

    extracting -> pending -> confirmed
                          -> discarded

`extracting` exists because vision on a receipt takes 3-6 seconds, far outside
the 2s fast-path budget. The upload returns immediately and the review screen
polls, exactly as `/jobs/{id}` does for the deep path.

`pending` is the human gate, and it is the whole point. Nothing here reaches
`status='active'` — the state `query` and the expiry sweep read — without
someone looking at it. Same posture as `proposals`, for the same reason: one
invented expiry teaches you to distrust the inventory, and an inventory you
don't trust is decoration.
"""

import sqlite3

from app import mutations, timeutil
from pantry import extract, shelflife


def create(conn: sqlite3.Connection, sha: str, path: str) -> int:
    """Insert an `extracting` receipt. Written directly, not through the
    mutations helper: an upload is not yet a user decision, and there is
    nothing to regret until it is confirmed."""
    return int(
        conn.execute(
            "INSERT INTO receipts (image_sha256, image_path) VALUES (?,?)",
            (sha, path),
        ).lastrowid
    )


def fill(receipt_id: int, image_bytes: bytes, media_type: str, today: str) -> None:
    """Extract, write items, move to `pending`. Never raises.

    Runs in a background task, where an exception would vanish into the server
    log and leave the receipt stuck in `extracting` forever. Instead every
    failure lands in `extract_error`, which the review screen shows with a
    Retry button. A receipt that looks like it had nothing on it is the worst
    available outcome; an error you can see is strictly better.

    NOTE: the tokens spent here are not counted by /metrics. That block is
    per-utterance and a receipt has no utterance behind it. Deliberate for now
    — one call per shopping trip is not what a spend report is for.
    """
    from app.db import transaction

    try:
        read = extract.read_receipt(image_bytes, media_type, today)
    except Exception as exc:  # noqa: BLE001 — see docstring
        with transaction() as conn:
            conn.execute(
                "UPDATE receipts SET status='pending', extract_error=? WHERE id=?",
                (str(exc), receipt_id),
            )
        return

    with transaction() as conn:
        conn.execute(
            """UPDATE receipts
                 SET store=?, purchased_on=?, total_cents=?, status='pending',
                     extract_error=NULL
               WHERE id=?""",
            (read["store"], read["purchased_on"], read["total_cents"], receipt_id),
        )
        for item in read["items"]:
            # The model never sets this. shelflife.py proposes, the human
            # confirms — which is what makes a wrong date one table edit
            # rather than a prompt change.
            expires_on = shelflife.expires_on(
                item["category"], item["location"], read["purchased_on"]
            )
            conn.execute(
                """INSERT INTO pantry_items
                     (receipt_id, raw_text, name, category, quantity, unit,
                      location, expires_on, expiry_source, status)
                   VALUES (?,?,?,?,?,?,?,?,?,'pending')""",
                (
                    receipt_id,
                    item["raw_text"],
                    item["name"],
                    item["category"],
                    item["quantity"],
                    item["unit"],
                    item["location"],
                    expires_on,
                    "default" if expires_on else None,
                ),
            )


def detail(conn: sqlite3.Connection, receipt_id: int) -> dict | None:
    """The receipt and its items, ordered for the review screen.

    Perishables first — dated items ascending, undated last. You are going to
    skim this list, so what needs a decision goes where you will see it.
    """
    row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if row is None:
        return None
    items = conn.execute(
        """SELECT * FROM pantry_items
             WHERE receipt_id = ? AND status IN ('pending','active')
             ORDER BY expires_on IS NULL, expires_on, id""",
        (receipt_id,),
    ).fetchall()
    return {**dict(row), "items": [dict(item) for item in items]}


# What the review screen may change. `raw_text` is absent on purpose: it is
# the receipt's own words, and it is what you check a wrong-looking name
# against months later.
EDITABLE = {"name", "category", "quantity", "unit", "location", "expires_on"}


def patch_items(conn: sqlite3.Connection, receipt_id: int, edits: list[dict]) -> int:
    """Apply review-screen edits. Returns the number of rows touched."""
    touched = 0
    for edit in edits:
        item_id = edit.get("id")
        if item_id is None:
            continue
        owned = conn.execute(
            "SELECT id FROM pantry_items WHERE id = ? AND receipt_id = ?",
            (item_id, receipt_id),
        ).fetchone()
        if owned is None:
            continue

        if edit.get("delete"):
            conn.execute("DELETE FROM pantry_items WHERE id = ?", (item_id,))
            touched += 1
            continue

        values = {key: edit[key] for key in EDITABLE if key in edit}
        if not values:
            continue
        if "expires_on" in values:
            # Touching the date makes it yours. The fridge list uses this to
            # show which dates you stand behind and which the table guessed.
            values["expiry_source"] = "user"
        assignments = ", ".join(f"{col} = ?" for col in values)
        conn.execute(
            f"UPDATE pantry_items SET {assignments} WHERE id = ?",  # noqa: S608
            (*values.values(), item_id),
        )
        touched += 1
    return touched


def confirm(conn: sqlite3.Connection, receipt_id: int) -> int | None:
    """Items go active, receipt goes confirmed. Returns the item count.

    None means the receipt was not `pending` — already confirmed, discarded,
    or still extracting. The caller turns that into a 409.

    **One mutation, on the receipt row.** The item UPDATEs deliberately bypass
    the mutations helper. Thirty log rows per shopping trip would bury the
    user's last real action and make /undo useless for exactly what it was
    built for — the same reasoning CLAUDE.md already applies to synced writes.
    `pantry_items.receipt_id` is ON DELETE CASCADE, so reversing this single
    insert takes the whole trip with it, which is what "I photographed the
    wrong receipt" means.
    """
    row = conn.execute(
        "SELECT status FROM receipts WHERE id = ?", (receipt_id,)
    ).fetchone()
    if row is None or row["status"] != "pending":
        return None

    count = conn.execute(
        "UPDATE pantry_items SET status='active' WHERE receipt_id=? AND status='pending'",
        (receipt_id,),
    ).rowcount

    conn.execute(
        "UPDATE receipts SET status='confirmed', confirmed_at=? WHERE id=?",
        (timeutil.to_utc_iso(timeutil.now("UTC")), receipt_id),
    )
    # Logged as an *insert*, not an update, even though the row already
    # exists. Reversing an update would flip the status back to 'pending' and
    # leave every item active — half an undo. Reversing an insert deletes the
    # receipt, and CASCADE takes the trip with it, which is what "I
    # photographed the wrong receipt" means. The row only became real to the
    # user at this moment; before confirmation it was a draft.
    mutations.log_insert(conn, None, "receipts", receipt_id)
    return count


def discard(conn: sqlite3.Connection, receipt_id: int) -> bool:
    """Throw away an unreviewed receipt.

    The row survives so `image_sha256` keeps blocking a re-upload — a receipt
    you rejected must not come back the next time you tap the wrong thumbnail.
    """
    row = conn.execute(
        "SELECT status FROM receipts WHERE id = ?", (receipt_id,)
    ).fetchone()
    if row is None or row["status"] != "pending":
        return False
    conn.execute(
        "DELETE FROM pantry_items WHERE receipt_id=? AND status='pending'", (receipt_id,)
    )
    conn.execute("UPDATE receipts SET status='discarded' WHERE id=?", (receipt_id,))
    return True
