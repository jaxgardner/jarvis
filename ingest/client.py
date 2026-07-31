"""One HTTP path to Google's APIs, for every ingester.

Small on purpose. The ingesters do GETs against JSON endpoints; what they need
from a client is that transient failures don't look like real ones, and that
real ones carry their status code so the caller can act on it. Two statuses in
particular are *routine* rather than errors, and the whole reason this returns
a typed exception:

    410 Gone   — Calendar expired the syncToken. Drop it, refetch in full.
    404        — Gmail expired the historyId. Same move.

An ingester that treats either as fatal stops permanently after a quiet week,
which is the failure this module is built to avoid.
"""

import time

import httpx

from ingest import google_auth

TIMEOUT = 60.0

# Retried, with backoff. 429 is Google's rate limit; 5xx is Google having a
# bad minute. Neither means the sync is broken.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"{url} -> {status}: {body[:300]}")


def get(url: str, params: dict | None = None, *, attempts: int = MAX_ATTEMPTS) -> dict:
    """GET a Google API endpoint and return parsed JSON.

    Raises ApiError on any non-200 that survives the retry policy. Callers that
    care about a specific status — 410 and 404 both mean "cursor expired, start
    over" — catch it and read `.status`.
    """
    last: ApiError | None = None

    for attempt in range(attempts):
        # Fetched per attempt, not once: a long paginated sync can outlive an
        # access token, and google_auth.access_token() refreshes on demand.
        token = google_auth.access_token()
        try:
            response = httpx.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT,
            )
        except httpx.RequestError as exc:
            last = ApiError(0, f"network error: {exc}", url)
            _sleep(attempt, attempts)
            continue

        if response.status_code == 200:
            return response.json()

        if response.status_code == 401:
            # The token was rejected despite being fresh by our clock. Force a
            # refresh and try once more; if it fails again it is a real auth
            # problem and should surface as one.
            google_auth.invalidate()
            last = ApiError(401, response.text, url)
            if attempt == 0:
                continue
            raise last

        if response.status_code in RETRY_STATUSES:
            last = ApiError(response.status_code, response.text, url)
            _sleep(attempt, attempts)
            continue

        raise ApiError(response.status_code, response.text, url)

    raise last or ApiError(0, "exhausted retries", url)


def _sleep(attempt: int, attempts: int) -> None:
    # Nothing follows the last attempt, so sleeping after it just delays the
    # exception by eight seconds.
    if attempt < attempts - 1:
        time.sleep(BACKOFF_BASE**attempt)
