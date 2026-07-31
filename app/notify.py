"""Push. One function, several backends behind it.

    push(message, title=..., category=..., data=...) -> bool

**This function returns a bool and never raises, and both halves of that are
load-bearing.** `scheduler/run.py` calls it in a loop over every due reminder:
a False return puts that one reminder back to `pending` for the next tick,
while an exception would abort the whole run and take every *other* due
reminder down with it. Phase 7 swapped the backend underneath this signature;
it did not change the signature, and neither should anything else.

Backends are selected by `PUSH_BACKENDS` (default `ntfy`). Running
`ntfy,apns` sends both — which is how the APNs cutover is meant to be done:
a fortnight of dual delivery, then drop ntfy once APNs has been quiet-free.
"""

import httpx

from app import apns, config, devices
from app.db import transaction

TIMEOUT = 10.0

# ntfy priority names → the APNs equivalent. APNs has two levels that matter:
# 10 delivers immediately, 5 lets the OS batch for power. A reminder firing is
# the definition of "immediately"; the daily selfcheck is not.
_APNS_PRIORITY = {"low": 5, "min": 5, "default": 10, "high": 10, "urgent": 10}


def push(
    message: str,
    *,
    title: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    category: str | None = None,
    data: dict | None = None,
    collapse_id: str | None = None,
) -> bool:
    """Send a notification. Returns success rather than raising.

    True means at least one enabled backend accepted it. With both backends
    enabled, ntfy alone succeeding is still True — during the cutover the
    proven path is what decides whether a reminder counts as delivered.
    """
    backends = config.push_backends()
    delivered = False

    for backend in backends:
        try:
            if backend == "ntfy":
                delivered |= _push_ntfy(message, title, priority, tags)
            elif backend == "apns":
                delivered |= _push_apns(
                    message, title, priority, category, data, collapse_id
                )
        except Exception:  # noqa: BLE001 — see the module docstring
            continue

    return delivered


# ── ntfy ──────────────────────────────────────────────────


def _push_ntfy(
    message: str, title: str | None, priority: str | None, tags: str | None
) -> bool:
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


# ── APNs ──────────────────────────────────────────────────


def _push_apns(
    message: str,
    title: str | None,
    priority: str | None,
    category: str | None,
    data: dict | None,
    collapse_id: str | None,
) -> bool:
    """Fan out to every registered device.

    Returns False when no device is registered. That looks unhelpful — the
    scheduler will retry the reminder every tick until it ages out — but the
    alternative is reporting delivery of a notification that went nowhere,
    and "reminders silently stopped arriving" is exactly the failure this
    whole subsystem is built to make loud.
    """
    if not config.apns_configured():
        return False

    conn_targets = _targets()
    if not conn_targets:
        return False

    payload = apns.alert_payload(message, title=title, category=category, data=data)
    apns_priority = _APNS_PRIORITY.get((priority or "default").lower(), 10)

    delivered = False
    dead: list[str] = []
    for device in conn_targets:
        result = apns.send(
            device["apns_token"],
            payload,
            apns_env=device.get("apns_env") or config.APNS_ENV,
            priority=apns_priority,
            collapse_id=collapse_id,
        )
        if result.ok:
            delivered = True
        elif result.token_is_dead:
            dead.append(device["apns_token"])

    if dead:
        _forget(dead)
    return delivered


def _targets() -> list[dict]:
    with transaction() as conn:
        return devices.push_targets(conn)


def _forget(tokens: list[str]) -> None:
    """Clear device tokens Apple says are gone. A reinstall re-registers on
    next launch, so this is cleanup, not deletion."""
    with transaction() as conn:
        for token in tokens:
            devices.drop_apns_token(conn, token)
