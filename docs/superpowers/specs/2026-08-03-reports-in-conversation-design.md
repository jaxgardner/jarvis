# Reports you can talk about

Two things you cannot do by voice today: answer a *particular* report, and ask
what one said. Both come from the same gap — the router has never known your
reports exist. Show it the last ten and both fall out.

## Why this shape

**The router is the only thing that can resolve "the vendor one".** It is
already the component that turns speech into a structured choice, and it
already resolves the hard case of this kind: dates. The system prompt carries a
`CALENDAR` block precisely because the model is bad at deriving dates and good
at copying them from a table. Reports are the same problem with different
nouns, and deserve the same answer rather than a fuzzy match bolted on after
the fact.

**Naming a report is strictly better than guessing at one.** `is_follow_up`
resolves to "the most recent finished job", which is right often enough to feel
fine and wrong in exactly the case you'd care about — answering the report from
this morning after asking for something else at lunch. Once the router can
return a `job_id`, the boolean is a worse version of the same signal, so it
goes rather than sitting alongside.

**Talking about a report is a `query`, not a mode.** `handlers.query` already
gathers `EVENT:`, `NOTE:` and `EMAIL:` lines and hands them to `router.answer()`
for a spoken sentence. A report is one more line in that list. This keeps the
composition — "what did the vendor report and the landlord's email both say
about the deadline" is one question with one answer — and adds no handler, no
tool, and no second path to maintain.

**A summary, not the report.** Reports run to tens of kilobytes; a vendor
comparison with three tables would dominate the context window and the latency
budget for a question the summary answers in a sentence. The cost is real and
worth stating plainly: **a question about a detail the summary dropped will be
answered "it didn't say".** That is the accepted trade. The escape hatch is the
report itself, on screen, which is where detail was always going to live.

**Summaries are written by a model, not by the agent.** Asking the deep agent
to emit a delimited summary block would be free, and would fail silently the
first time it forgot — the same reasoning that kept a "needs input" marker out
of the reply feature. One Haiku call at finish time is reliable, tunable in one
place, and cheap enough to ignore.

## Schema

Migration `011_job_summary.sql`:

```sql
ALTER TABLE jobs ADD COLUMN summary TEXT;
```

NULL is a normal, permanent state, not a pending one: every job that finished
before this shipped has no summary and never will, and a summarization call
that fails leaves NULL behind. `query` handles NULL by falling back to the
first 1500 characters of `result`. That fallback is what makes a backfill
script unnecessary — old reports answer questions on day one, slightly worse.

## The REPORTS block

`app/router.py` builds it alongside the existing calendar block: the last 10
jobs with `status IN ('done','failed')`, newest first, as

```
REPORTS — the user's recent deep reports. Refer to one by its id.
27  Compare the three vendors
26  Research standing desks under $600
24  Draft the lease renewal email
```

Prompts are truncated to 60 characters. Ten rows costs roughly 150–200 tokens,
taking the router prompt from a measured 3322 to about 3500 — still under
Haiku 4.5's 4096-token minimum cacheable prefix, so the "prompt caching does
not fire here" note in CLAUDE.md remains true and no budgeting changes.

When there are no reports the block is omitted entirely rather than rendered
empty, so a fresh install does not spend tokens telling the model about
nothing.

## Router tools

### `escalate` gains `job_id`, loses `is_follow_up`

| Parameter | Meaning |
| :-- | :-- |
| `restated_task` | Unchanged. The task, or the answer to a question. |
| `job_id` | The report this continues. Absent means new work. |

Server-side, `job_id` routes to `handlers.reply_to_job` — the same function the
reply box and the notification action call, so a spoken answer and a typed one
are indistinguishable by the time they reach the database.

Its three outcomes each get a templated reply:

| Outcome | Reply |
| :-- | :-- |
| `ok` | "Picking up where we left off. I'll ping you." |
| `live` | "That one's still working. I'll leave it be." |
| `missing` | Falls through to inserting a new job, with the normal reply. |

