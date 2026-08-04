# Part 2 — The local router: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Find out whether a local 3B model can make `route()` faster than Haiku without routing worse — and record the answer either way.

**Architecture:** `mlx-lm` behind `ROUTER_BACKEND=haiku|mlx`, running `route()` **only**. `answer()` stays on Haiku permanently: it produces the prose spoken to the user verbatim, and it is the one call where a smaller model's quality loss is audible rather than merely measurable. A persisted KV cache over the byte-stable `_SYSTEM_STATIC` prefix is what makes this viable at all, and grammar-constrained decoding is what keeps tool calls valid.

**Tech Stack:** Python 3.12, `mlx-lm`, `Qwen2.5-3B-Instruct-4bit`.

## This part is allowed to fail

**Read this before writing any code.** This is an experiment with a pass/fail
gate, not a migration. Shipping it requires beating Haiku on `turn_ms` **and**
tying on three live routing suites. Losing either is a result to record, not a
bug to work around.

Two things could make it lose, and both are real:

- **Prefill.** The prompt is 3244 input tokens on the median call with a
  5253-token static prefix. On a base M4's 10-core GPU, a cold prefill of that
  exceeds the 686ms Haiku floor being replaced. Everything depends on the KV
  cache actually being reused.
- **GPU contention.** Today the model runs on Anthropic's servers, so
  generation and Kanade voice conversion are genuinely parallel. Local, they
  compete for the same GPU and serialize. A model that wins on tokens/sec can
  lose on `turn_ms`, which is the only number that counts.

## Prerequisites

- **Part 1 must be complete and have been running for several days.** The gate
  is stated in `turn_ms`; without a real baseline there is nothing to compare
  against. Benchmarking the afternoon Part 1 lands measures a cold MLX cache
  against a warm API connection and concludes the wrong thing.
- **Memory headroom.** Measured 2026-08-04: 16GB total, 3.2GB of 4GB swap
  already committed, FastAPI resident at 1.58GB. A 3B 4-bit model is ~2GB and
  fits. An 8B is ~4.5GB and does not — do not substitute one.

## Global Constraints

- **Python 3.12**, run with `uv run`.
- **`route()` only.** `answer()` never touches the local model.
- **The filesystem is the switch.** A missing model falls back to Haiku rather than failing a turn — the same rule `/speech` follows.
- **`_SYSTEM_STATIC` must stay byte-stable.** It is the cache prefix for both backends.
- **Commit after every task.**

## File Structure

| File | Responsibility | Action |
| :-- | :-- | :-- |
| `app/router_mlx.py` | Local `route()`: model, cache, constrained decoding | Create |
| `app/router.py` | Dispatch on `ROUTER_BACKEND` | Modify |
| `app/config.py` | `ROUTER_BACKEND`, `ROUTER_MLX_MODEL` | Modify |
| `app/main.py` | `/health` reports the active backend | Modify |
| `bench/router_bench.py` | The measurement that decides this | Create |
| `tests/test_router_mlx.py` | Fallback, schema conformance, cache reuse | Create |

---

### Task 1: Install and measure the floor before writing anything

**Files:**
- Create: `bench/router_bench.py`

**Interfaces:**
- Produces: `bench/router_bench.py`, runnable standalone.

**Why this is Task 1:** if a bare `mlx-lm` call over a 5000-token prompt cannot
beat 686ms on this machine, nothing later in this plan can rescue it, and you
will have learned that in twenty minutes instead of two weeks.

- [ ] **Step 1: Install**

```bash
uv add mlx-lm
uv run python -c "import mlx_lm; print(mlx_lm.__version__)"
```

- [ ] **Step 2: Fetch the model**

```bash
uv run python - <<'PY'
from mlx_lm import load
model, tokenizer = load("mlx-community/Qwen2.5-3B-Instruct-4bit")
print("loaded")
PY
```

Expected: ~2GB downloaded. Check free memory afterwards with `memory_pressure`.

- [ ] **Step 3: Write the benchmark**

Create `bench/router_bench.py`:

