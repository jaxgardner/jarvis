"""Google OAuth — offline, no network, no browser.

The interactive authorization runs once by hand and isn't worth mocking end to
end. The *refresh* path runs unattended forever, and its failure mode is the
seven-day silent stop, so that is what these cover.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import config
from ingest import google_auth


@pytest.fixture(autouse=True)
def credential_store(tmp_path, monkeypatch):
    """Point the token file somewhere disposable. Without this a test run
    would overwrite the real refresh token."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jarvis.db")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    return tmp_path


def stored(**overrides) -> google_auth.Credentials:
    defaults = {
        "refresh_token": "1//refresh",
        "access_token": "ya29.access",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "scopes": google_auth.SCOPES,
        "obtained_at": "2026-07-31T12:00:00Z",
    }
    return google_auth.Credentials(**{**defaults, **overrides})


# ── storage ───────────────────────────────────────────────


def test_token_file_lives_outside_the_repo(credential_store):
    """It holds a long-lived credential for reading mail. It must not be
    anywhere a `git add -A` could reach."""
    from app.config import REPO_ROOT

    path = google_auth.token_path()
    assert REPO_ROOT not in path.parents
    assert path.parent == credential_store


def test_token_file_is_not_world_readable(credential_store):
    google_auth.save(stored())
    assert google_auth.token_path().stat().st_mode & 0o077 == 0


def test_round_trips(credential_store):
    google_auth.save(stored(refresh_token="1//specific"))
    assert google_auth.load().refresh_token == "1//specific"


def test_load_returns_none_before_authorizing(credential_store):
    assert google_auth.load() is None


# ── expiry ────────────────────────────────────────────────


def test_a_token_expiring_within_the_margin_counts_as_expired():
    """Refreshing early is the point: a token that dies mid-sync turns one
    401 into a failed run."""
    soon = datetime.now(timezone.utc) + timedelta(minutes=2)
    assert stored(expires_at=soon.strftime("%Y-%m-%dT%H:%M:%SZ")).expired


def test_a_fresh_token_is_not_expired():
    assert not stored().expired


def test_missing_access_token_counts_as_expired():
    assert stored(access_token="").expired


# ── refresh ───────────────────────────────────────────────


def test_refresh_stores_the_new_access_token(credential_store, monkeypatch):
    def fake_post(url, data=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "1//refresh"
        return httpx.Response(
            200,
            json={"access_token": "ya29.new", "expires_in": 3600},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(google_auth.httpx, "post", fake_post)
    google_auth.save(stored(access_token="", expires_at=""))

    refreshed = google_auth.refresh(google_auth.load())
    assert refreshed.access_token == "ya29.new"
    assert google_auth.load().access_token == "ya29.new", "must persist, not just return"


def test_refresh_keeps_the_existing_refresh_token(credential_store, monkeypatch):
    """Google doesn't return a new refresh_token on refresh. Overwriting the
    stored one with an absent value would destroy the credential on the first
    successful refresh."""
    monkeypatch.setattr(
        google_auth.httpx,
        "post",
        lambda url, data=None, timeout=None: httpx.Response(
            200, json={"access_token": "ya29.new", "expires_in": 3600},
            request=httpx.Request("POST", url),
        ),
    )
    google_auth.save(stored())
    google_auth.refresh(google_auth.load())
    assert google_auth.load().refresh_token == "1//refresh"


def test_a_dead_refresh_token_raises_rather_than_returning_falsy(credential_store, monkeypatch):
    """This is the seven-day failure. It must be distinguishable from a
    network blip, so it raises instead of being swallowed as 'no token today'."""
    monkeypatch.setattr(
        google_auth.httpx,
        "post",
        lambda url, data=None, timeout=None: httpx.Response(
            400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url),
        ),
    )
    google_auth.save(stored(access_token="", expires_at=""))

    with pytest.raises(google_auth.RefreshFailed) as caught:
        google_auth.refresh(google_auth.load())
    assert caught.value.status == 400


def test_invalid_grant_says_what_actually_causes_it(credential_store, monkeypatch):
    """The error Google returns is 'invalid_grant', which explains nothing.
    Whoever reads this at 7am needs to be pointed at the publishing status."""
    monkeypatch.setattr(
        google_auth.httpx,
        "post",
        lambda url, data=None, timeout=None: httpx.Response(
            400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url),
        ),
    )
    google_auth.save(stored(access_token="", expires_at=""))

    with pytest.raises(google_auth.RefreshFailed, match="Testing"):
        google_auth.refresh(google_auth.load())


# ── access_token() ────────────────────────────────────────


def test_access_token_reuses_a_live_token(credential_store, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("should not have refreshed a live token")

    monkeypatch.setattr(google_auth.httpx, "post", refuse)
    google_auth.save(stored())
    assert google_auth.access_token() == "ya29.access"


def test_access_token_refreshes_an_expired_one(credential_store, monkeypatch):
    monkeypatch.setattr(
        google_auth.httpx,
        "post",
        lambda url, data=None, timeout=None: httpx.Response(
            200, json={"access_token": "ya29.fresh", "expires_in": 3600},
            request=httpx.Request("POST", url),
        ),
    )
    google_auth.save(stored(expires_at="2020-01-01T00:00:00Z"))
    assert google_auth.access_token() == "ya29.fresh"


def test_access_token_without_credentials_says_how_to_fix_it(credential_store):
    with pytest.raises(RuntimeError, match="--authorize"):
        google_auth.access_token()


# ── scopes ────────────────────────────────────────────────


def test_scopes_are_read_only():
    """Ingestion reads. A scope that can write to a calendar is a scope that
    can delete one, and nothing in Phase 6 needs it."""
    assert all(scope.endswith(".readonly") for scope in google_auth.SCOPES)


def test_the_stored_file_carries_no_client_secret(credential_store):
    """The secret belongs in .env, which is gitignored and chmod 600. Copying
    it into a second file just doubles the number of places to leak it."""
    google_auth.save(stored())
    assert "client_secret" not in json.loads(google_auth.token_path().read_text())
