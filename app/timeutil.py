"""Time handling.

STORAGE RULE: every timestamp is stored as ISO 8601 **normalized to UTC**
("2026-07-30T21:00:00Z"). The design doc says "with offset" — Z is an offset,
so this satisfies it, but the normalization matters for a reason worth stating:

  Range queries compare these as *strings*. "2026-07-30T15:00:00-06:00" and
  "2026-07-30T22:00:00Z" are the same instant, but sort in the wrong order
  lexicographically. Mixed offsets in one column — which you get for free
  across a DST boundary — silently corrupt every BETWEEN query in /agenda.

So: normalize on write, convert to local only when speaking.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"


def zone(tz_name: str) -> ZoneInfo:
    return ZoneInfo(tz_name)


def now(tz_name: str) -> datetime:
    """Current time as an aware datetime in the given zone."""
    return datetime.now(zone(tz_name))


def parse(value: str) -> datetime:
    """Parse an ISO 8601 string into an aware datetime.

    Accepts the 'Z' suffix, which fromisoformat() rejects before 3.11 and
    which the model emits constantly.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(
            f"naive timestamp {value!r} — the router must emit an offset"
        )
    return dt


def to_utc_iso(value: str | datetime) -> str:
    """Normalize any aware timestamp to the stored form."""
    dt = parse(value) if isinstance(value, str) else value
    if dt.tzinfo is None:
        raise ValueError("refusing to store a naive datetime")
    return dt.astimezone(timezone.utc).strftime(ISO_UTC)


def to_local(value: str, tz_name: str) -> datetime:
    return parse(value).astimezone(zone(tz_name))


def window_utc(tz_name: str, days: int) -> tuple[str, str]:
    """[start, end) covering `days` days from local midnight today."""
    local_now = now(tz_name)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return to_utc_iso(start), to_utc_iso(start + timedelta(days=days))


# ── speech ────────────────────────────────────────────────
# Replies go straight to a TTS engine, so these render for the ear:
# no ISO strings, no 24-hour clock, no leading zeros.


def speak_time(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {meridiem}" if dt.minute else f"{hour} {meridiem}"


def speak_datetime(value: str, tz_name: str, all_day: bool = False) -> str:
    """'Thursday, July 30 at 3 PM' — relative-dated when it's near."""
    dt = to_local(value, tz_name)
    today = now(tz_name).date()
    delta = (dt.date() - today).days

    if delta == 0:
        day = "today"
    elif delta == 1:
        day = "tomorrow"
    elif delta == -1:
        day = "yesterday"
    elif 0 < delta < 7:
        day = dt.strftime("%A")
    else:
        day = dt.strftime("%A, %B %-d")

    return day if all_day else f"{day} at {speak_time(dt)}"
