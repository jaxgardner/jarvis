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
    kwargs.setdefault("messages", [{"role": "user", "content": "hello"}])
    return client.messages.count_tokens(model=router.MODEL, **kwargs).input_tokens


def has_cache_control() -> bool:
    return any("cache_control" in entry for entry in router.TOOLS)


def test_a_declared_cache_actually_caches(client):
    """If cache_control is declared, it must produce a real cache read.

    This asserts the *behaviour*, not the token count, because the token count
    turned out not to predict it. The tools measure ~4199 tokens against
    Haiku 4.5's documented 4096 floor — 103 over — and caching still does not
    fire: probed directly, both cache counters come back 0. Padded to 7240
    tokens the same probe caches 6912 immediately. The cache measures a
    smaller prefix than `count_tokens` reports for the request, so a
    comparison against the published floor is not evidence of anything.

    A marker that silently no-ops is worse than no marker: it reads as a
    working optimization forever. Two requests are the only way to know, and
    they are only spent when someone has opted in.
    """
    if not has_cache_control():
        pytest.skip("no cache_control declared — nothing to verify")

    body = dict(
        model=router.MODEL,
        max_tokens=32,
        tools=router.TOOLS,
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": "remind me to call the dentist"}],
    )
    client.messages.create(**body)  # writes the cache
    usage = client.messages.create(**body).usage

    assert usage.cache_read_input_tokens > 0, (
        "cache_control is declared but no tokens were served from cache — "
        "the prefix is below the effective minimum and the marker does nothing"
    )


def test_the_prompt_size_is_recorded(client):
    """Numbers printed so the CLAUDE.md note can be kept accurate, with no
    assertion about them — the size stopped being load-bearing the moment it
    turned out not to predict whether caching fires.

    `tools alone` is the marginal cost of the tools block: measured against a
    messages-only baseline, because `count_tokens(tools=...)` also carries the
    message and its overhead.
    """
    messages = [{"role": "user", "content": "hello"}]
    baseline = count(client, messages=messages)
    with_tools = count(client, messages=messages, tools=router.TOOLS)
    whole = count(
        client,
        messages=messages,
        system=router.system_prompt("America/Denver"),
        tools=router.TOOLS,
    )
    print(f"tools alone:         {with_tools - baseline} tokens")
    print(f"whole router prompt: {whole} tokens (floor {CACHE_FLOOR}, does not cache)")
    assert whole > 0
