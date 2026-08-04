-- A summary of a report, for talking about it out loud.
--
-- Reports run to tens of kilobytes. Handing one to the answering model would
-- spend the whole context window and the latency budget on a question a
-- sentence answers, so voice reads this instead.
--
-- Written by one Haiku call when the run finishes. NULL is a normal and
-- permanent state, not a pending one: every job that finished before this
-- shipped has no summary and never will, and a summarization call that fails
-- leaves NULL behind. `handlers.query` falls back to the first 1500
-- characters of `result`, which is what makes a backfill script unnecessary.
--
-- The trade is accepted and worth restating: a question about a detail the
-- summary dropped is answered "it didn't say". The detail is in `result`, on
-- screen, which is where detail was always going to live.

ALTER TABLE jobs ADD COLUMN summary TEXT;
