"""Receipt extraction.

The model's job is narrow on purpose: read pixels, map `GV WHL MLK 1GAL` to a
name and a category. It never sets an expiry date — `shelflife.py` does, and a
human confirms it. These tests pin that boundary as much as they pin parsing.
"""

import pytest

from pantry import extract, shelflife


class FakeBlock:
    type = "tool_use"

    def __init__(self, payload):
        self.input = payload


class FakeResponse:
    def __init__(self, payload):
        self.content = [FakeBlock(payload)]
        self.usage = type("U", (), {"input_tokens": 1200, "output_tokens": 300})()
        self.stop_reason = "tool_use"


@pytest.fixture
def fake_model(monkeypatch):
    """Swap the Anthropic client for one that returns a canned tool call."""
    sent = {}

    def reply_with(payload):
        class FakeMessages:
            def create(self, **kwargs):
                sent.update(kwargs)
                return FakeResponse(payload)

        class FakeClient:
            messages = FakeMessages()

        monkeypatch.setattr(extract, "_CLIENT", FakeClient())
        return sent

    return reply_with


def test_items_come_back_normalized(fake_model):
    fake_model(
        {
            "store": "King Soopers",
            "purchased_on": "2026-07-31",
            "total_cents": 4213,
            "items": [
                {
                    "raw_text": "GV WHL MLK 1GAL",
                    "name": "whole milk",
                    "category": "milk",
                    "quantity": 1,
                    "unit": "gal",
                }
            ],
        }
    )
    result = extract.read_receipt(b"jpegbytes", "image/jpeg", "2026-07-31")

    assert result["store"] == "King Soopers"
    assert result["purchased_on"] == "2026-07-31"
    assert result["total_cents"] == 4213
    item = result["items"][0]
    assert item["raw_text"] == "GV WHL MLK 1GAL"
    assert item["name"] == "whole milk"
    assert item["category"] == "milk"
    assert item["location"] == "fridge", "filled from DEFAULT_LOCATION"


def test_the_model_is_never_asked_for_an_expiry_date():
    """The single most important property of this module. If a date ever
    appears in the tool schema, dates stop being auditable."""
    schema = extract.EXTRACT_TOOL["input_schema"]
    item_props = schema["properties"]["items"]["items"]["properties"]
    assert not any("expir" in key for key in item_props), item_props
    assert not any("expir" in key for key in schema["properties"])


def test_the_category_enum_is_the_shelf_life_table(fake_model):
    """A category the table doesn't know is a category with no date. Binding
    the enum to CATEGORIES is what stops the two drifting apart."""
    item_props = extract.EXTRACT_TOOL["input_schema"]["properties"]["items"]["items"]
    assert item_props["properties"]["category"]["enum"] == shelflife.CATEGORIES


def test_an_unknown_category_is_dropped_rather_than_stored(fake_model):
    """The enum constrains the model but does not bind it. A hallucinated
    category must not reach the database, where it would silently mean 'no
    date' while looking like a real classification."""
    fake_model(
        {
            "items": [
                {"raw_text": "XYZ", "name": "mystery", "category": "plutonium"}
            ]
        }
    )
    result = extract.read_receipt(b"x", "image/jpeg", "2026-07-31")
    assert result["items"][0]["category"] is None


def test_an_invalid_location_falls_back_to_the_category_default(fake_model):
    fake_model(
        {
            "items": [
                {
                    "raw_text": "MLK",
                    "name": "milk",
                    "category": "milk",
                    "location": "garage",
                }
            ]
        }
    )
    result = extract.read_receipt(b"x", "image/jpeg", "2026-07-31")
    assert result["items"][0]["location"] == "fridge"


def test_an_item_with_no_name_is_dropped(fake_model):
    """`pantry_items.name` is NOT NULL. A nameless line is a misread, and
    inserting it would 500 the upload instead of costing one missing row."""
    fake_model(
        {
            "items": [
                {"raw_text": "?????", "name": ""},
                {"raw_text": "MLK", "name": "milk", "category": "milk"},
            ]
        }
    )
    result = extract.read_receipt(b"x", "image/jpeg", "2026-07-31")
    assert [i["name"] for i in result["items"]] == ["milk"]


def test_a_missing_purchase_date_falls_back_to_today(fake_model):
    """A receipt whose date didn't read is still a receipt. Today is the only
    honest default — you are standing in the kitchen with the groceries."""
    fake_model({"items": []})
    result = extract.read_receipt(b"x", "image/jpeg", "2026-07-31")
    assert result["purchased_on"] == "2026-07-31"


def test_a_malformed_purchase_date_falls_back_to_today(fake_model):
    fake_model({"purchased_on": "07/31/26", "items": []})
    result = extract.read_receipt(b"x", "image/jpeg", "2026-07-31")
    assert result["purchased_on"] == "2026-07-31"


def test_the_image_is_sent_as_base64_with_its_media_type(fake_model):
    sent = fake_model({"items": []})
    extract.read_receipt(b"jpegbytes", "image/png", "2026-07-31")
    content = sent["messages"][0]["content"]
    image = next(block for block in content if block["type"] == "image")
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"


def test_the_call_forces_a_tool_so_there_is_never_free_text(fake_model):
    sent = fake_model({"items": []})
    extract.read_receipt(b"x", "image/jpeg", "2026-07-31")
    assert sent["tool_choice"] == {"type": "any"}
    assert sent["model"] == "claude-haiku-4-5"


def test_a_response_with_no_tool_call_raises(monkeypatch):
    """tool_choice=any makes this unreachable in practice. Treat it as a bug
    rather than silently returning an empty receipt, which would look to the
    user like a receipt with nothing on it."""

    class Empty:
        content = []
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            return Empty()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(extract, "_CLIENT", FakeClient())
    with pytest.raises(RuntimeError):
        extract.read_receipt(b"x", "image/jpeg", "2026-07-31")
