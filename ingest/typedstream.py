"""Pull the text out of a Messages `attributedBody` blob.

Modern macOS stores message text as an NSArchiver typedstream in
`message.attributedBody` and leaves `message.text` NULL. An importer that
reads `text` alone appears to work on old rows and silently drops most of the
corpus — which is the failure you notice months later, when the assistant
insists someone never texted you.

This is a deliberately narrow reader, not a general typedstream parser. It
finds the NSString payload and returns it. Anything it does not recognise
returns None, because the alternative — raising — would take down an import of
several thousand messages over one malformed row.

Unprivileged by design: `helpers/tccread` holds Full Disk Access and does
nothing but read bytes, so every parsing bug lives here, where it is testable
against a fixture and cannot be reached with elevated permissions.
"""

_MARKER = b"NSString"
# The archive uses 0x81 to introduce a two-byte little-endian length for
# strings longer than 0x80; shorter ones carry their length in one byte.
_LONG = 0x81


def decode(blob: bytes | None) -> str | None:
    if not blob:
        return None

    index = blob.find(_MARKER)
    if index == -1:
        return None

    # Skip the class name, then the small run of type/version bytes that
    # separates it from the payload. The '+' (0x2b) is the type code for the
    # string that follows.
    cursor = blob.find(b"+", index)
    if cursor == -1:
        return None
    cursor += 1

    if cursor >= len(blob):
        return None

    length = blob[cursor]
    cursor += 1
    if length == _LONG:
        if cursor + 2 > len(blob):
            return None
        length = int.from_bytes(blob[cursor : cursor + 2], "little")
        cursor += 2

    payload = blob[cursor : cursor + length]
    if len(payload) != length:
        return None

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None

    text = text.strip("\x00").strip()
    return text or None
