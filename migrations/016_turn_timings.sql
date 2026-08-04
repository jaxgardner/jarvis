-- The turn, as opposed to /say.
--
-- latency_ms times the endpoint and misses roughly 1550ms of what the user
-- actually waits through: 800ms of endpointer before /say is called at all,
-- and ~640ms of synthesis after it returns. Measured 2026-08-04, a one-call
-- utterance is 1410ms of /say inside a ~3000ms turn. Optimising the smaller
-- number is how a system gets faster on paper and no faster in the room.
--
-- Both nullable, permanently. The Shortcut client has no microphone and so
-- has no turn to report; that is a client without a mic rather than a
-- measurement that went missing.
ALTER TABLE utterances ADD COLUMN turn_ms INTEGER;

-- Server-side hop breakdown as JSON: router, handler, synth-start. Written by
-- _say, returned as Server-Timing, and kept so a slow turn can be attributed
-- after the fact rather than reproduced.
ALTER TABLE utterances ADD COLUMN timings TEXT;
