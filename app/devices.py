"""Registered devices and per-device bearer tokens.

Auth model, in one paragraph: `JARVIS_TOKEN` is the enrollment credential —
it can register a device and nothing else is gated on it, but it keeps working
everywhere so the Shortcut doesn't break mid-migration. A registered device
gets its own token, which is what the app uses from then on. Revoking one
device is an UPDATE; before this, it was a re-key of every client.

Device rows are operational, not domain data, so they are written directly
rather than through `mutations` — /undo restoring a revoked phone's access
would be an actively bad outcome.
"""

import hashlib
import secrets
import sqlite3

TOKEN_BYTES = 32  # 256 bits, urlsafe-base64'd to 43 characters


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """sha256, not bcrypt, deliberately.

    Password hashes are slow to survive being guessed from a low-entropy
    secret. This is 256 bits of CSPRNG output — there is nothing to guess, and
    a slow hash on the auth path would cost latency against a 2s budget on
    every single request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    from app import timeutil

    return timeutil.to_utc_iso(timeutil.now("UTC"))


# ── lookup ────────────────────────────────────────────────


def authenticate(conn: sqlite3.Connection, token: str) -> dict | None:
    """Resolve a presented bearer token to a live device, or None.

    Looked up by hash, so the comparison is against a value the database
    already indexed — no scan, and no need for compare_digest here: the
    attacker never sees a partial match to time against, only found/not-found
    on a full 256-bit preimage.
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
        (hash_token(token),),
    ).fetchone()
    return dict(row) if row else None


def touch(conn: sqlite3.Connection, device_id: int) -> None:
    """Stamp last_seen_at. Best-effort — this is diagnostics, not accounting."""
    conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (_now(), device_id))


def live(conn: sqlite3.Connection) -> list[dict]:
    """Every registered device. Never includes token_hash — callers of this
    serialize straight to JSON."""
    rows = conn.execute(
        """SELECT id, label, platform, apns_env, created_at, last_seen_at, revoked_at,
                  (apns_token IS NOT NULL) AS has_push
             FROM devices ORDER BY id"""
    ).fetchall()
    return [dict(r) for r in rows]


def push_targets(conn: sqlite3.Connection) -> list[dict]:
    """Devices that can actually receive a push, newest first."""
    rows = conn.execute(
        """SELECT id, apns_token, apns_env FROM devices
             WHERE revoked_at IS NULL AND apns_token IS NOT NULL
             ORDER BY id DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


# ── writes ────────────────────────────────────────────────


def register(
    conn: sqlite3.Connection,
    *,
    label: str,
    platform: str = "ios",
    apns_token: str | None = None,
    apns_env: str = "prod",
) -> tuple[dict, str]:
    """Enroll a new device. Returns (row, plaintext_token).

    The plaintext is returned here and never again — it is not recoverable
    from the database, by design.

    A re-registration of the same physical device (same APNs token) revokes
    the old row rather than updating it. Reinstalling the app should not let
    the previous install's bearer token keep working: on a restore-from-backup
    the old Keychain entry may still exist on another device.
    """
    if apns_token:
        conn.execute(
            "UPDATE devices SET revoked_at = ? WHERE apns_token = ? AND revoked_at IS NULL",
            (_now(), apns_token),
        )

    token = new_token()
    cur = conn.execute(
        """INSERT INTO devices (label, platform, token_hash, apns_token, apns_env, last_seen_at)
             VALUES (?,?,?,?,?,?)""",
        (label.strip(), platform, hash_token(token), apns_token, apns_env, _now()),
    )
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row), token


def refresh(
    conn: sqlite3.Connection,
    device_id: int,
    *,
    apns_token: str | None = None,
    apns_env: str | None = None,
    label: str | None = None,
) -> dict | None:
    """Update an already-enrolled device in place. No new bearer token.

    This is the every-launch path: iOS hands the app a device token on each
    start, usually the same one, occasionally not.
    """
    updates: dict[str, object] = {"last_seen_at": _now()}
    if apns_token is not None:
        # Another row may still hold this token from a previous install.
        conn.execute(
            """UPDATE devices SET revoked_at = ?
                 WHERE apns_token = ? AND revoked_at IS NULL AND id != ?""",
            (_now(), apns_token, device_id),
        )
        updates["apns_token"] = apns_token
    if apns_env is not None:
        updates["apns_env"] = apns_env
    if label is not None:
        updates["label"] = label.strip()

    assignments = ", ".join(f"{c} = ?" for c in updates)
    conn.execute(
        f"UPDATE devices SET {assignments} WHERE id = ? AND revoked_at IS NULL",  # noqa: S608
        (*updates.values(), device_id),
    )
    row = conn.execute(
        "SELECT * FROM devices WHERE id = ? AND revoked_at IS NULL", (device_id,)
    ).fetchone()
    return dict(row) if row else None


def revoke(conn: sqlite3.Connection, device_id: int) -> bool:
    """Lock a device out. Idempotent — revoking twice is not an error, because
    the caller doing this is usually panicking about a lost phone."""
    cur = conn.execute(
        "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_now(), device_id),
    )
    return cur.rowcount > 0


def drop_apns_token(conn: sqlite3.Connection, apns_token: str) -> None:
    """Clear a device token APNs has told us is dead (410 Unregistered).

    The device row survives — the app may still be installed and simply have
    had push permission revoked, and its bearer token is still valid for
    everything else.
    """
    conn.execute(
        "UPDATE devices SET apns_token = NULL WHERE apns_token = ?", (apns_token,)
    )