```python
"""Does a local 3B beat Haiku at routing, on this machine, on this prompt?

Measures the two things that decide it: prefill with and without a reused KV
cache, and generation of a realistic tool call. Run it before building
anything — if the cold number is bad and the warm number is not much better,
the KV cache is not being reused and the rest of this plan cannot help.
"""

import statistics
import time

from app import router

UTTERANCES = [
    "remind me to call the dentist at four",
    "what have I got going on today",
    "note that the fence posts are rotten",
    "start a project on the back garden fence",
    "what did I say about the landlord",
]


def bench_haiku(runs: int = 5) -> list[float]:
    timings = []
    for text in UTTERANCES[:runs]:
        start = time.perf_counter()
        router.route(text, "America/Denver")
        timings.append((time.perf_counter() - start) * 1000)
    return timings


def bench_mlx(runs: int = 5) -> dict:
    from mlx_lm import generate, load
    from mlx_lm.models.cache import make_prompt_cache

    from app import config

    model, tokenizer = load(config.ROUTER_MLX_MODEL)

    static = router._SYSTEM_STATIC
    prefix_tokens = len(tokenizer.encode(static))

    # Cold: no cache, full prefill of the static prefix every time.
    cold = []
    for text in UTTERANCES[:runs]:
        prompt = static + "\n" + text
        start = time.perf_counter()
        generate(model, tokenizer, prompt=prompt, max_tokens=64, verbose=False)
        cold.append((time.perf_counter() - start) * 1000)

    # Warm: prefill the static prefix once into a cache, reuse it. This is the
    # number that decides the whole part.
    cache = make_prompt_cache(model)
    generate(model, tokenizer, prompt=static, max_tokens=1, prompt_cache=cache, verbose=False)

    warm = []
    for text in UTTERANCES[:runs]:
        start = time.perf_counter()
        generate(
            model, tokenizer, prompt=text, max_tokens=64,
            prompt_cache=cache, verbose=False,
        )
        warm.append((time.perf_counter() - start) * 1000)

    return {"prefix_tokens": prefix_tokens, "cold": cold, "warm": warm}


def report(label: str, timings: list[float]) -> None:
    print(
        f"{label:<12} n={len(timings)} "
        f"median={statistics.median(timings):.0f}ms "
        f"min={min(timings):.0f}ms max={max(timings):.0f}ms"
    )


if __name__ == "__main__":
    print("Haiku floor to beat: 686ms measured 2026-08-04 (undo_last, min)\n")
    report("haiku", bench_haiku())
    result = bench_mlx()
    print(f"\nstatic prefix: {result['prefix_tokens']} tokens")
    report("mlx cold", result["cold"])
    report("mlx warm", result["warm"])
    print(
        "\nIf warm is not dramatically better than cold, the KV cache is not "
        "being reused and nothing later in this plan will fix it."
    )
```

Add `ROUTER_MLX_MODEL` to `app/config.py`:

```python
ROUTER_BACKEND = os.getenv("ROUTER_BACKEND", "haiku").strip().lower()
ROUTER_MLX_MODEL = os.getenv(
    "ROUTER_MLX_MODEL", "mlx-community/Qwen2.5-3B-Instruct-4bit"
).strip()
```

- [ ] **Step 4: Run it, twice, on an idle machine**

```bash
uv run python -m bench.router_bench
uv run python -m bench.router_bench
```

Run twice: the first includes model load. Beware n=5 — CLAUDE.md records that
a single Haiku call's spread runs 600–2400ms, so read medians, not minima.

- [ ] **Step 5: Decide, and record the decision either way**

**If `mlx warm` median is not below ~600ms, stop here.** Write the numbers
into CLAUDE.md as a measured dead end and close the branch. That is a
successful outcome of this task: it costs an afternoon and prevents the same
proposal returning every six months.

**Also stop if `warm` is not much faster than `cold`** — that means the cache
is not being reused, and the version of `mlx-lm` you have may not support
`prompt_cache` the way this assumes. Check its API before continuing.

- [ ] **Step 6: Commit the benchmark regardless of outcome**

```bash
git add bench/router_bench.py app/config.py pyproject.toml uv.lock
git commit -m "bench: measure a local 3B router against the Haiku floor"
```

---

### Task 2: The local backend

**Files:**
- Create: `app/router_mlx.py`
- Test: `tests/test_router_mlx.py`

**Interfaces:**
- Consumes: `router._SYSTEM_STATIC`, `router._live_half`, `router.TOOLS`, `config.ROUTER_MLX_MODEL`.
- Produces: `router_mlx.available() -> bool`, `router_mlx.route(text, tz_name, reports=(), projects=(), today="", context="") -> tuple[str, dict]`, `router_mlx.warm() -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_router_mlx.py`:

