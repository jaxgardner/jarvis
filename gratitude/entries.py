"""Three things a day: the writes, the reads, and the streak.

The scheduler imports this through `gratitude.nudge`, so nothing here may
reach an LLM — design principle 3 says the evening prompt keeps arriving on
nights when the agent is broken.
"""

from datetime import datetime, timedelta

from app import config

# Three is the feature, not a setting. A two-item day would make the page's
# three slots a lie, and a ten-item day is not what the prompt is asking for.
TARGET = 3


def day_for(local: datetime, day_start: int | None = None) -> str:
    """Which gratitude day an aware LOCAL datetime belongs to.

    The day runs to `GRATITUDE_DAY_START` rather than to midnight. Pure date
    arithmetic on the local wall clock, so it is indifferent to DST: both
    halves of an ambiguous 01:30 are before the cutoff and land on the same
    date.

    The single owner of this rule. Every `entry_on` in the database and every
    day-comparison in this package comes through here, so there is no second
    place for the boundary to be decided differently.
    """
    if day_start is None:
        day_start = config.GRATITUDE_DAY_START
    if local.hour < day_start:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()
