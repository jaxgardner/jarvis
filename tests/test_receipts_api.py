"""Receipt upload, review, confirm, undo.

The path from a photograph to real inventory, and the human gate in the
middle of it.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"

# A one-pixel PNG. Small enough to inline, real enough that content-type
# sniffing and hashing behave like they will in production.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "receipts.db"
    apply_migrations(path)

    import app.config as appconfig
    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setattr(appconfig, "RECEIPT_DIR", tmp_path / "receipts")
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


def rows(db, sql: str, args: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_the_same_bytes_hash_to_the_same_path(db, tmp_path, monkeypatch):
    from pantry import images

    first_hash, first_path = images.store(PNG, "image/png")
    second_hash, second_path = images.store(PNG, "image/png")

    assert first_hash == second_hash
    assert first_path == second_path


def test_stored_bytes_round_trip(db, tmp_path):
    from pathlib import Path

    from pantry import images

    _, path = images.store(PNG, "image/png")
    assert Path(path).read_bytes() == PNG


def test_the_extension_follows_the_media_type(db, tmp_path):
    from pantry import images

    _, jpeg_path = images.store(b"jpegbytes", "image/jpeg")
    _, png_path = images.store(PNG, "image/png")
    assert jpeg_path.endswith(".jpg")
    assert png_path.endswith(".png")


def test_an_unsupported_media_type_is_rejected(db):
    from pantry import images

    with pytest.raises(ValueError):
        images.store(b"%PDF-", "application/pdf")


def test_prune_removes_images_for_old_confirmed_receipts(db, tmp_path):
    from pathlib import Path

    from app.db import transaction
    from pantry import images

    sha, path = images.store(PNG, "image/png")
    with transaction() as conn:
        conn.execute(
            """INSERT INTO receipts (image_sha256, image_path, status, confirmed_at)
                 VALUES (?,?,'confirmed', strftime('%Y-%m-%dT%H:%M:%SZ','now','-40 days'))""",
            (sha, path),
        )

    with transaction() as conn:
        removed = images.prune(conn)

    assert removed == 1
    assert not Path(path).exists()
    assert rows(db, "SELECT image_path FROM receipts")[0]["image_path"] is None


def test_prune_keeps_recent_and_unconfirmed_receipts(db, tmp_path):
    from pathlib import Path

    from app.db import transaction
    from pantry import images

    recent_sha, recent_path = images.store(b"recent", "image/jpeg")
    pending_sha, pending_path = images.store(b"pending", "image/jpeg")
    with transaction() as conn:
        conn.execute(
            """INSERT INTO receipts (image_sha256, image_path, status, confirmed_at)
                 VALUES (?,?,'confirmed', strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
            (recent_sha, recent_path),
        )
        conn.execute(
            """INSERT INTO receipts (image_sha256, image_path, status)
                 VALUES (?,?,'pending')""",
            (pending_sha, pending_path),
        )

    with transaction() as conn:
        removed = images.prune(conn)

    assert removed == 0
    assert Path(recent_path).exists()
    assert Path(pending_path).exists(), "an unreviewed receipt still needs its photo"


def test_prune_survives_a_file_already_gone(db, tmp_path):
    """The row and the disk can disagree — a manual cleanup, a restore from a
    backup that skipped the images. Pruning must not abort the batch on it."""
    from pathlib import Path

    from app.db import transaction
    from pantry import images

    sha, path = images.store(PNG, "image/png")
    Path(path).unlink()
    with transaction() as conn:
        conn.execute(
            """INSERT INTO receipts (image_sha256, image_path, status, confirmed_at)
                 VALUES (?,?,'confirmed', strftime('%Y-%m-%dT%H:%M:%SZ','now','-40 days'))""",
            (sha, path),
        )

    with transaction() as conn:
        removed = images.prune(conn)

    assert removed == 1
    assert rows(db, "SELECT image_path FROM receipts")[0]["image_path"] is None
