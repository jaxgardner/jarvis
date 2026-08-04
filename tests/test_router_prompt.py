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
    """Anywhere in the prefix — the marker moved from the tools to the static
    system block when the prompt was split, and a check that only knew its old
    home would skip silently and assert nothing."""
    blocks = router.system_blocks("America/Denver")
    return any("cache_control" in entry for entry in router.TOOLS) or any(
        "cache_control" in block for block in blocks
    )


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
        system=router.system_blocks("America/Denver"),
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


def test_the_cached_block_carries_nothing_that_can_change():
    """The whole optimization rests on the first block being byte-stable.

    A single live value in it — a date, a project name, the time — would make
    the prefix differ on every call, and the failure is silent: the marker
    stays, the cache never reads, and it reads as a working optimization
    forever. Needs no API key; this is a property of the strings.
    """
    a = router.system_blocks("America/Denver", reports=(), projects=(), today="")[0]
    b = router.system_blocks(
        "Europe/London",
        reports=({"id": 3, "prompt": "compare vendors"},),
        projects=({"id": 7, "name": "back garden fence"},),
        today="EVENT: standup — tomorrow at 9 AM",
    )[0]

    assert a == b, "the cached block changed with the request — caching is dead"
    assert a["cache_control"] == {"type": "ephemeral"}

    text = a["text"]
    for leak in ("2026", "Denver", "London", "standup", "vendors", "fence", "TODAY —"):
        assert leak not in text, f"{leak!r} leaked into the cached block"


def test_the_live_half_carries_everything_that_can_change():
    """The other side of the same contract: what was moved out has to still
    be in the prompt, or the router loses the day and the calendar table."""
    blocks = router.system_blocks(
        "America/Denver",
        reports=({"id": 3, "prompt": "compare vendors"},),
        projects=({"id": 7, "name": "back garden fence"},),
        today="EVENT: standup — tomorrow at 9 AM",
    )
    live = blocks[1]["text"]

    assert "cache_control" not in live
    for expected in ("Current date and time:", "CALENDAR", "TODAY", "standup",
                     "REPORTS", "vendors", "PROJECTS", "fence"):
        assert expected in live


def test_an_empty_day_omits_the_today_block():
    """A heading with nothing under it invites the model to answer from it
    anyway — the same call the REPORTS table already made."""
    live = router.system_blocks("America/Denver", today="")[1]["text"]
    assert "TODAY" not in live


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
    cached = count(
        client,
        messages=messages,
        system=[router.system_blocks("America/Denver")[0]],
        tools=router.TOOLS,
    )
    print(f"tools alone:          {with_tools - baseline} tokens")
    print(f"cacheable prefix:     {cached} tokens (documented floor {CACHE_FLOOR})")
    print(f"whole router prompt:  {whole} tokens")
    assert whole > 0


def test_context_is_in_the_live_half_not_the_static_one():
    """The static block is byte-stable and carries the cache_control marker.
    A question-derived block inside it kills prompt caching silently and
    permanently — no error, both counters zero, and it reads as a working
    optimisation forever."""
    blocks = router.system_blocks(
        "America/Denver", context="NOTE: the fence needs a post"
    )
    static, live = blocks[0], blocks[1]
    assert "cache_control" in static
    assert "fence" not in static["text"]
    assert "fence" in live["text"]


def test_static_half_is_byte_identical_across_contexts():
    a = router.system_blocks("America/Denver", context="NOTE: one thing")
    b = router.system_blocks("Europe/London", context="NOTE: a different thing")
    assert a[0]["text"] == b[0]["text"]


def test_empty_context_omits_the_heading():
    """An empty CONTEXT: heading invites an answer from a block holding
    nothing."""
    live = router.system_blocks("America/Denver", context="")[1]["text"]
    assert "CONTEXT" not in live