```python
"""The local router: fallback, schema conformance, and cache reuse.

Everything here except the marked live test runs without the model present,
because the fallback behaviour is what must hold on a machine that has never
downloaded it.
"""

import pytest

from app import config, router


def test_missing_model_is_not_available(monkeypatch):
    """The filesystem is the switch, like /speech. A machine without the
    weights falls back to Haiku rather than failing a turn."""
    from app import router_mlx

    monkeypatch.setattr(router_mlx, "_MODEL", None)
    monkeypatch.setattr(config, "ROUTER_MLX_MODEL", "definitely/not-a-real-model")
    assert router_mlx.available() is False


def test_dispatch_falls_back_when_unavailable(monkeypatch):
    """ROUTER_BACKEND=mlx with no model must still route, on Haiku."""
    from app import router_mlx

    monkeypatch.setattr(config, "ROUTER_BACKEND", "mlx")
    monkeypatch.setattr(router_mlx, "available", lambda: False)
    called = {}

    def fake_haiku(*args, **kwargs):
        called["haiku"] = True
        return ("answer", {"reply": "hi"})

    monkeypatch.setattr(router, "_route_haiku", fake_haiku)
    router.route("hello", "America/Denver")
    assert called.get("haiku")


def test_tool_names_match_the_haiku_schema():
    """Both backends must offer exactly the same tools, or a routing test
    passing on one says nothing about the other."""
    from app import router_mlx

    assert router_mlx.tool_names() == {t["name"] for t in router.TOOLS}


def test_static_prefix_is_shared_with_haiku():
    """The cached prefix must be the same bytes for both backends — it is the
    thing being cached, and two versions would drift."""
    from app import router_mlx

    assert router_mlx.cache_prefix() == router._SYSTEM_STATIC


@pytest.mark.skipif(
    config.ROUTER_BACKEND != "mlx", reason="needs ROUTER_BACKEND=mlx and the model"
)
def test_returns_a_valid_tool_call():
    from app import router_mlx

    tool, args = router_mlx.route("remind me to call the dentist at four", "America/Denver")
    assert tool in {t["name"] for t in router.TOOLS}
    assert isinstance(args, dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_router_mlx.py -v`
Expected: FAIL — no module `app.router_mlx`

- [ ] **Step 3: Implement**

Create `app/router_mlx.py`:

```python
"""route() on a local 3B, behind ROUTER_BACKEND=mlx.

route() ONLY. answer() stays on Haiku permanently — it produces the prose
spoken to the user verbatim, and it is the one call where a smaller model's
quality loss is audible rather than merely measurable. Part 1's pre-retrieval
already removes most answer() calls; what remains are the questions that
genuinely needed a search, which are the ones worth a good model.

The persisted KV cache is what makes this viable. The prompt is ~3244 input
tokens with a 5253-token static prefix, and a cold prefill of that on a
10-core M4 exceeds the 686ms Haiku floor being replaced. The prefix is
router._SYSTEM_STATIC — the same bytes Anthropic prompt caching uses, which is
why that split already existed.
"""

import json
import threading

from app import config, router

_MODEL = None
_TOKENIZER = None
_CACHE = None
# Reentrant: warm() calls _ensure(), which takes the same lock. A plain Lock
# deadlocks on the first call after a cold start — and only after a cold
# start, which is the worst kind of bug to ship. speech/synth.py learned this
# the same way.
_LOCK = threading.RLock()


def tool_names() -> set[str]:
    return {tool["name"] for tool in router.TOOLS}


def cache_prefix() -> str:
    """The bytes held in the KV cache. Shared with the Haiku path so the two
    backends cannot drift apart on the thing being cached."""
    return router._SYSTEM_STATIC


def _ensure():
    """Load the model and prefill the static prefix once."""
    global _MODEL, _TOKENIZER, _CACHE
    with _LOCK:
        if _MODEL is not None:
            return _MODEL, _TOKENIZER, _CACHE
        from mlx_lm import generate, load
        from mlx_lm.models.cache import make_prompt_cache

        _MODEL, _TOKENIZER = load(config.ROUTER_MLX_MODEL)
        _CACHE = make_prompt_cache(_MODEL)
        # Prefill the static half once. Everything after this call reuses it,
        # which is the entire performance argument for this module.
        generate(
            _MODEL, _TOKENIZER, prompt=cache_prefix(),
            max_tokens=1, prompt_cache=_CACHE, verbose=False,
        )
        return _MODEL, _TOKENIZER, _CACHE


def available() -> bool:
    """Whether the local path can serve a turn.

    The filesystem is the switch, as it is for /speech: weights that are not
    there mean fall back to Haiku, never fail. A flag that can disagree with
    the filesystem is a flag that eventually will.
    """
    try:
        _ensure()
        return True
    except Exception:  # noqa: BLE001 — an unusable model is a fallback, not an error
        return False


def warm() -> None:
    """Pay the load and the prefix prefill at startup rather than on the first
    thing you say after a reboot."""
    try:
        _ensure()
    except Exception:  # noqa: BLE001
        pass


def _schema() -> dict:
    """A JSON schema accepting exactly one tool call.

    Grammar-constrained decoding against this is what replaces
    tool_choice={"type": "any"}. There is no local equivalent of that
    guarantee, and without a grammar a 3B produces prose, a half-formed call,
    or a tool that does not exist — the failures the router has no path for.
    """
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": sorted(tool_names())},
            "arguments": {"type": "object"},
        },
        "required": ["tool", "arguments"],
    }


def route(
    text: str, tz_name: str, reports=(), projects=(), today: str = "", context: str = ""
) -> tuple[str, dict]:
    """Classify one utterance locally. Same contract as router.route."""
    from mlx_lm import generate

    model, tokenizer, cache = _ensure()

    # Only the live half goes through the model each call; the static half is
    # already in the cache.
    live = router._live_half(tz_name, reports, projects, today, context)
    prompt = (
        f"{live}\n\n"
        f"Tools: {json.dumps([{'name': t['name'], 'description': t['description'], "
        f"'input_schema': t['input_schema']} for t in router.TOOLS])}\n\n"
        f"User: {text}\n\n"
        "Reply with one JSON object: {\"tool\": ..., \"arguments\": {...}}. "
        "Nothing else."
    )

    with _LOCK:
        raw = generate(
            model, tokenizer, prompt=prompt, max_tokens=512,
            prompt_cache=cache, verbose=False,
        )

    try:
        parsed = json.loads(raw.strip())
        tool = parsed["tool"]
        if tool not in tool_names():
            raise ValueError(f"unknown tool {tool!r}")
        return tool, dict(parsed.get("arguments") or {})
    except Exception as exc:
        # A local model that produced nothing usable falls back rather than
        # failing the turn. This is the case the grammar exists to prevent, so
        # count it — if it is not rare, the grammar is not working.
        raise RuntimeError(f"mlx router produced no valid tool call: {exc}") from exc
```

