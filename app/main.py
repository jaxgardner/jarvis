"""FastAPI entrypoint.

    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

Bind 0.0.0.0 so the phone can reach it over Tailscale. Nothing is exposed to
the public internet — there is no port forward — but /say still requires a
bearer token, because "it's on a private network" is not authentication.
"""

import secrets
import sqlite3
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app import config, handlers, mutations, router, timeutil
from app.db import connect, transaction

app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)


def require_token(authorization: str = Header(default="")) -> None:
    """Bearer auth. compare_digest, not ==, so the comparison doesn't leak the
    token's length or contents through timing."""
    expected = config.jarvis_token()
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


class SayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client: str | None = None
    tz: str | None = None


@app.get("/health")
def health() -> dict:
    """Liveness for launchd, uptime checks, and the cellular smoke test."""
    try:
        conn = connect()
        try:
            applied = conn.execute(
                "SELECT count(*) AS n FROM schema_migrations"
            ).fetchone()["n"]
        finally:
            conn.close()
        db = {"ok": True, "migrations_applied": applied}
    except sqlite3.Error as exc:
        db = {"ok": False, "error": str(exc)}

    return {"status": "ok", "db": db, "configured": config.configured()}


@app.post("/say", dependencies=[Depends(require_token)])
def say(req: SayRequest) -> dict:
    started = time.perf_counter()
    tz_name = req.tz or config.DEFAULT_TZ

    # The utterance row is written first and unconditionally: a request that
    # blows up partway is exactly the one worth having a record of.
    with transaction() as conn:
        utterance_id = int(
            conn.execute(
                "INSERT INTO utterances (raw_text, client) VALUES (?,?)",
                (req.text, req.client),
            ).lastrowid
        )

    try:
        tool, args = router.route(req.text, tz_name)
    except Exception as exc:
        _finish(utterance_id, None, None, "Sorry — something went wrong.", started)
        raise HTTPException(status_code=502, detail=f"router failed: {exc}") from exc

    if tool == "escalate":
        with transaction() as conn:
            # A follow-up inherits the previous job's Claude Code session, so
            # "what did you find about the second one" resumes that
            # conversation instead of starting cold with no idea what "the
            # second one" was.
            session_id = None
            if args.get("is_follow_up"):
                prior = conn.execute(
                    """SELECT session_id FROM jobs
                         WHERE status = 'done' AND session_id IS NOT NULL
                         ORDER BY id DESC LIMIT 1"""
                ).fetchone()
                session_id = prior["session_id"] if prior else None

            job_id = int(
                conn.execute(
                    "INSERT INTO jobs (utterance_id, prompt, session_id) VALUES (?,?,?)",
                    (utterance_id, args.get("restated_task", req.text), session_id),
                ).lastrowid
            )
        reply = (
            "Picking up where we left off. I'll ping you."
            if session_id
            else "On it. I'll ping you when it's done."
        )
        latency = _finish(utterance_id, "deep", tool, reply, started)
        return {
            "reply": reply,
            "route": "deep",
            "job_id": job_id,
            "utterance_id": utterance_id,
            "latency_ms": latency,
        }

    handler = handlers.FAST_HANDLERS.get(tool)
    if handler is None:
        raise HTTPException(status_code=500, detail=f"unknown tool {tool!r}")

    try:
        with transaction() as conn:
            reply = handler(conn, utterance_id, args, tz_name)
    except (KeyError, ValueError) as exc:
        # Malformed tool arguments — a router problem, not a user problem.
        _finish(utterance_id, "fast", tool, "Sorry — I didn't catch that.", started)
        raise HTTPException(status_code=422, detail=f"bad tool args: {exc}") from exc

    latency = _finish(utterance_id, "fast", tool, reply, started)
    return {
        "reply": reply,
        "route": "fast",
        "utterance_id": utterance_id,
        "latency_ms": latency,
    }


def _finish(
    utterance_id: int, route: str | None, intent: str | None, reply: str, started: float
) -> int:
    """Close out the utterance row. latency_ms from day one — you can't
    optimize what you don't record."""
    latency = int((time.perf_counter() - started) * 1000)
    with transaction() as conn:
        conn.execute(
            """UPDATE utterances
                 SET route = ?, intent = ?, response_text = ?, model = ?, latency_ms = ?
                 WHERE id = ?""",
            (route, intent, reply, router.MODEL if route else None, latency, utterance_id),
        )
    return latency


@app.get("/agenda", dependencies=[Depends(require_token)])
def agenda(days: int = 1, tz: str | None = None) -> dict:
    tz_name = tz or config.DEFAULT_TZ
    conn = connect()
    try:
        rows = handlers.agenda_rows(conn, tz_name, max(1, min(days, 365)))
    finally:
        conn.close()
    for item in rows["events"]:
        item["when"] = timeutil.speak_datetime(
            item["starts_at"], tz_name, bool(item["all_day"])
        )
    for item in rows["reminders"]:
        item["when"] = timeutil.speak_datetime(item["fire_at"], tz_name)
    return rows


@app.post("/undo", dependencies=[Depends(require_token)])
def undo() -> dict:
    with transaction() as conn:
        undone = mutations.undo_last(conn)
    if undone is None:
        return {"undone": False, "reply": "There's nothing to undo."}
    return {"undone": True, "reply": "Undone.", **undone}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
def job(job_id: int) -> dict:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="no such job")
    return dict(row)


@app.get("/metrics", dependencies=[Depends(require_token)])
def metrics() -> dict:
    """p50/p95 by route over the last 24h. Treat p95 > 2000ms as a bug."""
    conn = connect()
    try:
        out: dict[str, dict] = {}
        for route_name in ("fast", "deep"):
            values = [
                r["latency_ms"]
                for r in conn.execute(
                    """SELECT latency_ms FROM utterances
                         WHERE route = ? AND latency_ms IS NOT NULL
                           AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day')
                         ORDER BY latency_ms""",
                    (route_name,),
                ).fetchall()
            ]
            if values:
                out[route_name] = {
                    "count": len(values),
                    "p50": values[len(values) // 2],
                    "p95": values[min(len(values) - 1, int(len(values) * 0.95))],
                    "max": values[-1],
                }
            else:
                out[route_name] = {"count": 0}
        return out
    finally:
        conn.close()
