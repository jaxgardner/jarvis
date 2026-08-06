"""attributedBody carries the message text; `text` is usually NULL.

Real captured bytes, not a hand-written approximation — a fixture you wrote
yourself only proves the decoder matches what you imagined the format was.
"""

from pathlib import Path

import pytest

from ingest import typedstream

FIXTURE = Path(__file__).parent / "fixtures" / "attributed_body.hex"


@pytest.mark.skipif(not FIXTURE.exists(), reason="no captured attributedBody fixture")
def test_decodes_real_blob():
    blob = bytes.fromhex(FIXTURE.read_text().strip())
    text = typedstream.decode(blob)
    assert text
    assert "\x00" not in text


def test_empty_blob_is_none():
    assert typedstream.decode(b"") is None


def test_garbage_is_none_not_an_exception():
    """A blob this decoder does not understand must not take down an import
    of six thousand messages."""
    assert typedstream.decode(b"\x01\x02\x03not a typedstream") is None


def test_plain_marker_payload():
    """The shape the decoder keys on: an NSString marker, a length byte, then
    UTF-8. Synthetic, so the parser's contract is pinned even when the real
    fixture is absent on a fresh clone."""
    blob = (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01\x40\x84\x84\x84"
        b"\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92"
        b"\x84\x84\x84\x08NSString\x01\x94\x84\x01\x2b\x05hello\x86"
    )
    assert typedstream.decode(blob) == "hello"
