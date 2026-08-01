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


@pytest.fixture
def client(db, monkeypatch):
    """A TestClient with extraction stubbed to a fixed two-item receipt.

    Real extraction is covered in tests/test_receipt_extract.py. Here the
    model is a fixture so these tests are about the state machine.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from pantry import receipts

    monkeypatch.setattr(
        receipts.extract,
        "read_receipt",
        lambda image_bytes, media_type, today: {
            "store": "King Soopers",
            "purchased_on": "2026-07-31",
            "total_cents": 4213,
            "items": [
                {
                    "raw_text": "GV WHL MLK 1GAL",
                    "name": "whole milk",
                    "category": "milk",
                    "quantity": 1.0,
                    "unit": "gal",
                    "location": "fridge",
                },
                {
                    "raw_text": "DRY PASTA 16OZ",
                    "name": "dried pasta",
                    "category": "pasta_dry",
                    "quantity": 1.0,
                    "unit": "oz",
                    "location": "pantry",
                },
            ],
        },
    )

    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {SHARED}"
    return c


def upload(client, payload=PNG, content_type="image/png"):
    return client.post(
        "/receipts", files={"image": ("receipt.png", payload, content_type)}
    )


def test_upload_returns_a_receipt_id_immediately(client):
    response = upload(client)
    assert response.status_code == 200
    assert response.json()["receipt_id"] > 0


def test_extraction_fills_items_and_moves_the_receipt_to_pending(client, db):
    receipt_id = upload(client).json()["receipt_id"]
    detail = client.get(f"/receipts/{receipt_id}").json()

    assert detail["status"] == "pending"
    assert detail["store"] == "King Soopers"
    assert [i["name"] for i in detail["items"]] == ["whole milk", "dried pasta"]


def test_dates_are_prefilled_from_the_shelf_life_table(client):
    receipt_id = upload(client).json()["receipt_id"]
    items = {i["name"]: i for i in client.get(f"/receipts/{receipt_id}").json()["items"]}

    assert items["whole milk"]["expires_on"] == "2026-08-07"
    assert items["whole milk"]["expiry_source"] == "default"
    assert items["dried pasta"]["expires_on"] is None, "shelf-stable has no date"


def test_perishables_sort_to_the_top_of_the_review_screen(client):
    """You are going to skim this list. What needs a decision goes first;
    dated items ascending, undated last."""
    receipt_id = upload(client).json()["receipt_id"]
    names = [i["name"] for i in client.get(f"/receipts/{receipt_id}").json()["items"]]
    assert names == ["whole milk", "dried pasta"]


def test_nothing_reaches_the_inventory_before_confirmation(client, db):
    upload(client)
    active = rows(db, "SELECT * FROM pantry_items WHERE status = 'active'")
    assert active == [], "the review screen is the only path into the fridge"


def test_confirm_activates_every_item(client, db):
    receipt_id = upload(client).json()["receipt_id"]
    response = client.post(f"/receipts/{receipt_id}/confirm")

    assert response.status_code == 200
    assert response.json()["items"] == 2
    assert len(rows(db, "SELECT * FROM pantry_items WHERE status = 'active'")) == 2
    assert rows(db, "SELECT status FROM receipts")[0]["status"] == "confirmed"


def test_confirming_logs_exactly_one_mutation(client, db):
    """Thirty mutation rows per shopping trip would bury the user's last real
    action and make /undo useless for what it was built for."""
    receipt_id = upload(client).json()["receipt_id"]
    client.post(f"/receipts/{receipt_id}/confirm")

    logged = rows(db, "SELECT table_name, op FROM mutations")
    assert logged == [{"table_name": "receipts", "op": "insert"}]


def test_undo_after_confirm_reverses_the_whole_trip(client, db):
    receipt_id = upload(client).json()["receipt_id"]
    client.post(f"/receipts/{receipt_id}/confirm")

    assert client.post("/undo").status_code == 200

    assert rows(db, "SELECT * FROM pantry_items") == []
    assert rows(db, "SELECT * FROM receipts") == []


def test_editing_a_date_marks_it_as_yours(client, db):
    """expiry_source is what lets the fridge list show which dates you stand
    behind and which the table guessed."""
    receipt_id = upload(client).json()["receipt_id"]
    item_id = client.get(f"/receipts/{receipt_id}").json()["items"][0]["id"]

    response = client.patch(
        f"/receipts/{receipt_id}/items",
        json={"items": [{"id": item_id, "expires_on": "2026-08-03"}]},
    )
    assert response.status_code == 200

    edited = client.get(f"/receipts/{receipt_id}").json()["items"][0]
    assert edited["expires_on"] == "2026-08-03"
    assert edited["expiry_source"] == "user"


def test_editing_a_name_leaves_raw_text_alone(client):
    """raw_text is the receipt's own words and is never overwritten — it is
    what you check against when a name looks wrong months later."""
    receipt_id = upload(client).json()["receipt_id"]
    item_id = client.get(f"/receipts/{receipt_id}").json()["items"][0]["id"]

    client.patch(
        f"/receipts/{receipt_id}/items",
        json={"items": [{"id": item_id, "name": "2% milk"}]},
    )
    edited = client.get(f"/receipts/{receipt_id}").json()["items"][0]
    assert edited["name"] == "2% milk"
    assert edited["raw_text"] == "GV WHL MLK 1GAL"


def test_deleting_a_misread_line_drops_it(client, db):
    receipt_id = upload(client).json()["receipt_id"]
    item_id = client.get(f"/receipts/{receipt_id}").json()["items"][0]["id"]

    client.patch(
        f"/receipts/{receipt_id}/items",
        json={"items": [{"id": item_id, "delete": True}]},
    )
    client.post(f"/receipts/{receipt_id}/confirm")

    names = [r["name"] for r in rows(db, "SELECT name FROM pantry_items")]
    assert names == ["dried pasta"]


def test_a_confirmed_receipt_is_no_longer_editable(client):
    """Past confirmation the items are real inventory and are edited as
    inventory. A confirmed receipt is a record, not a live document."""
    receipt_id = upload(client).json()["receipt_id"]
    client.post(f"/receipts/{receipt_id}/confirm")

    assert client.patch(
        f"/receipts/{receipt_id}/items", json={"items": []}
    ).status_code == 409
    assert client.post(f"/receipts/{receipt_id}/confirm").status_code == 409
    assert client.post(f"/receipts/{receipt_id}/discard").status_code == 409


def test_discard_keeps_the_row_so_the_photo_stays_rejected(client, db):
    receipt_id = upload(client).json()["receipt_id"]
    assert client.post(f"/receipts/{receipt_id}/discard").status_code == 200

    assert rows(db, "SELECT status FROM receipts")[0]["status"] == "discarded"
    assert rows(db, "SELECT * FROM pantry_items WHERE status = 'pending'") == []

    again = upload(client)
    assert again.json()["receipt_id"] == receipt_id
    assert again.json()["status"] == "discarded", "a rejected receipt stays rejected"


def test_re_uploading_the_same_photo_returns_the_same_receipt(client, db):
    first = upload(client).json()["receipt_id"]
    second = upload(client).json()["receipt_id"]
    assert first == second
    assert len(rows(db, "SELECT * FROM receipts")) == 1
    assert len(rows(db, "SELECT * FROM pantry_items")) == 2, "not extracted twice"


def test_a_failed_extraction_is_visible_not_silent(client, db, monkeypatch):
    """The worst outcome here is a receipt that looks like it had nothing on
    it. An error the review screen can show is strictly better."""
    from pantry import receipts

    def boom(*args, **kwargs):
        raise RuntimeError("the model is down")

    monkeypatch.setattr(receipts.extract, "read_receipt", boom)

    receipt_id = upload(client).json()["receipt_id"]
    detail = client.get(f"/receipts/{receipt_id}").json()

    assert detail["status"] == "pending"
    assert "the model is down" in detail["extract_error"]
    assert detail["items"] == []


def test_an_unsupported_upload_is_rejected(client):
    response = client.post(
        "/receipts", files={"image": ("scan.pdf", b"%PDF-", "application/pdf")}
    )
    assert response.status_code == 415


def test_the_endpoints_require_a_token(db):
    from fastapi.testclient import TestClient

    from app.main import app

    anonymous = TestClient(app)
    assert anonymous.post("/receipts", files={"image": ("r.png", PNG, "image/png")}).status_code == 401
    assert anonymous.get("/receipts/1").status_code == 401
