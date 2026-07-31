"""Device registration and per-device bearer tokens — offline.

The property under test throughout: a lost phone costs one DELETE, not a
re-key of every client, and the shared token keeps working the whole time so
the Shortcut doesn't break mid-migration.
"""

import sqlite3

import pytest

from tests.helpers import apply_migrations

SHARED = "shared-token-for-tests"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "devices.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    monkeypatch.setenv("JARVIS_TOKEN", SHARED)
    return path


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def shared() -> dict:
    return {"Authorization": f"Bearer {SHARED}"}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def enroll(client, apns_token: str = "aa" * 32, label: str = "iPhone") -> str:
    response = client.post(
        "/devices",
        json={"label": label, "apns_token": apns_token},
        headers=shared(),
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


# ── auth ──────────────────────────────────────────────────


def test_shared_token_still_works(client):
    """The migration's whole premise: adding per-device tokens must not break
    the client that exists today."""
    assert client.get("/agenda", headers=shared()).status_code == 200


def test_missing_credentials_are_rejected(client):
    assert client.get("/agenda").status_code == 401


def test_a_non_bearer_scheme_is_rejected(client):
    assert client.get("/agenda", headers={"Authorization": f"Basic {SHARED}"}).status_code == 401


def test_wrong_token_is_rejected(client):
    assert client.get("/agenda", headers=bearer("not-the-token")).status_code == 401


# ── registration ──────────────────────────────────────────


def test_registration_mints_a_working_device_token(client):
    token = enroll(client)
    assert token and token != SHARED
    assert client.get("/agenda", headers=bearer(token)).status_code == 200


def test_the_device_token_is_never_stored(client, db):
    """A stolen copy of jarvis.db must not be a stolen credential."""
    token = enroll(client)
    conn = sqlite3.connect(db)
    try:
        stored = conn.execute("SELECT token_hash FROM devices").fetchone()[0]
    finally:
        conn.close()
    assert stored != token
    assert token not in stored


def test_refresh_updates_in_place_without_a_new_credential(client, db):
    """The every-launch path: iOS hands the app a device token on each start,
    and it is occasionally a different one."""
    token = enroll(client, apns_token="aa" * 32)
    response = client.post(
        "/devices",
        json={"label": "iPhone", "apns_token": "bb" * 32},
        headers=bearer(token),
    )
    assert response.status_code == 200
    assert response.json()["token"] is None, "refresh must not rotate the credential"

    conn = sqlite3.connect(db)
    try:
        live = conn.execute(
            "SELECT apns_token FROM devices WHERE revoked_at IS NULL"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in live] == ["bb" * 32]
    assert client.get("/agenda", headers=bearer(token)).status_code == 200


def test_reenrolling_the_same_hardware_retires_the_old_row(client):
    """Reinstalling the app must not leave the previous install's token valid —
    on a restore-from-backup that Keychain entry can live on another device."""
    first = enroll(client, apns_token="cc" * 32)
    second = enroll(client, apns_token="cc" * 32)
    assert first != second
    assert client.get("/agenda", headers=bearer(first)).status_code == 401
    assert client.get("/agenda", headers=bearer(second)).status_code == 200


def test_a_device_with_no_push_permission_can_still_register(client):
    """apns_token is NULL until the user grants notifications. The bearer token
    is what /say needs, and that must not wait on a permission prompt."""
    response = client.post("/devices", json={"label": "iPad"}, headers=shared())
    assert response.status_code == 200
    assert client.get("/agenda", headers=bearer(response.json()["token"])).status_code == 200


def test_a_malformed_apns_token_is_rejected(client):
    response = client.post(
        "/devices", json={"label": "iPhone", "apns_token": "not-hex!"}, headers=shared()
    )
    assert response.status_code == 422


# ── revocation ────────────────────────────────────────────


def test_revoking_locks_out_only_that_device(client):
    lost = enroll(client, apns_token="dd" * 32, label="lost phone")
    kept = enroll(client, apns_token="ee" * 32, label="laptop")

    device_id = client.get("/devices", headers=shared()).json()["devices"][0]["id"]
    assert client.delete(f"/devices/{device_id}", headers=shared()).json()["revoked"]

    assert client.get("/agenda", headers=bearer(lost)).status_code == 401
    assert client.get("/agenda", headers=bearer(kept)).status_code == 200
    assert client.get("/agenda", headers=shared()).status_code == 200


def test_revoking_twice_is_not_an_error(client):
    enroll(client)
    device_id = client.get("/devices", headers=shared()).json()["devices"][0]["id"]
    assert client.delete(f"/devices/{device_id}", headers=shared()).json()["revoked"] is True
    assert client.delete(f"/devices/{device_id}", headers=shared()).json()["revoked"] is False


def test_a_revoked_device_cannot_refresh_itself_back(client):
    token = enroll(client)
    device_id = client.get("/devices", headers=shared()).json()["devices"][0]["id"]
    client.delete(f"/devices/{device_id}", headers=shared())
    assert client.post("/devices", json={"label": "x"}, headers=bearer(token)).status_code == 401


# ── listing ───────────────────────────────────────────────


def test_listing_devices_never_exposes_the_hash(client):
    enroll(client)
    body = client.get("/devices", headers=shared()).json()
    assert body["devices"]
    for device in body["devices"]:
        assert "token_hash" not in device
        assert "apns_token" not in device
        assert device["has_push"] == 1


def test_last_seen_is_stamped_on_use(client, db):
    token = enroll(client)
    client.get("/agenda", headers=bearer(token))
    conn = sqlite3.connect(db)
    try:
        seen = conn.execute("SELECT last_seen_at FROM devices").fetchone()[0]
    finally:
        conn.close()
    assert seen is not None
