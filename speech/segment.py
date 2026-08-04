"""Where to cut a reply so the first sound arrives early.

Synthesis runs at roughly 4x realtime on the Mini — a sentence measured 1.0s
of compute for 4.4s of audio. That ratio is the whole reason this file exists:
if the first clause is synthesized and sent on its own, playback starts after
that clause instead of after the whole reply, and every later chunk is
produced faster than the phone consumes the one before it. Playback starts
early and never starves.

The cost is prosody. Each chunk is a separate utterance to Kokoro and gets its
own intonation contour, so a cut in the wrong place sounds like two sentences
glued together. That is why cuts only ever land on punctuation that already
carries a pause, never on a word boundary or a fixed character count: the seam
has to be somewhere the voice was going to slow down anyway.

`TTS_STREAM_CHUNKS=0` turns the whole thing off and goes back to one utterance
per reply, because whether this sounds right is a question for ears.
"""

# Three strengths, because the risk of cutting is not the same at each. A full
# stop ends an utterance the voice was already going to end — cutting there
# costs nothing at any length. A dash or semicolon carries a real pause but
# the phrase continues through it. A comma is the slightest of the three, and
# a short fragment behind one is the most likely to sound clipped.
_SENTENCE = ".!?"
_BREAK = "—–;:"
_SOFT = ","

# Closing punctuation that belongs to the chunk it follows rather than the
# next one: `he said "no." ` cuts after the quote, not between it and the stop.
_TRAILING = ".!?…\"')]»"

# How much text the first chunk has to be worth before a cut of each strength
# is taken. A sentence boundary needs none: "Noted." is a whole utterance and
# sounds like one. "Got it —" at eight characters is still comfortably a
# phrase. Behind a comma, a fragment needs to be long enough to carry itself.
_MIN_FIRST = {_SENTENCE: 0, _BREAK: 8, _SOFT: 24}

# After the first chunk the race is already won: playback has started and
# synthesis is ahead of it. Longer chunks from here mean fewer seams.
_TARGET = 140


def segments(text: str) -> list[str]:
    """Split `text` into chunks to synthesize in order.

    Always returns at least one chunk, and joining the result with a single
    space reproduces the input's words in order — nothing is dropped, and
    nothing is rewritten. A reply with no internal punctuation comes back
    whole, because there is nowhere to cut it that would not be audible.
    """
    text = text.strip()
    if not text:
        return []

    marks = _boundaries(text)
    if not marks:
        return [text]

    chunks: list[str] = []
    start = 0
    for end, kind in marks:
        if end <= start:
            continue
        floor = _TARGET if chunks else _MIN_FIRST[kind]
        if end - start >= floor:
            chunks.append(text[start:end].strip())
            start = end

    # Whatever is left after the last cut. No minimum length: a short tail can
    # only exist because a cut above was already judged worth taking, and the
    # floors are what stop a fragment being stranded in the first place.
    tail = text[start:].strip()
    if tail:
        chunks.append(tail)

    return [chunk for chunk in chunks if chunk] or [text]


def _boundaries(text: str) -> list[tuple[int, str]]:
    """Every place the text could be cut, as (index, strength).

    A mark only counts when whitespace follows it, which is what keeps "5:30"
    and "3.5" and "e.g." in one piece: the punctuation inside them is followed
    by a digit or a letter, so it is not a boundary. End-of-text is excluded —
    cutting there would produce an empty final chunk.
    """
    marks: list[tuple[int, str]] = []
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        kind = next((group for group in _MIN_FIRST if char in group), None)
        if kind is None:
            index += 1
            continue

        after = index + 1
        while after < length and text[after] in _TRAILING:
            after += 1

        if after < length and not text[after].isspace():
            # Punctuation inside a word or a number. Not a pause.
            index += 1
            continue

        while after < length and text[after].isspace():
            after += 1
        if after < length:
            marks.append((after, kind))
        index = max(after, index + 1)

    return marks
