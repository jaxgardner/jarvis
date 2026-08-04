# A daily gratitude prompter

At 10pm, if the day is unfinished, the phone asks for three things you're
grateful for. You say them in Talk — all three at once or one at a time — and a
new tab shows today's slots, a streak, and every day behind it.

## Why this shape

**The nudge is a sweep, not a `reminders` row.** This is the same call the
pantry expiry sweep made, for the same two reasons and one more. A reminder
appears in `/agenda`, and a gratitude prompt is not an appointment. A reminder
is scheduled ahead of time, so one queued at 10pm still fires after you logged
your three at eight — the sweep reads `gratitude_entries` live and is therefore
correct by construction: a finished day cannot notify. And the scheduler has no
LLM dependency, which is what keeps the prompt arriving on evenings when the
agent is broken.

**The day runs to 4am, not to midnight.** A prompt that arrives at 10pm will
sometimes be answered at 12:30. Under a plain calendar rule that entry opens a
new day and leaves the one you were actually thinking about looking skipped —
which is the streak breaking for doing the thing. `GRATITUDE_DAY_START = 4`
moves the boundary to where the day subjectively ends. It applies only to
`entry_on`; the 22:00 push window is real clock time, so nothing fires at
half past midnight.

**Three is a target, not a limit.** A fourth thing is stored and shown; the day
still reads complete at three. Refusing gratitude because a counter is full
would be the app being pedantic about its own bookkeeping, which is the call
`consume_item` already makes when it adds an unknown food to the shopping list
rather than complaining that the pantry row is missing.

**An incomplete today does not break the streak.** The streak counts backwards
from the most recent day that could still be finished. Today at zero is a day
in progress, not a failure; the streak breaks only when a *completed* day is
missing behind you. A number that turns into a reproach at 6pm is a number that
gets muted along with the notification.

**Capture is one tool taking a list.** `log_gratitude(items[])` accepts one to
three per turn and appends to the day. Requiring all three in one breath would
fail exactly when you have two on the tip of your tongue; a guided three-turn
conversation would need per-user state across utterances, which nothing in the
fast path has and which one feature does not justify building.

**No voice recall.** `query` learns nothing about gratitude. Reading the page
is the only way to look back. Every misroute in this system starts in the
router, and a `kind='gratitude'` branch is surface area bought for a question
("what was I grateful for last Tuesday") nobody has asked yet.

## Schema

Migration `012_gratitude.sql`:

```sql
CREATE TABLE gratitude_entries (
  id         INTEGER PRIMARY KEY,
  body       TEXT NOT NULL,
  entry_on   TEXT NOT NULL,   -- YYYY-MM-DD, cutoff already applied
  created_at TEXT NOT NULL    -- ISO 8601 with offset
);
CREATE INDEX idx_gratitude_day ON gratitude_entries(entry_on);
```

`entry_on` is `_on` rather than `_at` by the naming convention: a gratitude day
has no time of day, and `created_at` already carries the instant for ordering
within the day. Order is arrival order — there is no position column, because
there is no way to reorder them and nothing that would read one.

`gratitude_entries` joins the mutations helper's domain-table allow-list, so
every write is logged and reversible like any other. Two items in one turn
write two mutation rows sharing one `utterance_id`, which is what makes
`undo_last` take the whole turn back instead of half of it.

## The `gratitude/` package

Mirrors `pantry/`: reading and writing in one module, the scheduler's sweep in
another, so the scheduler imports the sweep without dragging in anything else.

### `gratitude/entries.py`

    TARGET = 3

    day_for(now, tz_name) -> str          # "YYYY-MM-DD" with the 4am cutoff
    add(conn, utterance_id, items, tz)    # -> (added, day_total)
    for_day(conn, on) -> list[dict]
    recent(conn, tz, days) -> list[dict]  # newest first, grouped by entry_on
    streak(conn, tz) -> int

`day_for` is pure and takes an aware datetime, so the midnight, DST and
timezone cases are testable without a database or a clock.

`streak` walks backwards from today: today counts toward the streak when it has
three, and is skipped without breaking it when it does not. Any earlier day
under three ends the walk. Bounded at 366 days so a long history cannot make
the page slow.

### `gratitude/nudge.py`

