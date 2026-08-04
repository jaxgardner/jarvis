"""The tool definition, checked without spending money.

Whether Haiku actually picks it is a different question and lives in
tests/test_utterances.py, which runs against the live model.
"""

from app import router


def test_the_tool_is_defined_and_takes_a_list():
    tool = next(t for t in router.TOOLS if t["name"] == "log_gratitude")
    items = tool["input_schema"]["properties"]["items"]
    assert items["type"] == "array"
    assert items["items"]["type"] == "string"
    assert tool["input_schema"]["required"] == ["items"]


def test_every_tool_has_a_handler_or_is_handled_in_main():
    """A tool the model can pick with nothing behind it answers 500."""
    from app import handlers
    from app.main import DEEP_TOOLS

    for tool in router.TOOLS:
        assert tool["name"] in handlers.FAST_HANDLERS or tool["name"] in DEEP_TOOLS


def test_the_system_prompt_tells_the_model_when_to_use_it():
    prompt = router.system_prompt("America/Denver")
    assert "log_gratitude" in prompt


# The cacheable-prefix measurement used to live here. It moved to
# tests/test_router_prompt.py when projects landed — it was never about
# gratitude, and its assertion changed from "under 4096" to the invariant that
# survives the prompt growing.
