"""APNs transport. One HTTP/2 POST per device, no SDK.

Provider authentication is a `.p8` signing key, not a `.p12` certificate: the
key never expires, so nothing here needs renewing annually. The key ID, team
ID, and bundle ID come from the Developer portal and are not secrets — the
`.p8` file is.

Callers should use `app.notify.push()`, not this module. This is the wire
layer; `notify` is the contract the scheduler depends on.
"""

import json
import threading
import time
from dataclasses import dataclass

import httpx
import jwt

from app import config

# Apple accepts a provider token for one hour and rejects requests that
# regenerate one more often than every 20 minutes. 45 minutes sits clear of
# both walls.
TOKEN_TTL = 45 * 60

TIMEOUT = 10.0

HOSTS = {
    "prod": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}

# Status codes that mean "this device token is dead, stop sending to it" as
# opposed to "this attempt failed". 410 is the documented Unregistered
# response; a 400 carries its reason in the body.
DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


@dataclass(frozen=True)
class Result:
    ok: bool
    status: int  # 0 when the request never reached Apple
    reason: str = ""

    @property
    def token_is_dead(self) -> bool:
        return self.status == 410 or self.reason in DEAD_TOKEN_REASONS


# ── provider token ────────────────────────────────────────

_lock = threading.Lock()
_cached: tuple[str, float] | None = None  # (jwt, issued_at)


def provider_token(now: float | None = None) -> str:
    """The signed JWT sent as `authorization: bearer`. Cached and reused.

    Signing is ~1ms, but Apple rate-limits regeneration, so the cache is a
    correctness requirement rather than an optimization.
    """
    global _cached
    now = time.time() if now is None else now
    with _lock:
        if _cached is not None and now - _cached[1] < TOKEN_TTL:
            return _cached[0]
        token = jwt.encode(
            {"iss": config.APNS_TEAM_ID, "iat": int(now)},
            config.apns_key_path().read_text(),
            algorithm="ES256",
            headers={"kid": config.APNS_KEY_ID},
        )
        _cached = (token, now)
        return token


def reset_provider_token() -> None:
    """Drop the cached JWT. Called after an ExpiredProviderToken response, and
    by tests."""
    global _cached
    with _lock:
        _cached = None


# ── client ────────────────────────────────────────────────

_client: httpx.Client | None = None


def client() -> httpx.Client:
    """One long-lived HTTP/2 client.

    APNs is HTTP/2-only and strongly prefers a persistent connection —
    reconnecting per push costs a TLS handshake against a latency budget, and
    Apple treats connection churn as abuse.
    """
    global _client
    if _client is None:
        _client = httpx.Client(http2=True, timeout=TIMEOUT)
    return _client


# ── payloads ──────────────────────────────────────────────


def alert_payload(
    body: str,
    *,
    title: str | None = None,
    category: str | None = None,
    data: dict | None = None,
    sound: str = "default",
    thread_id: str | None = None,
) -> dict:
    """Build the `aps` payload.

    `category` is what makes a notification actionable: the app registers a
    `UNNotificationCategory` of the same identifier with its action buttons,
    and iOS renders them without launching anything. `data` rides alongside
    `aps` as top-level custom keys — that is where the reminder id goes, so
    the Snooze button knows what it is snoozing.
    """
    alert: dict = {"body": body}
    if title:
        alert["title"] = title

    aps: dict = {"alert": alert, "sound": sound}
    if category:
        aps["category"] = category
    if thread_id:
        aps["thread-id"] = thread_id

    return {"aps": aps, **(data or {})}


# ── send ──────────────────────────────────────────────────


def send(
    device_token: str,
    payload: dict,
    *,
    apns_env: str = "prod",
    push_type: str = "alert",
    priority: int = 10,
    collapse_id: str | None = None,
    expiration: int | None = None,
    _retry: bool = True,
) -> Result:
    """POST one notification. Returns a Result; network errors do not raise.

    Config errors (missing .p8, unreadable key) DO raise — those are a broken
    install, not a transient failure, and `notify.push` converts them into a
    False return for the scheduler.
    """
    host = HOSTS.get(apns_env, HOSTS["prod"])
    headers = {
        "authorization": f"bearer {provider_token()}",
        "apns-topic": config.APNS_BUNDLE_ID,
        "apns-push-type": push_type,
        "apns-priority": str(priority),
    }
    if collapse_id:
        # Max 64 bytes; a longer one is rejected outright. Truncating beats
        # failing the whole send over a cosmetic grouping hint.
        headers["apns-collapse-id"] = collapse_id[:64]
    if expiration is not None:
        headers["apns-expiration"] = str(expiration)

    try:
        response = client().post(
            f"{host}/3/device/{device_token}",
            content=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
    except httpx.HTTPError as exc:
        return Result(ok=False, status=0, reason=type(exc).__name__)

    if response.status_code == 200:
        return Result(ok=True, status=200)

    reason = ""
    try:
        reason = response.json().get("reason", "")
    except ValueError:
        reason = response.text[:200]

    # A cached provider token that aged out mid-flight is the one failure
    # worth retrying immediately — otherwise every push in this tick fails
    # until the 45-minute cache happens to roll over.
    if reason == "ExpiredProviderToken" and _retry:
        reset_provider_token()
        return send(
            device_token,
            payload,
            apns_env=apns_env,
            push_type=push_type,
            priority=priority,
            collapse_id=collapse_id,
            expiration=expiration,
            _retry=False,
        )

    return Result(ok=False, status=response.status_code, reason=reason)
