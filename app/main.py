"""FastAPI entrypoint.

    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

Bind 0.0.0.0 so the phone can reach it over Tailscale. Nothing is exposed to
the public internet — there is no port forward — but /say still requires a
bearer token, because "it's on a private network" is not authentication.
"""

import secrets
import sqlite3
import time
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app import config, devices, handlers, mutations, router, timeutil, usage
from app.db import connect, transaction

app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)


@dataclass(frozen=True)
class Principal:
    """Who is calling. `device_id` is None for the shared JARVIS_TOKEN."""

    device_id: int | None
    label: str


def require_token(authorization: str = Header(default="")) -> Principal:
    """Bearer auth, per-device first, shared token second.

    Devices are checked before JARVIS_TOKEN so that revoking a lost phone
    takes effect immediately without touching the shared token — which stays
    valid indefinitely, because the iOS Shortcut still uses it and a migration
    that breaks the working client on day one is not a migration.
    """
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(status_code=401, detail="unauthorized")

    conn = connect()
    try:
        device = devices.authenticate(conn, presented)
        if device is not None:
            devices.touch(conn, device["id"])
            return Principal(device_id=device["id"], label=device["label"])
    finally:
        conn.close()

    try:
        expected = config.jarvis_token()
    except RuntimeError:
        # No shared token configured. Registered devices still work; nothing
        # else does. That is a valid end state once the Shortcut is retired.
        raise HTTPException(status_code=401, detail="unauthorized") from None

    # compare_digest, not ==, so the comparison doesn't leak the token's
    # length or contents through timing.
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    return Principal(device_id=None, label="shared")


class SayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    client: str | None = None
    tz: str | None = None


class DeviceRequest(BaseModel):
    label: str = Field(default="iPhone", min_length=1, max_length=120)
    platform: str = Field(default="ios", pattern="^(ios|macos|shortcut)$")
    apns_token: str | None = Field(default=None, pattern="^[0-9a-fA-F]{4,200}$")
    apns_env: str | None = Field(default=None, pattern="^(prod|sandbox)$")


class SnoozeRequest(BaseModel):
    # Default matches the notification's "Snooze 10m" button.
    minutes: int = Field(default=10, ge=1, le=1440)
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
    # One scope around the whole request: `query` can call the model twice
    # (route, then answer), and both hops belong to the same utterance.
    with usage.tally():
        return _say(req)


def _say(req: SayRequest) -> dict:
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
        # What this utterance actually touched. The client needs it to offer a
        # follow-up on the specific row — Siri's reminder snippet puts a Snooze
        # button on the thing you just captured, which it cannot do if all it
        # gets back is prose.
        "changed": _changed(utterance_id),
    }


