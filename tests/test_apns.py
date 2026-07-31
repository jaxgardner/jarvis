"""APNs transport and the push contract — offline, against a mock transport.

The contract these protect is the one `scheduler/run.py` depends on:
`notify.push()` returns a bool and never raises. A raised exception in that
loop takes down every other due reminder in the same tick.
"""

import json
import sqlite3
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app import apns, config, devices, notify
from tests.helpers import apply_migrations

TEAM_ID = "TEAM123456"
KEY_ID = "KEY7654321"
BUNDLE_ID = "com.example.jarvis"
DEVICE_TOKEN = "ab" * 32


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    """A real P-256 key, so the JWT is really ES256-signed and really
    verifiable. Apple's .p8 is exactly this: a PKCS#8 EC private key."""
    key = ec.generate_private_key(ec.SECP256R1())
    p8 = tmp_path / "AuthKey_TEST.p8"
    p8.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    monkeypatch.setattr(config, "APNS_KEY_PATH", str(p8))
    monkeypatch.setattr(config, "APNS_KEY_ID", KEY_ID)
    monkeypatch.setattr(config, "APNS_TEAM_ID", TEAM_ID)
    monkeypatch.setattr(config, "APNS_BUNDLE_ID", BUNDLE_ID)
    apns.reset_provider_token()
    yield key.public_key()
    apns.reset_provider_token()