`sweep(tz_name)`, called from the scheduler tick inside the same `try/except`
that wraps `expiry.sweep` — design principle 3 says reminders fire when
everything else is broken, and gratitude is very much everything else.

Each 60-second tick, in order:

| Condition | Result |
| :-- | :-- |
| local hour < `GRATITUDE_HOUR` (22) | return, nothing recorded |
| already pushed for this `entry_on` | return |
| day has ≥ 3 entries | return, nothing recorded |
| 0 entries | push "Three things you're grateful for?" |
| 1 entry | push "Two more — what else are you grateful for?" |
| 2 entries | push "One more — what else are you grateful for?" |
| push returned `False` | **not** stamped; retried next tick |

Dedupe is a `heartbeats` row named `gratitude`, whose `detail` holds the
`entry_on` it last pushed for. `_selfcheck` already uses that table for exactly
this — a daily thing that must not repeat — so there is no new table for one
string.

Nothing is stamped unless the push actually landed. `notify.push` returning
`False` means it went nowhere, which with no registered device is the normal
case, and recording it would claim a delivery that did not happen. The pantry
sweep makes the same promise.

The push window is 22:00–23:59 local. **A Mini that is asleep all evening
produces no push and no catch-up**, unlike `reminders`, which deliver up to six
hours late. Deliberate: a gratitude prompt at 8am is about a day that is
already gone, and the entry it would produce belongs to a day you can no longer
remember.

## Fast path

### The tool

```json
{
  "name": "log_gratitude",
  "input_schema": {
    "type": "object",
    "properties": {
      "items": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Each thing they named, as its own string. One to three."
      }
    },
    "required": ["items"]
  }
}
```

One line in the system prompt's tool list, between `add_note` and the pantry
tools: something the user says they are grateful or thankful for →
`log_gratitude`.

**The misroute risk is `add_note`, and it is the real risk in this feature.**
"I'm grateful my sister called" and "note that my sister called" are one word
apart, and the router has no other signal. The tool description names the
trigger words explicitly and the router test set gains gratitude cases, so a
later prompt edit cannot quietly break it.

**The prompt grows and the cache note must stay true.** `CLAUDE.md` records the
router prompt plus eleven tools at 3322 tokens against Haiku 4.5's 4096-token
minimum cacheable prefix. A twelfth tool adds roughly 120. Re-measure with
`count_tokens` and update that number — if it ever crosses 4096 the caching
note becomes wrong, and a stale measurement in that file is worse than none.

### The handler

`handlers.log_gratitude` normalizes (strip, drop empties, raise if nothing
survives), writes each item through `mutations.insert`, and returns a templated
reply — the confirmation is formatted in Python like every other, no second
round trip:

| Day total after the turn | Reply |
| :-- | :-- |
| 1 | "That's one down. Two more when you're ready." |
| 2 | "That's two down. One more when you're ready." |
| 3 | "Three for today. Done." |
| 4+ | "That's a fourth — logged." (fifth, sixth, then "another one") |

## Server

### `GET /gratitude?days=30`

```json
{
  "today": {
    "on": "2026-08-04",
    "target": 3,
    "entries": [{"id": 91, "body": "the sun", "at": "2026-08-04T20:12:00-06:00"}]
  },
  "streak": 9,
  "days": [
    {"on": "2026-08-03", "entries": [{"id": 88, "body": "the rain", "at": "..."}]}
  ]
}
```

One call fills the whole screen. `days` excludes today, which is already broken
out — the top card and the history render differently and merging them would
mean the view re-deriving which group is "now".

`days` defaults to 30 and is clamped, like the other windowed endpoints.

**No delete or edit endpoint.** A mis-heard entry is reversed the way every
other mis-heard thing is: `/undo`, or a swipe on the Activity screen. A second
deletion path would be the only one in the app that bypasses the mutations log.

## iOS

**`JarvisTab.activity` becomes `.gratitude`** — label "Gratitude", symbol
`sparkles`. Same slot in the bar, so the tab count is unchanged.

**`GratitudeView`**: a today card holding the streak and three slots, filled or
`·`, then day-grouped history below it. The empty slots are the point — the
page answers "have I done it?" at a glance, which is what you open it for
between 10pm and bed. A fourth entry appears in the card under the three.

