"""Deep-path worker — runs queued jobs through Claude Code headless.

    uv run python -m worker.run          # drain the queue, then exit
    uv run python -m worker.run --once   # at most one job

Two things here are load-bearing and easy to get wrong:

1. ANTHROPIC_API_KEY is stripped from the subprocess environment. app.config
   loads .env into os.environ, and a child process inherits it — which would
   make `claude` authenticate as an API-key user and bill the deep path to
   your credit balance. Removing it makes the CLI fall back to the OAuth
   subscription, which is the whole reason the design splits the two paths.

2. The session id is generated here and passed with --session-id, rather than
   scraped out of the CLI's output afterwards. Same result, no parsing, and
   the id exists in the database before the process starts — so a job that
   dies mid-run is still resumable.
"""

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from app import notify, timeutil
from app.config import REPO_ROOT
from app.db import transaction

TIMEOUT_SECONDS = 300  # 5 minutes, per the design doc's starting point
MAX_ATTEMPTS = 2

MCP_CONFIG = REPO_ROOT / "mcp.json"

# Everything the deep path is allowed to touch. Note mcp__jarvis__* covers the
# database tools; Bash is deliberately absent.
ALLOWED_TOOLS = "mcp__jarvis__*,Read,Write,WebSearch,WebFetch"


def _claim() -> dict | None:
    """Take the oldest queued job. UPDATE...RETURNING so two workers can't
    take the same one."""
    with transaction() as conn:
        row = conn.execute(
            """UPDATE jobs SET status = 'running',
                               started_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                               attempts = attempts + 1
                 WHERE id = (SELECT id FROM jobs WHERE status = 'queued'
                               ORDER BY created_at LIMIT 1)
                 RETURNING *""",
        ).fetchone()
    return dict(row) if row else None


def _child_env(job: dict) -> dict:
    env = dict(os.environ)
    # See module docstring — this is what keeps the deep path on the
    # subscription instead of billing API credits.
    env.pop("ANTHROPIC_API_KEY", None)

    # Claude Code's normal OAuth credentials live in the login keychain, which
    # a LaunchDaemon cannot read — it runs outside any login session, so the
    # CLI reports "Not logged in · Please run /login" and exits 1. A long-lived
    # token from `claude setup-token` lives in .env instead, readable by the
    # daemon user, and still bills against the subscription.
    token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token

    if job.get("utterance_id"):
        env["JARVIS_UTTERANCE_ID"] = str(job["utterance_id"])
    return env


def auth_configured() -> bool:
    """Whether the worker can authenticate headlessly at all."""
    return bool(os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip())


def _command(job: dict, session_id: str, resume: bool) -> list[str]:
    cmd = [
        "claude",
        "-p",
        job["prompt"],
        "--output-format",
        "json",
        "--mcp-config",
        str(MCP_CONFIG),
        # Without this, the CLI also loads whatever MCP servers are configured
        # for the user account — the job would get tools this code never
        # granted it.
        "--strict-mcp-config",
        "--allowedTools",
        ALLOWED_TOOLS,
        # Auto mode: the same classifier-driven permissioning an interactive
        # session uses, rather than dontAsk's flat gate. It judges each action
        # in context (17 allow rules, 65 soft denies, a hard deny on data
        # exfiltration) instead of accepting or refusing by tool name alone.
        #
        # This widens what the agent may *decide* to do, not what it *can* do:
        # --allowedTools still bounds the tool set, and Bash is not in it.
        "--permission-mode",
        "auto",
    ]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    return cmd


def _finish(job_id: int, status: str, result: str | None, error: str | None) -> None:
    with transaction() as conn:
        conn.execute(
            """UPDATE jobs SET status = ?, result = ?, error = ?,
                               finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                 WHERE id = ?""",
            (status, result, error, job_id),
        )


def _requeue(job_id: int, error: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'queued', error = ? WHERE id = ?", (error, job_id)
        )


