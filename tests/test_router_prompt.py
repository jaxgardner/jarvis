"""What the router prompt costs, measured rather than assumed.

CLAUDE.md makes a claim about prompt caching that other decisions lean on.
This is the test that keeps the claim honest as the prompt grows.

Skipped without an API key: count_tokens is a network call.
"""

import anthropic
import pytest

from app import config, router

pytestmark = pytest.mark.skipif(
    not config.configured()["anthropic_api_key"],
    reason="needs ANTHROPIC_API_KEY (count_tokens is a network call)",
)

# Haiku 4.5's minimum cacheable prefix.
CACHE_FLOOR = 4096


@pytest.fixture(scope="module")
def client():
    return anthropic.Anthropic(api_key=config.anthropic_api_key())


def count(client, **kwargs) -> int:
    return client.messages.count_tokens(
        model=router.MODEL,
        messages=[{"role": "user", "content": "hello"}],
        **kwargs,
    ).input_tokens


def has_cache_control() -> bool:
    return any("cache_control" in entry for entry in router.TOOLS)


def test_caching_is_either_impossible_or_switched_on(client):
    """The invariant, not the number.

    Either the cacheable prefix is under the floor and nothing can be cached —
    in which case declaring cache_control is a lie in the code — or it is over
    and caching should be turned on. Both halves are failures worth catching.

    Note what the prefix actually is: the cache order is tools, then system,
    then messages, and the system prompt carries the current datetime in its
    third line. Everything from that line on differs on every call, so only
    the TOOLS block is ever cacheable, however large the system prompt grows.
    """
    tools_only = count(client, tools=router.TOOLS)
    print(f"tools alone: {tools_only} tokens (floor {CACHE_FLOOR})")

    if tools_only < CACHE_FLOOR:
        assert not has_cache_control(), (
            "cache_control is declared on a prefix too small to cache"
        )
    else:
        assert has_cache_control(), (
            f"tools reached {tools_only} tokens — caching can fire now; "
            "add cache_control to the last tool definition"
        )


def test_the_whole_prompt_is_recorded(client):
    """Not an assertion about size — a number printed so the CLAUDE.md note can
    be kept accurate. The prompt grew when projects landed; this is where you
    read the new figure."""
    whole = count(
        client,
        system=router.system_prompt("America/Denver"),
        tools=router.TOOLS,
    )
    print(f"whole router prompt: {whole} tokens")
    assert whole > 0