The `live` case is why this is not simply "insert a job with that session id":
answering a report that is already running should tell you so, not quietly
start a second piece of work you did not ask for.

`handlers.resume_latest_job` is **deleted**. It existed only to guess which
report was meant, and nothing guesses any more.

### `query` gains `job_id`

Optional. When set, `handlers.query` prepends one context line:

```
REPORT (Compare the three vendors): <summary, or result truncated to 1500>
```

Everything else about `query` is untouched — the templated answers still short-
circuit first, notes and email are still searched, and `router.answer()` still
speaks the result.

The line is omitted entirely when there is nothing to put in it: a `job_id`
naming no job, or naming a failed one that has neither summary nor result. The
question is still answered from whatever else was found, because a question is
still a question, and an empty `REPORT ():` line would invite the model to
invent what belongs there.

## Summarizing

New module `app/reports.py`, one public function:

```python
def summarize(result: str) -> str | None
```

It calls `claude-haiku-4-5` through `router._client()` — the connection and its
keepalive already live there — with a prompt asking for roughly 1000 characters
of plain prose covering what was asked, what was found, and any numbers or
names that matter. Plain text, since the destination is a TTS engine.

`worker.run_job` calls it after a successful `_finish`, before `notify.push`.
Three properties, all load-bearing:

- **It cannot fail the job.** Every exception is caught. The report is already
  saved and the push is already owed; a summarizer that could swallow either
  would be worse than no summarizer.
- **It is time-limited** to 30 seconds. A hung call must not hold the worker,
  which drains a queue on a 30-second `StartInterval`.
- **It runs on `done` only.** A failed job has no result to summarize.

`worker._summarize()` — the free, sentence-based function that writes the push
notification — is untouched and keeps that job. The two exist for different
readers: one line you glance at on a lock screen, versus a paragraph a model
reads to answer a question.

**This puts a metered API call in the deep path**, which until now ran entirely
on the Claude Code subscription. It is Haiku against a few thousand tokens, so
fractions of a cent per job, and deep jobs are rare — but it contradicts a
stated property of the system and belongs in CLAUDE.md rather than being
rediscovered from a bill. Like receipt extraction, it stays out of `/metrics`:
that block is per-utterance, and a summary has no utterance behind it.

## Testing

| Test | Asserts |
| :-- | :-- |
| `test_reports_block_lists_recent_jobs` | ids and truncated prompts present, newest first |
| `test_reports_block_is_omitted_when_there_are_none` | no empty header in the prompt |
| `test_reports_block_caps_at_ten` | an eleventh job does not appear |
| `test_escalate_with_a_job_id_resumes_that_report` | that job re-queued; no new row |
| `test_escalate_without_a_job_id_starts_new_work` | a row is inserted |
| `test_escalate_on_a_live_job_says_so_and_does_nothing` | reply names it; job untouched |
| `test_escalate_with_an_unknown_job_id_starts_new_work` | falls through to insert |
| `test_query_with_a_job_id_puts_the_summary_in_context` | `REPORT (…):` line reaches `router.answer` |
| `test_query_falls_back_to_the_result_when_there_is_no_summary` | truncated result used |
| `test_query_with_an_unknown_job_id_still_answers` | no line, no crash |
| `test_summarize_returns_none_when_the_model_fails` | exception swallowed, None returned |
| `test_a_failed_summary_does_not_fail_the_job` | job stays `done`, push still sent |
| `test_summaries_are_only_written_for_finished_jobs` | no call on the failure path |

The model call is stubbed throughout; nothing here reaches the network.

## Known edges, accepted

- **The summary is a lossy copy.** Ask about a number it dropped and you get
  "it didn't say" rather than the number, which is in `result` on screen. This
  is the whole trade and re-litigating it means adding retrieval.
- **A report older than the tenth is unreachable by voice.** It is still on the
  Reports screen with a reply box. Ten is a guess; it is one constant.
- **The router may name the wrong id.** It is copying from a table rather than
  matching text, so this should be rare — but "answer the desk one" with two
  desk reports is genuinely ambiguous, and it will pick one.