def _changed(utterance_id: int) -> dict | None:
    """The row this utterance last touched, if any. Questions touch nothing."""
    conn = connect()
    try:
        row = conn.execute(
            """SELECT table_name, row_id, op FROM mutations
                 WHERE utterance_id = ? ORDER BY id DESC LIMIT 1""",
            (utterance_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"table": row["table_name"], "row_id": row["row_id"], "op": row["op"]}


def _finish(
    utterance_id: int, route: str | None, intent: str | None, reply: str, started: float
) -> int:
    """Close out the utterance row. latency_ms from day one — you can't
    optimize what you don't record. Tokens for the same reason: without them,
    working out what a month cost means counting tokens after the fact against
    a prompt that has since changed."""
    latency = int((time.perf_counter() - started) * 1000)
    spend = usage.current()
    with transaction() as conn:
        conn.execute(
            """UPDATE utterances
                 SET route = ?, intent = ?, response_text = ?, model = ?, latency_ms = ?,
                     input_tokens = ?, output_tokens = ?, model_calls = ?
                 WHERE id = ?""",
            (
                route,
                intent,
                reply,
                router.MODEL if route else None,
                latency,
                spend["input_tokens"],
                spend["output_tokens"],
                spend["model_calls"],
                utterance_id,
            ),
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


# ── devices (Phase 7) ─────────────────────────────────────


@app.post("/devices")
def register_device(
    req: DeviceRequest, principal: Principal = Depends(require_token)
) -> dict:
    """Enroll a device, or refresh the one that is calling.

    Two shapes, decided by who authenticated:

      - Shared JARVIS_TOKEN → enrollment. Mints a per-device bearer token and
        returns it **once**. The app writes it to the Keychain and uses it for
        everything afterwards; it cannot be recovered from the server.
      - A device token → refresh. Updates the APNs token in place and returns
        no credential, because the caller already holds one.

    The app calls this on every launch: iOS re-issues device tokens on
    reinstall and restore, and a stale one fails silently, which is the worst
    way for push to break.
    """
    with transaction() as conn:
        if principal.device_id is not None:
            row = devices.refresh(
                conn,
                principal.device_id,
                apns_token=req.apns_token,
                apns_env=req.apns_env,
                label=req.label,
            )
            if row is None:
                raise HTTPException(status_code=401, detail="device revoked")
            return {"device_id": row["id"], "label": row["label"], "token": None}

        row, token = devices.register(
            conn,
            label=req.label,
            platform=req.platform,
            apns_token=req.apns_token,
            apns_env=req.apns_env or config.APNS_ENV,
        )
        return {"device_id": row["id"], "label": row["label"], "token": token}


@app.get("/devices", dependencies=[Depends(require_token)])
def list_devices() -> dict:
    conn = connect()
    try:
        return {"devices": devices.live(conn)}
    finally:
        conn.close()


@app.delete("/devices/{device_id}", dependencies=[Depends(require_token)])
def revoke_device(device_id: int) -> dict:
    """Lock a device out. This is the whole reason per-device tokens exist —
    a lost phone should cost one request, not a re-key of every client."""
    with transaction() as conn:
        revoked = devices.revoke(conn, device_id)
    return {"revoked": revoked, "device_id": device_id}


# ── notification actions (Phase 7) ────────────────────────


@app.post("/reminders/{reminder_id}/snooze", dependencies=[Depends(require_token)])
def snooze_reminder(reminder_id: int, req: SnoozeRequest) -> dict:
    tz_name = req.tz or config.DEFAULT_TZ
    with transaction() as conn:
        reply = handlers.snooze(conn, reminder_id, req.minutes, tz_name)
    if reply is None:
        raise HTTPException(status_code=404, detail="no such reminder")
    return {"reply": reply, "reminder_id": reminder_id}


@app.post("/reminders/{reminder_id}/ack", dependencies=[Depends(require_token)])
def ack_reminder(reminder_id: int) -> dict:
    with transaction() as conn:
        reply = handlers.ack(conn, reminder_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="no such reminder")
    return {"reply": reply, "reminder_id": reminder_id}


# ── dashboard reads (Phase 7d) ────────────────────────────


@app.get("/activity", dependencies=[Depends(require_token)])
def activity(limit: int = 50) -> dict:
    """Recent utterances with the rows each one changed.

    This is what makes /undo legible. The mutations log has always known what
    changed; until now nothing surfaced it, so "undo that" was an act of faith.

    Only the newest non-undone mutation is marked `undoable`. /undo reverses
    the most recent one and nothing else, so offering a swipe on an older row
    would be a gesture that silently undoes something else — worse than
    offering nothing.
    """
    limit = max(1, min(limit, 200))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, raw_text, response_text, route, intent, latency_ms,
                      input_tokens, output_tokens, model_calls, created_at
                 FROM utterances ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        utterances = [dict(r) for r in rows]

        newest_undoable = conn.execute(
            "SELECT id FROM mutations WHERE undone_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        undoable_id = newest_undoable["id"] if newest_undoable else None

        by_utterance: dict[int, list[dict]] = {}
        if utterances:
            marks = ",".join("?" for _ in utterances)
            changes = conn.execute(
                f"""SELECT id, utterance_id, table_name, row_id, op, undone_at
                      FROM mutations WHERE utterance_id IN ({marks})
                      ORDER BY id""",  # noqa: S608 — placeholders, not values
                tuple(u["id"] for u in utterances),
            ).fetchall()
            for change in changes:
                by_utterance.setdefault(change["utterance_id"], []).append(
                    {
                        "id": change["id"],
                        "table": change["table_name"],
                        "row_id": change["row_id"],
                        "op": change["op"],
                        "undone_at": change["undone_at"],
                        "undoable": change["id"] == undoable_id,
                    }
                )

        for utterance in utterances:
            utterance["mutations"] = by_utterance.get(utterance["id"], [])
        return {"utterances": utterances}
    finally:
        conn.close()


@app.get("/jobs", dependencies=[Depends(require_token)])
def jobs(limit: int = 50) -> dict:
    """Deep-path history. Results are truncated here — the full text is on
    GET /jobs/{id}, and a list view that ships every result would be mostly
    prose nobody is reading."""
    limit = max(1, min(limit, 200))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, prompt, status, error, attempts, session_id,
                      created_at, started_at, finished_at,
                      substr(result, 1, 280) AS result_preview,
                      length(result) > 280   AS result_truncated
                 FROM jobs ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return {"jobs": [dict(r) for r in rows]}
    finally:
        conn.close()


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
def metrics(days: int = 1) -> dict:
    """p50/p95 by route, plus token spend. Treat p95 > 2000ms as a bug."""
    window = f"-{max(1, min(days, 365))} day"
    conn = connect()
    try:
        out: dict[str, dict] = {}
        for route_name in ("fast", "deep"):
            values = [
                r["latency_ms"]
                for r in conn.execute(
                    """SELECT latency_ms FROM utterances
                         WHERE route = ? AND latency_ms IS NOT NULL
                           AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)
                         ORDER BY latency_ms""",
                    (route_name, window),
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

        out["spend"] = _spend(conn, window)
        return out
    finally:
        conn.close()


def _spend(conn: sqlite3.Connection, window: str) -> dict:
    """What the fast path actually cost.

    Only counts utterances written since migration 004 — earlier rows have
    NULL token columns and are excluded rather than counted as zero, because a
    silent zero would understate spend and look like a win.

    The deep path is absent on purpose: it runs on the Claude Code
    subscription, not API credits, so folding it in here would invent a number.
    """
    row = conn.execute(
        """SELECT count(*) AS utterances,
                  coalesce(sum(model_calls), 0)   AS model_calls,
                  coalesce(sum(input_tokens), 0)  AS input_tokens,
                  coalesce(sum(output_tokens), 0) AS output_tokens
             FROM utterances
             WHERE input_tokens IS NOT NULL
               AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now',?)""",
        (window,),
    ).fetchone()

    total = router.cost_usd(row["input_tokens"], row["output_tokens"])
    return {
        "model": router.MODEL,
        "utterances": row["utterances"],
        "model_calls": row["model_calls"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "usd": round(total, 4),
        "usd_per_utterance": round(total / row["utterances"], 5)
        if row["utterances"]
        else 0.0,
        # Straight-line projection from this window. Honest about being a
        # projection — a quiet day and a heavy day differ by more than 2x.
        "usd_per_month_at_this_rate": round(
            total * (30 / max(1, int(window.split()[0].lstrip("-")))), 2
        ),
    }
