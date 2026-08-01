"""Receipt photos on disk.

They live beside the database, outside the repo, for the same reason the
database does: they survive a re-clone and are never committed.

Content-addressed by sha256. That is not an optimization — it is what makes
`receipts.image_sha256` able to say "you already uploaded this", which in turn
is what stops a receipt you discarded from coming back the next time you tap
the wrong thumbnail.

A photo's only purpose is re-reading a bad extraction, so it is pruned 30 days
after the receipt is confirmed. An *unconfirmed* receipt keeps its photo
indefinitely: you have not reviewed it yet, and reviewing it without the image
is guesswork.
"""

import hashlib
import sqlite3
from pathlib import Path

from app import config

# What the upload endpoint accepts, and the extension each gets on disk.
# HEIC is absent deliberately: the Anthropic vision API does not accept it, so
# iOS converts before upload rather than the server converting after.
MEDIA_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

PRUNE_AFTER_DAYS = 30


def store(image_bytes: bytes, media_type: str) -> tuple[str, str]:
    """Write the photo and return (sha256_hex, path).

    Idempotent: the same bytes always land on the same path. Re-uploading
    rewrites identical content rather than creating a second file.
    """
    suffix = MEDIA_TYPES.get(media_type)
    if suffix is None:
        raise ValueError(f"unsupported image type: {media_type!r}")

    digest = hashlib.sha256(image_bytes).hexdigest()
    config.RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RECEIPT_DIR / f"{digest}{suffix}"
    path.write_bytes(image_bytes)
    return digest, str(path)


def prune(conn: sqlite3.Connection, days: int = PRUNE_AFTER_DAYS) -> int:
    """Delete photos for receipts confirmed more than `days` ago.

    The `receipts` row stays — `image_sha256` still has to block a re-upload.
    Only `image_path` is cleared, so the review screen can tell "the photo has
    aged out" from "there never was one".
    """
    stale = conn.execute(
        """SELECT id, image_path FROM receipts
             WHERE status = 'confirmed'
               AND image_path IS NOT NULL
               AND confirmed_at < strftime('%Y-%m-%dT%H:%M:%SZ','now',?)""",
        (f"-{int(days)} days",),
    ).fetchall()

    for row in stale:
        try:
            Path(row["image_path"]).unlink(missing_ok=True)
        except OSError:
            # The row and the disk can disagree — a manual cleanup, a restore
            # that skipped the images. Clearing the path is still right, and
            # one unreadable file must not abort the batch.
            pass
        conn.execute("UPDATE receipts SET image_path = NULL WHERE id = ?", (row["id"],))

    return len(stale)
