"""Google OAuth, by hand.

    uv run python -m ingest.google_auth --authorize   once, interactively
    uv run python -m ingest.google_auth --check       any time, incl. day 8

No Google SDK. `google-auth-oauthlib` drags in google-auth, oauthlib,
requests-oauthlib and requests to perform what is, on the recurring path, a
single POST — and this project already writes its own APNs client and its own
ntfy push for the same reason. The one-time authorization is more than a POST,
so it is implemented carefully below rather than casually: PKCE with S256, a
`state` parameter that is actually checked, and a loopback redirect.

**The thing this file exists to prevent.** A Google Cloud OAuth consent screen
left in *Testing* publishing status issues refresh tokens that expire after
seven days, with no warning and no error until they are used. That produces an
assistant which ingests perfectly for a week and then quietly stops — the same
silent-failure shape as reminders that stop firing. Set the consent screen to
**In production** before authorizing. `--check` exists so day 8 is a thing you
verify rather than assume.
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from app import config

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Read-only, and only what each phase actually needs. calendar.readonly is a
# "sensitive" scope; gmail.readonly is "restricted" — the stricter of the two
# is what governs the consent screen's requirements.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Desktop-app OAuth clients may redirect to any loopback port, so this doesn't
# need registering in the console. Fixed rather than random so the one manual
# step is reproducible.
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/"

# Refresh this far before actual expiry. An access token that dies mid-sync
# turns one 401 into a failed run.
EXPIRY_MARGIN = timedelta(minutes=5)

TIMEOUT = 30.0


def token_path() -> Path:
    """Beside the database, not in the repo — same reasoning as the APNs .p8.

    This file holds a refresh token, which is a long-lived credential for
    reading your mail. It must never be committable.
    """
    return config.DB_PATH.parent / "google_token.json"


# ── stored credentials ────────────────────────────────────


@dataclass
class Credentials:
    refresh_token: str
    access_token: str = ""
    expires_at: str = ""  # ISO 8601 UTC
    scopes: list[str] | None = None
    obtained_at: str = ""

    @property
    def expired(self) -> bool:
        if not self.access_token or not self.expires_at:
            return True
        expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) + EXPIRY_MARGIN >= expiry

    def to_json(self) -> dict:
        return {
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "scopes": self.scopes or [],
            "obtained_at": self.obtained_at,
        }


def load() -> Credentials | None:
    path = token_path()
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return Credentials(
        refresh_token=data["refresh_token"],
        access_token=data.get("access_token", ""),
        expires_at=data.get("expires_at", ""),
        scopes=data.get("scopes") or [],
        obtained_at=data.get("obtained_at", ""),
    )


def save(credentials: Credentials) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with the restrictive mode already in place rather than chmod'ing
    # afterwards — the gap between the two is a window where the refresh token
    # is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(credentials.to_json(), handle, indent=2)


# ── the recurring path ────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh(credentials: Credentials) -> Credentials:
    """Exchange the refresh token for a new access token.

    Raises on failure rather than returning something falsy. A dead refresh
    token is not a transient condition to be retried quietly — it is the exact
    seven-day failure this module warns about, and the caller must be able to
    tell it apart from a network blip.
    """
    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": config.google_client_id(),
            "client_secret": config.google_client_secret(),
            "refresh_token": credentials.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RefreshFailed(response.status_code, response.text[:400])

    payload = response.json()
    expires_in = int(payload.get("expires_in", 3600))
    credentials.access_token = payload["access_token"]
    credentials.expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Google does not return a new refresh_token on refresh; the stored one
    # stays. If it ever does send one, honour it.
    if payload.get("refresh_token"):
        credentials.refresh_token = payload["refresh_token"]
    save(credentials)
    return credentials


class RefreshFailed(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        hint = ""
        if "invalid_grant" in body:
            hint = (
                "\n\ninvalid_grant almost always means the refresh token was "
                "revoked or expired. If the consent screen is still in "
                "'Testing' publishing status, tokens expire after 7 days — "
                "set it to 'In production' and re-run --authorize."
            )
        super().__init__(f"token refresh failed ({status}): {body}{hint}")


def access_token() -> str:
    """A valid access token, refreshing if needed. The only function the
    ingesters should call."""
    credentials = load()
    if credentials is None:
        raise RuntimeError(
            f"no Google credentials at {token_path()}. Run:\n"
            "    uv run python -m ingest.google_auth --authorize"
        )
    if credentials.expired:
        credentials = refresh(credentials)
    return credentials.access_token


# ── the one-time path ─────────────────────────────────────


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches Google's redirect. One request, then the server stops."""

    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in query.items()}
        body = (
            b"<html><body style='font:16px -apple-system;padding:3rem'>"
            b"<h2>Jarvis is authorized.</h2>"
            b"<p>You can close this tab and go back to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass  # the default handler logs every hit to stderr


def authorize() -> Credentials:
    """Interactive, once. Opens a browser, catches the redirect, stores the
    refresh token."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(24)

    params = {
        "client_id": config.google_client_id(),
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # access_type=offline is what makes Google issue a refresh token at
        # all; prompt=consent forces a fresh one even if this account has
        # authorized before, which matters when re-running after changing the
        # publishing status.
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Open this in a browser on the Mini:\n")
    print(f"  {url}\n")
    print("Waiting for the redirect…")
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # noqa: BLE001 — headless is fine, the URL is printed
        pass

    thread.join(timeout=300)
    server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise RuntimeError("timed out waiting for the redirect")
    if "error" in result:
        raise RuntimeError(f"authorization denied: {result['error']}")
    if not secrets.compare_digest(result.get("state", ""), state):
        # A mismatched state means the response didn't come from the request
        # we made. Never exchange that code.
        raise RuntimeError("state mismatch — discarding the authorization code")

    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": config.google_client_id(),
            "client_secret": config.google_client_secret(),
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"code exchange failed ({response.status_code}): {response.text[:400]}")

    payload = response.json()
    if "refresh_token" not in payload:
        raise RuntimeError(
            "Google returned no refresh_token. This happens when the account "
            "has already granted these scopes and prompt=consent was not "
            "honoured — revoke Jarvis at "
            "https://myaccount.google.com/permissions and try again."
        )

    credentials = Credentials(
        refresh_token=payload["refresh_token"],
        access_token=payload["access_token"],
        expires_at=(
            datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in", 3600)))
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        scopes=payload.get("scope", "").split(),
        obtained_at=_now_iso(),
    )
    save(credentials)
    return credentials


# ── the day-8 check ───────────────────────────────────────


def check() -> int:
    """Prove the credential still works, and say how old it is.

    Run this on day 8. If the consent screen was left in Testing, this is
    where it surfaces — loudly, on a day you chose, instead of silently on a
    morning you needed the agenda.
    """
    credentials = load()
    if credentials is None:
        print(f"no credentials at {token_path()}", file=sys.stderr)
        print("run: uv run python -m ingest.google_auth --authorize", file=sys.stderr)
        return 1

    age = ""
    if credentials.obtained_at:
        obtained = datetime.fromisoformat(credentials.obtained_at.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - obtained).days
        age = f" ({days} day(s) old)"

    print(f"stored at   {token_path()}")
    print(f"authorized  {credentials.obtained_at or 'unknown'}{age}")
    print(f"scopes      {', '.join(credentials.scopes or []) or 'unknown'}")

    try:
        # Force a real refresh rather than reusing a cached access token —
        # reusing one would pass for an hour after the refresh token died.
        credentials.access_token = ""
        credentials = refresh(credentials)
    except RefreshFailed as exc:
        print(f"\nREFRESH FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"refresh     ok, new access token expires {credentials.expires_at}")

    response = httpx.get(
        "https://www.googleapis.com/calendar/v3/users/me/calendarList",
        headers={"Authorization": f"Bearer {credentials.access_token}"},
        params={"maxResults": 10},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        print(f"\nCALENDAR CALL FAILED ({response.status_code}): {response.text[:300]}", file=sys.stderr)
        return 1

    calendars = response.json().get("items", [])
    print(f"calendars   {len(calendars)} visible")
    for calendar in calendars[:10]:
        primary = "  (primary)" if calendar.get("primary") else ""
        print(f"              {calendar.get('summary', '?')}{primary}")
    print("\nOK — the credential is live and can read Calendar.")
    return 0


def main() -> int:
    if "--authorize" in sys.argv:
        try:
            credentials = authorize()
        except Exception as exc:  # noqa: BLE001 — a CLI, print and exit
            print(f"authorization failed: {exc}", file=sys.stderr)
            return 1
        print(f"\nStored refresh token at {token_path()} (mode 600).")
        print(f"Scopes: {', '.join(credentials.scopes or [])}")
        print("\nNow run this again in 8 days:")
        print("    uv run python -m ingest.google_auth --check")
        return 0
    if "--check" in sys.argv:
        return check()

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
