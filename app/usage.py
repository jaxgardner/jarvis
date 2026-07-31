"""Per-request token accounting.

One utterance can make more than one model call — `query` routes through
Haiku and may then call it again to turn rows into a sentence — so the tally
has to accumulate across calls without every function in the chain growing a
`usage` return value. The handlers share one signature via `FAST_HANDLERS`;
threading tokens through it would mean changing all of them to carry
something only one of them produces.

A `ContextVar` scopes the tally to the request instead. FastAPI runs sync
endpoints in a threadpool and anyio copies the context into the worker
thread, so two concurrent /say calls cannot see each other's counts — which a
module-level dict would not guarantee.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import Any

_TALLY: ContextVar[dict[str, int] | None] = ContextVar("jarvis_usage", default=None)


@contextmanager
def tally() -> Iterator[dict[str, int]]:
    """Open a counting scope for one request."""
    counts = {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}
    token = _TALLY.set(counts)
    try:
        yield counts
    finally:
        _TALLY.reset(token)


def record(response_usage: Any) -> None:
    """Add one API response's usage to the open tally.

    A no-op outside a tally scope, so the router stays callable from tests,
    the MCP server, and one-off scripts without a request around it.
    """
    counts = _TALLY.get()
    if counts is None:
        return
    counts["input_tokens"] += getattr(response_usage, "input_tokens", 0) or 0
    counts["output_tokens"] += getattr(response_usage, "output_tokens", 0) or 0
    counts["model_calls"] += 1


def current() -> dict[str, int]:
    """What has been spent in this request so far."""
    counts = _TALLY.get()
    return dict(counts) if counts else {
        "input_tokens": 0,
        "output_tokens": 0,
        "model_calls": 0,
    }