**Grammar note:** the implementation above validates *after* generation. If
your `mlx-lm` version supports a logit processor or grammar argument, use it —
constrained decoding prevents the failure rather than detecting it, and the
gate in Task 4 is much more likely to pass with it. Check
`mlx_lm.generate`'s signature for `logits_processors` and wire the schema in.

- [ ] **Step 4: Dispatch from `router.route`**

In `app/router.py`, rename the existing `route` to `_route_haiku` and add:

```python
def route(
    text: str, tz_name: str, reports=(), projects=(), today: str = "", context: str = ""
) -> tuple[str, dict]:
    """Classify one utterance, on whichever backend is configured.

    Falls back to Haiku whenever the local path is unavailable or produces
    nothing usable. A turn must never fail because an experiment did.
    """
    if config.ROUTER_BACKEND == "mlx":
        from app import router_mlx

        if router_mlx.available():
            try:
                return router_mlx.route(text, tz_name, reports, projects, today, context)
            except Exception:  # noqa: BLE001 — fall back, never fail the turn
                pass
    return _route_haiku(text, tz_name, reports, projects, today, context)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_router_mlx.py -v`
Expected: PASS (4 passed, 1 skipped without the model)

- [ ] **Step 6: Commit**

```bash
git add app/router_mlx.py app/router.py tests/test_router_mlx.py
git commit -m "feat: local MLX router behind ROUTER_BACKEND, falling back to Haiku"
```

---

### Task 3: Report which backend is live

**Files:**
- Modify: `app/main.py` (`/health`, `lifespan`)

- [ ] **Step 1: Add to `/health`**

```python
    out["router"] = {
        "backend": config.ROUTER_BACKEND,
        # What is actually serving, which is not the same thing: mlx with no
        # weights is haiku, and a health check that reported the setting
        # rather than the behaviour would hide exactly that.
        "active": "mlx" if (
            config.ROUTER_BACKEND == "mlx" and router_mlx.available()
        ) else "haiku",
        "model": config.ROUTER_MLX_MODEL if config.ROUTER_BACKEND == "mlx" else router.MODEL,
    }
```

- [ ] **Step 2: Warm at startup**

In `lifespan`, beside the existing speech warm-up:

```python
    if config.ROUTER_BACKEND == "mlx":
        threading.Thread(target=router_mlx.warm, daemon=True).start()
```

On a background thread: loading 2GB and prefilling 5000 tokens must not hold
up the first request, and the fallback covers anything asked before it lands.

- [ ] **Step 3: Verify, then commit**

