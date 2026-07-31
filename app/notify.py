"""ntfy push.

One POST, no SDK, no API key — the topic name is the only credential, which
is why it must be long and random. Wrapped in a single function so switching
to Pushover (or anything else) is a one-file change.
"""

import httpx

from app import config

TIMEOUT = 10.0


def push(
    message: str,
    *,
    title: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
) -> bool:
    """Send a notification. Returns success rather than raising.

    The caller is a scheduler tick that must not die on a transient network
    failure — a raised exception there would take down the whole run and
    every other due reminder with it.
    """
    headers: dict[str, str] = {}
    if title:
        headers["Title"] = title
    if priority:
        headers["Priority"] = priority
    if tags:
        headers["Tags"] = tags

    url = f"{config.NTFY_SERVER}/{config.ntfy_topic()}"
    try:
        response = httpx.post(
            url, content=message.encode("utf-8"), headers=headers, timeout=TIMEOUT
        )
        return response.status_code < 400
    except httpx.HTTPError:
        return False
