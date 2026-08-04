-- Strip the invisible preheader padding out of stored Gmail snippets.
--
-- Marketing senders pad their preview text with zero-width characters so the
-- inbox shows the hook and nothing after it. Gmail returns them inside
-- `snippet`, `email_messages` stored them verbatim, and from there they went
-- into the context handed to `router.answer` and into the morning brief's
-- summarize call.
--
-- They are invisible but they are not free. Each is its own codepoint outside
-- the tokenizer's common set and costs two or three tokens. Measured on one
-- real morning: 611 padding characters were 1248 tokens, 66% of the entire
-- context, buying nothing that a reader or a model can see.
--
-- `ingest.gmail.clean_snippet` stops new ones arriving. This cleans what is
-- already stored, because `prune` only ages rows out on RETENTION_DAYS and
-- the cost is being paid on every question asked before then.
--
-- Listed by codepoint rather than by category, because SQLite has neither
-- regex nor Unicode tables. This is the set that actually appears in the
-- wild: the zero-width joiners and spaces, the directional marks, the BOM,
-- the soft hyphen, and U+034F, which is a combining mark rather than a
-- format character and so is the one a `Cf`-only filter would miss.

UPDATE email_messages
   SET snippet = replace(replace(replace(replace(replace(replace(replace(
                 replace(replace(replace(snippet,
                 char(0x00AD), ''),   -- SOFT HYPHEN
                 char(0x034F), ''),   -- COMBINING GRAPHEME JOINER
                 char(0x200B), ''),   -- ZERO WIDTH SPACE
                 char(0x200C), ''),   -- ZERO WIDTH NON-JOINER
                 char(0x200D), ''),   -- ZERO WIDTH JOINER
                 char(0x200E), ''),   -- LEFT-TO-RIGHT MARK
                 char(0x200F), ''),   -- RIGHT-TO-LEFT MARK
                 char(0x2060), ''),   -- WORD JOINER
                 char(0xFEFF), ''),   -- ZERO WIDTH NO-BREAK SPACE
                 char(0x00A0), ' ')   -- NO-BREAK SPACE, kept as a real space
 WHERE snippet IS NOT NULL;

-- The padding arrives interleaved with real spaces, which are left behind in
-- runs once it is gone. Four passes collapses runs of up to 16.
UPDATE email_messages SET snippet = replace(snippet, '  ', ' ') WHERE snippet LIKE '%  %';
UPDATE email_messages SET snippet = replace(snippet, '  ', ' ') WHERE snippet LIKE '%  %';
UPDATE email_messages SET snippet = replace(snippet, '  ', ' ') WHERE snippet LIKE '%  %';
UPDATE email_messages SET snippet = replace(snippet, '  ', ' ') WHERE snippet LIKE '%  %';

UPDATE email_messages SET snippet = trim(snippet) WHERE snippet IS NOT NULL;

-- A snippet that was nothing but padding is not an empty string, it is the
-- absence of a snippet. `to_row` makes the same call.
UPDATE email_messages SET snippet = NULL WHERE snippet = '';