def run_job(job: dict) -> dict:
    session_id = job.get("session_id") or str(uuid.uuid4())
    resume = bool(job.get("session_id"))

    if not resume:
        with transaction() as conn:
            conn.execute(
                "UPDATE jobs SET session_id = ? WHERE id = ?", (session_id, job["id"])
            )

    try:
        completed = subprocess.run(  # noqa: S603
            _command(job, session_id, resume),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=str(REPO_ROOT),
            env=_child_env(job),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _handle_failure(job, f"timed out after {TIMEOUT_SECONDS}s")
    except FileNotFoundError:
        return _handle_failure(job, "claude CLI not found on PATH")

    if completed.returncode != 0:
        # The CLI reports auth and config failures on stdout, not stderr, so a
        # stderr-only error message reads as a bare "exit 1" with no cause.
        detail = (completed.stderr.strip() or completed.stdout.strip() or "no output")
        return _handle_failure(job, f"exit {completed.returncode}: {detail[:1500]}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _handle_failure(job, f"unparseable output: {completed.stdout[:300]}")

    if payload.get("is_error"):
        return _handle_failure(job, f"agent error: {str(payload.get('result'))[:400]}")

    result = str(payload.get("result", "")).strip()
    _finish(job["id"], "done", result, None)

    # Permission denials aren't failures, but they explain a thin answer —
    # surface them rather than leaving you to wonder why it gave up.
    denials = payload.get("permission_denials") or []
    if denials:
        print(f"job {job['id']}: {len(denials)} permission denial(s)", file=sys.stderr)

    notify.push(
        _summarize(result),
        title="Job finished",
        tags="white_check_mark",
        priority="default",
    )
    return {
        "job_id": job["id"],
        "status": "done",
        "cost_usd": payload.get("total_cost_usd"),
        "turns": payload.get("num_turns"),
    }


def _handle_failure(job: dict, error: str) -> dict:
    if job["attempts"] < MAX_ATTEMPTS:
        _requeue(job["id"], error)
        return {"job_id": job["id"], "status": "queued", "error": error}

    _finish(job["id"], "failed", None, error)
    notify.push(
        f"Couldn't finish that one: {error[:150]}",
        title="Job failed",
        tags="x",
        priority="high",
    )
    return {"job_id": job["id"], "status": "failed", "error": error}


def _summarize(result: str, limit: int = 240) -> str:
    """One line for the push. The full text stays in the database and is read
    back via GET /jobs/{id} — a notification is not the delivery mechanism.

    Taking the literal first line is not enough. Agents routinely open with a
    lead-in that ends in a colon ("Saved as note 3. Here's the comparison:")
    and put the substance in a markdown table underneath, so a first-line
    summary reads as a sentence that got cut off mid-thought. This drops
    markdown structure, works in whole sentences, and refuses to end on a
    dangling colon.
    """
    sentences: list[str] = []
    for line in result.splitlines():
        stripped = line.strip()
        # Skip table rows, headings, fences, rules, quotes, and list bullets —
        # they carry the detail, but none of it survives one line of text.
        if not stripped or stripped.startswith(("|", "#", "```", "---", ">", "*", "-")):
            continue
        # Split sentences WITHIN the line, not across the flattened document.
        # A lead-in colon is line-final in the source; once lines are joined it
        # becomes sentence-internal and is no longer detectable.
        # Split on a terminator followed by whitespace, so decimals survive —
        # a naive [.!?] split turns "27.2 inches" into a sentence ending
        # "Its 27." and the summary trails off mid-measurement.
        for raw in re.split(r"(?<=[.!?])\s+", stripped):
            sentence = raw.strip()
            if sentence and not sentence.endswith(":"):
                sentences.append(sentence)

    if not sentences:
        return "Done."

    summary = ""
    for sentence in sentences:
        candidate = f"{summary} {sentence}".strip() if summary else sentence
        if len(candidate) > limit:
            break
        summary = candidate
    if not summary:
        summary = sentences[0][: limit - 1].rstrip() + "…"

    # Replies are read aloud or shown as a plain notification — strip emphasis
    # markers rather than speaking "asterisk asterisk".
    return re.sub(r"[*_`]{1,2}", "", summary).strip()


def main() -> int:
    once = "--once" in sys.argv
    processed = []
    while True:
        job = _claim()
        if job is None:
            break
        print(f"job {job['id']} (attempt {job['attempts']}): {job['prompt'][:60]}")
        processed.append(run_job(job))
        if once:
            break

    for entry in processed:
        print(json.dumps(entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
