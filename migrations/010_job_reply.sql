-- Replying to a report that asked you something.
--
-- Deep-path reports routinely end in a question — which vendor, which date,
-- shall I go ahead. The answer re-queues the *same* job against its existing
-- Claude Code session rather than inserting a new row: a task you had to
-- answer a question about is still one task, and the Reports list should not
-- grow a card every time you say "yes, do that".
--
-- `pending_input` cannot be folded into `prompt`. The worker passes `prompt`
-- as the -p argument, so writing the reply there would destroy the original
-- ask — which is what the detail view shows under "Asked" and the only
-- record of what the report is for. So `prompt` is immutable for the life of
-- a job, and this column is transient: set when you reply, cleared by the
-- worker when the run finishes.
--
-- No new status. A job being replied to is `queued` like any other, which is
-- why no worker, scheduler or list view has to learn a new state.

ALTER TABLE jobs ADD COLUMN pending_input TEXT;