```bash
curl -s localhost:8000/health -H "Authorization: Bearer $JARVIS_TOKEN" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['router'])"
git add app/main.py
git commit -m "feat: /health reports the active router backend"
```

---

### Task 4: The gate

**Files:** none — this task is a measurement and a decision.

- [ ] **Step 1: Routing must tie**

```bash
ROUTER_BACKEND=mlx uv run pytest tests/test_utterances.py \
  tests/test_gratitude_routing.py tests/test_projects_routing.py -v
```

Expected: **the same results as on Haiku.** Run the same suites on Haiku
first if you do not already know they are green, so a pre-existing failure is
not read as a regression.

`test_gratitude_is_not_a_note` deserves specific attention: that distinction
regressed once already on a much larger model, and it is the canary for a
local model routing on vibes rather than on the tool description.

- [ ] **Step 2: `turn_ms` must win**

Run both backends for a day each, in the same rooms, saying the same kinds of
things:

```bash
curl -s "localhost:8000/metrics?days=1" -H "Authorization: Bearer $JARVIS_TOKEN" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['turn'])"
```

Compare p50 and p95 of `turn_ms`, **not** `latency_ms` — the whole point of
Part 1 is that the second one cannot see this trade.

- [ ] **Step 3: Check for GPU contention specifically**

The failure this part is most likely to hit is invisible in an idle
benchmark. Say something that produces a long spoken reply, so Kanade is
converting while the model would be generating:

```bash
curl -s -X POST localhost:8000/say -H "Authorization: Bearer $JARVIS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text":"what have I got going on today","tz":"America/Denver"}' \
  -w '\ntotal=%{time_total}s\n'
```

Compare against the same utterance on Haiku. If the local backend is slower
here while winning on an idle benchmark, that is the contention, and it is a
reason to stop.

- [ ] **Step 4: Decide**

**Ships** only if routing ties and `turn_ms` p50 and p95 both improve. Set
`ROUTER_BACKEND=mlx` in `.env`.

**Does not ship** otherwise. Leave `ROUTER_BACKEND=haiku`. The code stays —
it is behind a flag, costs nothing when off, and makes the next attempt
cheap.

- [ ] **Step 5: Record the numbers in CLAUDE.md either way**

This is the deliverable of this part, more than the code is. Write down: the
static prefix token count, cold and warm prefill, `turn_ms` p50/p95 on both
backends, the contention result from Step 3, and the routing suite outcome.

If it did not ship, say so plainly and say why. A measured dead end stops the
same proposal returning every six months; an undocumented one guarantees it.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md .env.example
git commit -m "docs: what the local router measured, and what was decided"
```

---

## Self-review

Checked against the spec's Part 2:

- `mlx-lm`, not Ollama, for KV cache control — Tasks 1, 2.
- `route()` only; `answer()` stays on Haiku — enforced by `router_mlx`
  exposing no `answer`, and stated in its docstring.
- Persisted prompt cache over `_SYSTEM_STATIC` — Task 2,
  `test_static_prefix_is_shared_with_haiku`.
- 3B not 8B, with the memory reasoning — stated in the prerequisites.
- Forced tool calls — `_schema()` plus the grammar note in Task 2, Step 3.
- The gate over three named suites — Task 4, Step 1.
- GPU contention as the likely loss — called out at the top and measured
  explicitly in Task 4, Step 3.
- Record the outcome either way — Task 4, Step 5.
- Filesystem-is-the-switch fallback — Task 2,
  `test_dispatch_falls_back_when_unavailable`.

**One addition the spec did not have:** Task 1 is a standalone benchmark run
*before* any integration, with explicit permission to stop. The spec described
the gate at the end; putting a cheaper version of it at the front means a
dead end costs an afternoon instead of two weeks. If `mlx warm` cannot beat
686ms on a bare prompt, nothing downstream rescues it.

**One risk this plan cannot close:** `mlx-lm`'s prompt-cache and
constrained-decoding APIs move between versions. Both Task 1's benchmark and
Task 2's implementation name specific functions (`make_prompt_cache`,
`generate(..., prompt_cache=...)`, `logits_processors`). Check them against
the installed version before assuming a failure is a performance result rather
than an API mismatch.

## Next

**This is the last part.** If you arrived here from Part 6, everything in
`docs/superpowers/specs/2026-08-04-speed-senses-memory-design.md` is now
either built or deliberately recorded as not worth building.

Update the index at
[`2026-08-04-speed-senses-memory-index.md`](2026-08-04-speed-senses-memory-index.md)
with what shipped and what did not.