**Activity moves into the Health tab's nav group**, a fourth `NavRow` beside
Ingest health, Inbox and Devices. That group's own comment already calls Health
"the door to the surfaces that have no home of their own", and Activity is now
one: you go looking for it when something you said came out wrong, which is the
same reason you open Health at all. Swipe-to-undo comes across unchanged.

**Tapping the push opens Talk with the mic live.** `AppDelegate` sees
`kind == "gratitude"` in the payload and sets the `LaunchRouter` listening
latch the `StartListening` intent already uses. `RootView` gains an
`onChange` that switches to `.talk` when the latch is set — without it a tap
from any other tab opens the mic on the wrong screen, since today the latch is
only ever set while Talk is already showing.

No notification actions. Snooze and Done mean nothing here; the notification's
whole job is to be tapped.

## Config

| Name | Default | Why it is a knob |
| :-- | :-- | :-- |
| `GRATITUDE_HOUR` | `22` | When the evening ends is a personal fact, and this is one line in `.env` |
| `GRATITUDE_DAY_START` | `4` | Same, for how late "tonight" runs |

`TARGET = 3` is a module constant, not env. Three is the feature, not a
setting; a two-item day would make the page's three slots a lie.

## Testing

| Test | Asserts |
| :-- | :-- |
| `test_day_for_before_cutoff_is_yesterday` | 23:50 → today, 00:30 → yesterday, 07:00 → today |
| `test_day_for_survives_a_dst_boundary` | the spring-forward and fall-back nights land on the right dates |
| `test_streak_counts_consecutive_complete_days` | a clean run |
| `test_incomplete_today_does_not_break_the_streak` | zero entries today, yesterday's streak intact |
| `test_a_gap_ends_the_streak` | a missed day stops the walk |
| `test_logging_three_in_one_turn_writes_three_rows` | and all share one `utterance_id` |
| `test_undo_reverses_a_whole_gratitude_turn` | `/undo` after a two-item turn leaves the day empty |
| `test_a_fourth_entry_is_accepted` | stored, and the day still reads complete |
| `test_sweep_is_silent_when_the_day_is_complete` | no push, nothing stamped |
| `test_sweep_is_silent_before_the_hour` | 21:59 does nothing |
| `test_sweep_pushes_once_per_day` | second tick in the same evening is silent |
| `test_sweep_does_not_stamp_a_failed_push` | `push → False` leaves the heartbeat unset, next tick retries |
| `test_sweep_failure_does_not_break_the_tick` | a raising sweep still fires due reminders |
| `test_gratitude_endpoint_shape` | today, streak and days as documented |
| `ContractTests.testGratitudeResponseDecodes` | the Swift model decodes a canned payload |

The sweep tests inject the clock through the same `_local_date_and_hour` seam
`pantry/expiry.py` uses, and fake `notify.push` — nothing here needs a real
notification.

Routing is the exception: it belongs in `tests/test_utterances.py`, the live
Haiku test set that skips without an API key, because a mocked router proves
nothing about whether Haiku can tell gratitude from a note. Cases: "I'm
grateful for the sun, Emma calling, and the deadline moving" → `log_gratitude`
with three items; "I'm thankful my sister called" → `log_gratitude` with one;
and "note that my sister called" → still `add_note`, which is the assertion
that will catch a prompt edit going wrong.

## Documentation

`CLAUDE.md` gains a **Gratitude** section carrying the four decisions that are
expensive to rediscover: the sweep-not-reminder choice and its reasoning, the
4am cutoff, three-as-target-not-limit, and the streak's treatment of today.
Plus the layout line for `gratitude/`, the `/gratitude` row in the endpoint
table, `log_gratitude` in the router tool table, and the corrected router token
count.

## Known edges, accepted

- **A day nobody was home for gets no push and no catch-up.** See above; a
  morning prompt about last night is worse than silence.
- **A mis-heard entry past the current turn** needs `/undo` at the right moment
  or it stays. Same as every other domain table, and the Activity screen still
  shows what it was.
- **The streak has no grace day.** Miss one evening and it resets to zero. A
  forgiveness rule is a feature to add after living with the plain one, not
  before.
