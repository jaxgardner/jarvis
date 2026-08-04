"""Where a reply gets cut.

Two properties matter and they pull against each other. The first chunk must
be short, because it is what the phone waits for. And every cut must land on
punctuation that already carried a pause, because each chunk becomes its own
utterance with its own intonation — a cut anywhere else is audible.

The invariant underneath both: this splits, it never edits. Rejoining the
pieces with a single space gives back the reply.
"""

from speech.segment import segments


def rejoined(text: str) -> str:
    return " ".join(segments(text))


def test_a_short_lead_in_becomes_its_own_chunk():
    """The common shape of a confirmation, and the reason this exists.

    "Got it —" is a fifth of a second to synthesize where the whole sentence
    is a second, so playback starts almost immediately and the rest is
    produced faster than it is consumed.
    """
    assert segments("Got it — I'll remind you to call the dentist tomorrow at nine.") == [
        "Got it —",
        "I'll remind you to call the dentist tomorrow at nine.",
    ]


def test_a_reply_with_nowhere_safe_to_cut_stays_whole():
    """No punctuation means no pause, and inventing one would be audible."""
    text = "I don't have milk in the pantry but I've added it to the list"

    assert segments(text) == [text]


def test_sentences_split():
    assert segments("Noted. Anything else?") == ["Noted.", "Anything else?"]


def test_a_comma_needs_to_earn_its_cut():
    """A comma is a slighter pause than a dash, so a fragment behind one has
    to be long enough not to sound clipped."""
    assert segments("Sure, I'll take care of that this afternoon.") == [
        "Sure, I'll take care of that this afternoon."
    ]
    assert segments(
        "Before you leave for the airport, remember to lock the back door."
    ) == ["Before you leave for the airport,", "remember to lock the back door."]


def test_punctuation_inside_words_and_numbers_is_not_a_pause():
    """5:30, 3.5 and e.g. all contain marks this cuts on elsewhere. What makes
    them safe is the rule that a boundary needs whitespace after it."""
    for text in (
        "It's at 5:30.",
        "That's 3.5 miles away.",
        "Bring something warm, e.g. a coat, tomorrow morning.",
    ):
        assert rejoined(text) == text

    assert segments("It's at 5:30.") == ["It's at 5:30."]


def test_a_short_final_sentence_stands_on_its_own():
    """It is short, but it is a whole sentence — it reads as one either way,
    and by the time it is reached playback has long since started."""
    assert segments(
        "The dentist moved your appointment to Friday afternoon at two. Thanks."
    ) == [
        "The dentist moved your appointment to Friday afternoon at two.",
        "Thanks.",
    ]


def test_later_chunks_are_long_because_the_race_is_already_won():
    """Only the first chunk is latency-critical. After playback has started,
    fewer seams beats smaller pieces."""
    text = (
        "You have three things tomorrow. Coffee with Priya at nine, "
        "the dentist at eleven, and a call with the landlord at four. "
        "The dentist is the one that moved, so check the address."
    )

    chunks = segments(text)

    assert len(chunks[0]) < 40
    assert all(len(chunk) >= 40 for chunk in chunks[1:])
    assert rejoined(text) == text


def test_nothing_is_dropped_or_rewritten():
    for text in (
        "Got it — milk, eggs, and bread are on the list.",
        "Done: the reminder is set; you'll hear from me at nine.",
        "Wait — really?! Okay.",
        "You're out of milk — and it's 5:30.",
    ):
        assert rejoined(text) == text


def test_empty_text_produces_nothing_to_say():
    assert segments("") == []
    assert segments("   ") == []