@pytest.fixture
def transport(monkeypatch):
    """Capture requests instead of sending them. Returns the request log; set
    `.responses` to script a sequence of replies."""

    class Recorder:
        def __init__(self):
            self.requests: list[httpx.Request] = []
            self.responses: list[httpx.Response] = []

        def handle(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.responses:
                return self.responses.pop(0)
            return httpx.Response(200)

    recorder = Recorder()
    monkeypatch.setattr(
        apns, "_client", httpx.Client(transport=httpx.MockTransport(recorder.handle))
    )
    return recorder


# ── provider token ────────────────────────────────────────


def test_provider_token_is_a_verifiable_es256_jwt(signing_key):
    token = apns.provider_token()
    assert jwt.get_unverified_header(token)["kid"] == KEY_ID
    claims = jwt.decode(token, signing_key, algorithms=["ES256"])
    assert claims["iss"] == TEAM_ID
    assert claims["iat"] <= int(time.time()) + 1


def test_provider_token_is_cached(signing_key):
    """Apple rejects providers that regenerate more often than every 20
    minutes, so caching here is correctness, not speed."""
    assert apns.provider_token() == apns.provider_token()


def test_provider_token_is_regenerated_before_it_expires(signing_key):
    first = apns.provider_token(now=1_000_000)
    later = apns.provider_token(now=1_000_000 + apns.TOKEN_TTL + 1)
    assert first != later


# ── payload ───────────────────────────────────────────────


def test_alert_payload_carries_the_action_category_and_row_id():
    """`category` is what draws the Snooze / Done buttons; the id in the custom
    keys is what tells them which reminder they are acting on."""
    payload = apns.alert_payload(
        "take the bins out",
        title="Reminder",
        category="REMINDER",
        data={"reminder_id": 7, "kind": "reminder"},
    )
    assert payload["aps"]["alert"] == {"title": "Reminder", "body": "take the bins out"}
    assert payload["aps"]["category"] == "REMINDER"
    assert payload["reminder_id"] == 7, "custom keys ride beside aps, not inside it"


# ── send ──────────────────────────────────────────────────


def test_send_uses_the_documented_path_and_headers(signing_key, transport):
    result = apns.send(DEVICE_TOKEN, {"aps": {"alert": "hi"}}, priority=10)
    assert result.ok

    request = transport.requests[0]
    assert str(request.url) == f"https://api.push.apple.com/3/device/{DEVICE_TOKEN}"
    assert request.headers["apns-topic"] == BUNDLE_ID
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    assert request.headers["authorization"].startswith("bearer ")
    assert json.loads(request.content)["aps"]["alert"] == "hi"


def test_sandbox_env_goes_to_the_sandbox_host(signing_key, transport):
    """Xcode debug builds get sandbox tokens; sending those to production is
    the classic 'push works on TestFlight but not on my device' bug."""
    apns.send(DEVICE_TOKEN, {"aps": {}}, apns_env="sandbox")
    assert "api.sandbox.push.apple.com" in str(transport.requests[0].url)


def test_a_410_marks_the_device_token_dead(signing_key, transport):
    transport.responses = [httpx.Response(410, json={"reason": "Unregistered"})]
    result = apns.send(DEVICE_TOKEN, {"aps": {}})
    assert not result.ok
    assert result.token_is_dead


def test_a_503_is_a_retryable_failure_not_a_dead_token(signing_key, transport):
    transport.responses = [httpx.Response(503, json={"reason": "ServiceUnavailable"})]
    result = apns.send(DEVICE_TOKEN, {"aps": {}})
    assert not result.ok
    assert not result.token_is_dead


def test_an_expired_provider_token_is_retried_immediately(signing_key, transport):
    """Otherwise every push in the tick fails until the 45-minute cache happens
    to roll over on its own."""
    transport.responses = [
        httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
        httpx.Response(200),
    ]
    assert apns.send(DEVICE_TOKEN, {"aps": {}}).ok
    assert len(transport.requests) == 2


def test_the_retry_happens_only_once(signing_key, transport):
    transport.responses = [
        httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
        httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
    ]
    assert not apns.send(DEVICE_TOKEN, {"aps": {}}).ok
    assert len(transport.requests) == 2, "must not loop"


def test_a_network_error_is_a_result_not_an_exception(signing_key, monkeypatch):
    def explode(request):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(
        apns, "_client", httpx.Client(transport=httpx.MockTransport(explode))
    )
    result = apns.send(DEVICE_TOKEN, {"aps": {}})
    assert result.ok is False and result.status == 0


# ── the notify contract ───────────────────────────────────


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "push.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


def register(db, apns_token=DEVICE_TOKEN, env="prod"):
    from app.db import transaction

    with transaction() as conn:
        devices.register(conn, label="iPhone", apns_token=apns_token, apns_env=env)


def test_push_reaches_every_registered_device(db, signing_key, transport, monkeypatch):
    monkeypatch.setenv("PUSH_BACKENDS", "apns")
    register(db, "aa" * 32)
    register(db, "bb" * 32)
    assert notify.push("bins", title="Reminder", category="REMINDER") is True
    assert len(transport.requests) == 2


def test_push_reports_failure_when_no_device_is_registered(db, signing_key, transport, monkeypatch):
    """Vacuous success is the failure mode this whole subsystem exists to
    prevent — a reminder marked delivered that went nowhere."""
    monkeypatch.setenv("PUSH_BACKENDS", "apns")
    assert notify.push("bins") is False


def test_push_never_raises_when_apns_is_unconfigured(db, monkeypatch):
    """The scheduler calls this in a loop. An exception here would take out
    every other due reminder in the same tick."""
    monkeypatch.setenv("PUSH_BACKENDS", "apns")
    monkeypatch.setattr(config, "APNS_KEY_PATH", "")
    register(db)
    assert notify.push("bins") is False


def test_a_dead_token_is_cleared_from_the_device_row(db, signing_key, transport, monkeypatch):
    monkeypatch.setenv("PUSH_BACKENDS", "apns")
    register(db)
    transport.responses = [httpx.Response(410, json={"reason": "Unregistered"})]
    assert notify.push("bins") is False

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT apns_token, revoked_at FROM devices").fetchone()
    finally:
        conn.close()
    assert row[0] is None, "dead token should be cleared"
    assert row[1] is None, "the device itself stays enrolled — only push is gone"


def test_removing_the_ntfy_topic_cannot_break_apns_delivery(db, signing_key, transport, monkeypatch):
    """Retiring ntfy is done by editing .env, and the obvious edit is to delete
    NTFY_TOPIC while leaving 'ntfy' in PUSH_BACKENDS. That makes
    config.ntfy_topic() raise — which must degrade to a skipped backend, not
    an exception that takes the whole scheduler tick down with it."""
    monkeypatch.setenv("PUSH_BACKENDS", "ntfy,apns")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    register(db)
    assert notify.push("bins") is True
    assert len(transport.requests) == 1, "APNs should still have been tried"


def test_dual_send_counts_ntfy_alone_as_delivered(db, signing_key, transport, monkeypatch):
    """The two-week cutover window: APNs failing must not stop reminders from
    being marked delivered while ntfy is still the proven path."""
    monkeypatch.setenv("PUSH_BACKENDS", "ntfy,apns")
    monkeypatch.setattr(notify, "_push_ntfy", lambda *a, **k: True)
    transport.responses = [httpx.Response(500, json={"reason": "InternalServerError"})]
    register(db)
    assert notify.push("bins") is True


def test_dual_send_hits_both_backends(db, signing_key, transport, monkeypatch):
    monkeypatch.setenv("PUSH_BACKENDS", "ntfy,apns")
    ntfy_calls: list[str] = []
    monkeypatch.setattr(
        notify, "_push_ntfy", lambda message, *a, **k: (ntfy_calls.append(message), True)[1]
    )
    register(db)
    assert notify.push("bins") is True
    assert ntfy_calls == ["bins"] and len(transport.requests) == 1
