# Answering a report that asked you something

Deep-path reports routinely end by asking for a decision — which vendor, which
date, do you want me to go ahead. Today there is nothing you can do with that
question. This adds a reply: you answer, the job resumes the same Claude Code
session, and it rewrites its report as though it had known the answer all
along.

## Why this shape

**The job is not blocked; it already finished.** `claude -p` runs to completion
and exits. A report that "asks a question" is a `done` row whose `result`
happens to end in one. So replying is not unblocking a waiting process — it is
starting a second run that continues the same conversation. Every decision
below follows from that.

**Resume already exists and is already load-bearing.** `worker/run.py`
generates the session id before the subprocess starts, specifically so a job
that dies mid-run stays resumable, and `_command()` passes `--resume` whenever
`session_id` is set. Voice follow-ups have used it since day one. This feature
is a new way to reach a mechanism that is already here, not a new mechanism.

**One report per task, not a thread of fragments.** The reply re-queues the
job it belongs to rather than inserting a new row. A task you had to answer a
question about is still one task, and the Reports list should not grow a card
every time you say "yes, do that". The alternative — new row per reply, linked
by a parent id — buys a visible transcript nobody asked for at the cost of a
list that no longer maps to things you asked for.

**The rewritten report replaces the old one, and must stand alone.** The
agent is told to end with the complete updated report, self-contained, with no
reference to what it said before. This is the load-bearing half of the design:
because the new report answers the whole task, nothing is lost by overwriting
`result`, and the detail view needs no version history, no diff, and no
"earlier answers" affordance. A report you have to read backwards through three
revisions to understand is worse than one that reads like a finished document.

**No detection of "needs input".** Nothing classifies a result as a question —
not a Haiku call, not a marker the agent is asked to emit. Every finished
report can be replied to. Detection buys a badge and costs either a model call
on every job or a silent failure whenever the agent forgets its marker; the
badge is not worth either. You find out a report wants something from you by
reading it, which you were doing anyway.

## Schema

Migration `010_job_reply.sql`, one column:

```sql
ALTER TABLE jobs ADD COLUMN pending_input TEXT;
```

`pending_input` holds the reply text between the moment you send it and the
moment the resumed run finishes. It cannot be folded into `prompt`: the worker
passes `prompt` as the `-p` argument, and overwriting it would destroy the
original ask — which is exactly what `ReportDetailView` shows under "Asked",
and the only record of what the report is *for*. So `prompt` is immutable for
the life of a job, `pending_input` is transient, and the worker clears it when
the run finishes.

Status stays the existing `queued|running|done|failed`. A job being replied to
is `queued` like any other, which is the whole reason no scheduler, worker, or
list view needs to learn a new state.

## The wrapper

The worker wraps `pending_input` before handing it to the CLI. Wrapping lives
in `worker/run.py` and not in the API so that every surface — reply box,
notification, voice — produces identically-shaped runs; a wrapper applied at
the call site is a wrapper that eventually differs between call sites.

Shape of it:

> The user replied to your last report: "…"
>
> Continue the task with that. When you are finished, end your response with
> the complete updated report, self-contained and standing on its own. It
> replaces your previous report rather than adding to it, so do not refer to
> what you said before or describe what changed.

`run_job` builds the prompt as `pending_input`-wrapped when the column is set,
and as `prompt` otherwise. Nothing else in the worker changes: `--resume` is
already conditional on `session_id`, `_finish` already writes `result`, and the
existing "Job finished" push fires again on the resumed run, which is what you
want — the report changed, you should be told.

## Server

### `POST /jobs/{id}/reply`

```
// request
{"text": "Go with B, and book it for Tuesday"}

// response — the job row, as GET /jobs/{id}
{"id": 27, "status": "queued", "prompt": "Compare the three vendors", ...}
```

Sets `pending_input`, `status='queued'`, `attempts=0`, and clears `error`.

**Resetting `attempts` is not cosmetic.** `MAX_ATTEMPTS` is 2 and counts up
across the life of the row. A job that failed once, retried, and succeeded sits
at `attempts=2`; without the reset, your reply would get zero retries and a
single transient failure would bury it.

**409 if the job is `queued` or `running`.** You cannot resume a session that
is mid-run, and a reply that silently vanished into a job already working would
be the worst possible failure for a feature whose entire job is to let you
answer a question.

404 if there is no such job. A job with no `session_id` — one that failed
before the subprocess started — is still repliable: the run simply starts a
fresh session with the wrapped text, which is a reasonable reading of what you
meant.

### Voice, reconciled

`escalate(is_follow_up)` in `app/main.py` currently inserts a *new* job row
carrying the previous job's `session_id`. It stops doing that and calls the
same path as the reply endpoint against the most recent finished job. One
mechanic regardless of how you answered.

This changes existing behavior deliberately: a spoken follow-up no longer
produces its own report card, it rewrites the report it belongs to. That is
the correct shape — "what about the second one" was never a separate piece of
work — and leaving the two paths divergent would mean the Reports list held two
different kinds of thing depending on how you happened to speak.

If no finished job exists, it falls back to inserting a new job, as today. The
reply text ("Picking up where we left off.") is unchanged.

## iOS

**`ReportDetailView` grows a reply box**, at the foot of the screen under the
result — where you are when you finish reading the question. A text field plus
a mic that reuses the existing `Transcriber`, so speaking the
answer and typing it are the same code path with two ways in. Sending flips the
view to the job's new `queued` state; the list's existing 5-second poll and the
pulsing `StatusDot` already cover watching it rework itself, so there is no new
progress UI.

The box is disabled while the job is `queued` or `running`, matching the
server's 409 rather than discovering it.

**The `JOB` notification gains a reply action.** `PushRegistrar` already
registers a `JOB` category; adding a `UNTextInputNotificationAction` to it gets
an inline reply field on the lock screen for free, and `AppDelegate` routes the
response's `userText` to `POST /jobs/{id}/reply` using the `job_id` already in
the notification payload. This is the surface that matters most in practice: a
report finishes while you are elsewhere, and answering it should not require
opening the app.

## Testing

| Test | Asserts |
| :-- | :-- |
| `test_reply_requeues_the_job` | `pending_input` set, status `queued`, attempts 0, error cleared |
| `test_reply_preserves_the_original_prompt` | `prompt` unchanged after a reply and after the resumed run |
| `test_reply_rejects_a_live_job` | 409 for `queued` and for `running` |
| `test_reply_404s_on_unknown_job` | 404 |
| `test_worker_resumes_with_the_wrapped_reply` | `-p` carries the wrapper and the reply; `--resume` carries the session id |
| `test_worker_clears_pending_input_on_finish` | column is NULL after a successful run |
| `test_worker_keeps_pending_input_on_requeue` | a failed attempt retries the reply, not the original prompt |
| `test_follow_up_resumes_the_previous_job` | no new row; the prior job is re-queued |
| `test_follow_up_with_no_prior_job_inserts_one` | fallback path intact |

Worker tests fake the subprocess, as the existing ones do — nothing here needs
a real `claude` invocation.

## Known edges, accepted

- **An expired Claude Code session.** `--resume` fails, the run retries and
  lands in `failed` with the CLI's message in the detail view. Recoverable by
  asking again from scratch. Pre-empting it would mean tracking session
  lifetime we do not control.
- **A reply sent from a stale notification**, after the job has been replied to
  from somewhere else and is running again, gets the 409. Correct, and the
  phone shows it.
