"""The tool definition, checked without spending money.

Whether Haiku actually picks it is a different question and lives in
tests/test_utterances.py, which runs against the live model.
"""

import anthropic
import pytest

from app import config, router


def test_the_tool_is_defined_and_takes_a_list():
    tool = next(t for t in router.TOOLS if t["name"] == "log_gratitude")
    items = tool["input_schema"]["properties"]["items"]
    assert items["type"] == "array"
    assert items["items"]["type"] == "string"
    assert tool["input_schema"]["required"] == ["items"]


def test_every_tool_has_a_handler_or_is_escalate():
    """A tool the model can pick with nothing behind it answers 500."""
    from app import handlers

    for tool in router.TOOLS:
        assert tool["name"] == "escalate" or tool["name"] in handlers.FAST_HANDLERS


def test_the_system_prompt_tells_the_model_when_to_use_it():
    prompt = router.system_prompt("America/Denver")
    assert "log_gratitude" in prompt


def test_the_prompt_still_fits_under_the_cacheable_prefix():
    """CLAUDE.md records the router prompt as under Haiku's 4096-token minimum
    cacheable prefix, and 'caching does not fire here' is a documented fact
    other decisions lean on. If this fails, the doc needs updating — not this
    number.

    Skipped without an API key: count_tokens is a network call.
    """
    if not config.configured()["anthropic_api_key"]:
        pytest.skip("needs ANTHROPIC_API_KEY (count_tokens is a network call)")

    client = anthropic.Anthropic(api_key=config.anthropic_api_key())
    counted = client.messages.count_tokens(
        model=router.MODEL,
        system=router.system_prompt("America/Denver"),
        tools=router.TOOLS,
        messages=[{"role": "user", "content": "hello"}],
    )
    print(f"router prompt: {counted.input_tokens} tokens")
    assert counted.input_tokens < 4096
