"""Receipt photo -> line items. One forced-tool vision call.

The model's job is deliberately narrow: read the pixels and map `GV WHL MLK
1GAL` to a name and a category from a fixed list. It is never asked for an
expiry date. Dates come from `shelflife.py` and are confirmed by a human,
which is what makes them auditable — a wrong date is one table edit, not a
prompt change.

Same shape as `ingest/gmail.py`'s extractor: `tool_choice: {"type": "any"}`,
so there is always structured output and never free text to parse.

Nothing here validates that the extraction is *correct*. That is the review
screen's job, and it is the only thing standing between a misread receipt and
the inventory. What this module does guarantee is that whatever comes back is
*insertable*: every item has a name, a real category or None, and a real
location.
"""

import base64
from datetime import date

import anthropic

from app import config, usage
from pantry import shelflife

EXTRACT_TOOL = {
    "name": "record_receipt",
    "description": "Record every line item on the grocery receipt in the image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "store": {"type": "string", "description": "Store name, if legible."},
            "purchased_on": {
                "type": "string",
                "description": "Date on the receipt as YYYY-MM-DD. Omit if illegible.",
            },
            "total_cents": {
                "type": "integer",
                "description": "Receipt total in cents, e.g. 4213 for $42.13.",
            },
            "items": {
                "type": "array",
                "description": "One entry per purchased line item.",
                "items": {
                    "type": "object",
                    "properties": {
                        "raw_text": {
                            "type": "string",
                            "description": (
                                "The line exactly as printed, abbreviations and "
                                "all, e.g. 'GV WHL MLK 1GAL'."
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "The item in plain English, e.g. 'whole milk'. "
                                "Expand the abbreviations."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": shelflife.CATEGORIES,
                            "description": (
                                "Closest category from the list. Use 'other' "
                                "only when nothing fits."
                            ),
                        },
                        "quantity": {"type": "number"},
                        "unit": {
                            "type": "string",
                            "description": "e.g. 'gal', 'lb', 'oz', 'ct'.",
                        },
                        "location": {
                            "type": "string",
                            "enum": list(shelflife.LOCATIONS),
                            "description": "Where it will be stored.",
                        },
                    },
                    "required": ["raw_text", "name", "category"],
                },
            },
        },
        "required": ["items"],
    },
}

_SYSTEM = """\
You are reading a photograph of a grocery receipt.

Record every purchased line item. Grocery receipts abbreviate aggressively — \
expand them: 'GV WHL MLK 1GAL' is whole milk, 'BNLS SKNLS CHKN BRST' is \
boneless skinless chicken breast.

Skip anything that is not a purchased item: subtotals, tax, the total, \
payment lines, coupons, savings lines, loyalty messages, store address and \
phone, and the barcode footer.

If part of the receipt is illegible, record what you can read and omit the \
rest. Do not invent items to fill gaps.

Today is {today}.\
"""

_CLIENT: anthropic.Anthropic | None = None


def _client() -> anthropic.Anthropic:
    """One client for the process lifetime — same reasoning as the router's."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=config.anthropic_api_key())
    return _CLIENT


def _clean_item(raw: dict) -> dict | None:
    """Normalize one extracted line, or None if it is unusable.

    Returning None rather than raising is deliberate: one misread line should
    cost one missing row in a screen you are about to review anyway, not a 500
    on the upload that loses the other twenty-nine.
    """
    name = str(raw.get("name") or "").strip()
    if not name:
        # `pantry_items.name` is NOT NULL, and a nameless line is a misread.
        return None

    # The enum constrains the model but does not bind it. A category the table
    # does not know must not reach the database, where it would silently mean
    # "no date" while looking like a real classification.
    category = raw.get("category")
    if category not in shelflife.CATEGORIES:
        category = None

    location = raw.get("location")
    if location not in shelflife.LOCATIONS:
        location = shelflife.DEFAULT_LOCATION.get(category, "pantry")

    quantity = raw.get("quantity")
    try:
        quantity = float(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        quantity = None

    return {
        "raw_text": str(raw.get("raw_text") or "").strip() or None,
        "name": name,
        "category": category,
        "quantity": quantity,
        "unit": str(raw.get("unit") or "").strip() or None,
        "location": location,
    }


def _clean_date(value, fallback: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        # A receipt whose date didn't read is still a receipt, and today is
        # the only honest default — you are standing in the kitchen with the
        # groceries.
        return fallback


def read_receipt(image_bytes: bytes, media_type: str, today: str) -> dict:
    """One vision call. Returns normalized, insertable receipt data."""
    encoded = base64.standard_b64encode(image_bytes).decode("ascii")
    response = _client().messages.create(
        model=config.PANTRY_VISION_MODEL,
        max_tokens=4096,
        system=_SYSTEM.format(today=today),
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "any"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": encoded,
                        },
                    },
                    {"type": "text", "text": "Record every item on this receipt."},
                ],
            }
        ],
    )
    usage.record(response.usage)

    payload = None
    for block in response.content:
        if block.type == "tool_use":
            payload = dict(block.input)
            break
    if payload is None:
        # tool_choice=any makes this unreachable in practice. Raising beats
        # returning an empty receipt, which would look to the user like a
        # receipt with nothing on it.
        raise RuntimeError(
            f"extractor returned no tool_use (stop_reason={response.stop_reason})"
        )

    total = payload.get("total_cents")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None

    items = [
        cleaned
        for cleaned in (_clean_item(raw) for raw in payload.get("items") or [])
        if cleaned is not None
    ]
    return {
        "store": str(payload.get("store") or "").strip() or None,
        "purchased_on": _clean_date(payload.get("purchased_on"), today),
        "total_cents": total,
        "items": items,
    }
