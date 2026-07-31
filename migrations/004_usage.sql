-- Record what each utterance actually cost.
--
-- Principle 5 says latency is a budget measured per hop. Money is the other
-- budget, and until now it was invisible: reconstructing a month's spend meant
-- counting tokens after the fact against a prompt that had since changed.
--
-- Tokens, not dollars. Prices change; token counts are a fact about what
-- happened. /metrics multiplies at read time.
--
-- `model_calls` is separate from a count of utterances because one utterance
-- can be two hops: `query` routes through Haiku, and if no templated answer
-- fits, calls it a second time to turn rows into a sentence. A per-utterance
-- average that quietly folds in a second call is the kind of number you
-- optimize against for a week before noticing.

ALTER TABLE utterances ADD COLUMN input_tokens  INTEGER;
ALTER TABLE utterances ADD COLUMN output_tokens INTEGER;
ALTER TABLE utterances ADD COLUMN model_calls   INTEGER;
