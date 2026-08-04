"""Summarizing a finished report, so it can be talked about out loud.

One Haiku call per finished deep job. This is the one place the deep path
spends API credit rather than riding the Claude Code subscription — small
money, but a real exception to how the two tiers are funded, so it lives in
its own module rather than hiding inside the worker.

Asking the deep agent to write its own summary would be free and would fail
silently the first time it forgot, which is the same reasoning that kept a
"needs input" marker out of the reply feature.
"""

from app import router

# Long enough to answer a follow-up question, short enough that ten of them
# would still fit in a router prompt if that ever became the design.
TARGET_CHARS = 1000

# A hung call must not hold the worker, which drains its queue on a 30-second
# StartInterval.
TIMEOUT_SECONDS = 30.0

_SYSTEM = f"""\
Summarize this report from a research assistant in about {TARGET_CHARS} \
characters of plain prose.

Cover what was asked, what was found, and any numbers, names or dates that \
someone would ask a follow-up question about. Keep specifics over \
generalities — "vendor B, $4,200 a year" rather than "one vendor was \
cheaper".

The summary is read aloud, so write plain sentences: no markdown, no lists, \
no headings, no emoji. Do not open with "This report" or "The assistant" — \
state what was found."""


def summarize(result: str) -> str | None:
    """One paragraph describing a finished report, or None.

    Never raises. A missing summary is a normal state that `handlers.query`
    already handles by falling back to the report text.
    """
    if not result or not result.strip():
        return None
    try:
        response = router._client().messages.create(
            model=router.MODEL,
            max_tokens=512,
            system=_SYSTEM,
            messages=[{"role": "user", "content": result}],
            timeout=TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — see docstring; nothing here is fatal
        return None
    # Deliberately no usage.record(): there is no utterance behind a summary,
    # so it stays out of /metrics the same way receipt extraction does.
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None
