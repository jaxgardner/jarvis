"""FastAPI entrypoint.

    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

Bind 0.0.0.0 so the phone can reach it over Tailscale. Nothing is exposed to
the public internet — there is no port forward — but /say still requires a
bearer token, because "it's on a private network" is not authentication.
"""

import itertools
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app import config, devices, handlers, mutations, router, timeutil, usage
from app.db import connect, transaction
from gratitude import entries as gratitude_entries
from pantry import images, inventory, receipts
from projects import store as projects_store
from speech import synth


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Loading the ONNX session takes a couple of seconds. In a thread so it
    # does not hold the port closed, and unconditional because `warm` already
    # does nothing on a machine with no weights.
    threading.Thread(target=synth.warm, daemon=True).start()
    yield


app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, lifespan=lifespan)


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
    # The two quiet failures. A receipt stuck in `extracting` means the
    # background task died and the user is looking at a spinner; active items
    # past their date mean the review flow stopped being used and the
    # inventory is drifting away from the actual fridge.
    pantry_health = {"stuck_receipts": 0, "pending_receipts": 0, "overdue_items": 0}
    try:
        conn = connect()
        try:
            # Ahead of the migrations count on purpose: that query raises on a
            # database built straight from the .sql files (no schema_migrations
            # table), and the pantry block must not be collateral damage.
            try:
                pantry_health = {
                    "stuck_receipts": conn.execute(
                        """SELECT count(*) AS n FROM receipts
                             WHERE status = 'extracting'
                               AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-5 minutes')"""
                    ).fetchone()["n"],
                    "pending_receipts": conn.execute(
                        "SELECT count(*) AS n FROM receipts WHERE status = 'pending'"
                    ).fetchone()["n"],
                    "overdue_items": conn.execute(
                        """SELECT count(*) AS n FROM pantry_items
                             WHERE status = 'active' AND expires_on < date('now')"""
                    ).fetchone()["n"],
                }
            except sqlite3.OperationalError:
                # /health is liveness for launchd and must answer even on a
                # database that has not been migrated yet.
                pass

            applied = conn.execute(
                "SELECT count(*) AS n FROM schema_migrations"
            ).fetchone()["n"]
        finally:
            conn.close()
        db = {"ok": True, "migrations_applied": applied}
    except sqlite3.Error as exc:
        db = {"ok": False, "error": str(exc)}

    return {
        "status": "ok",
        "db": db,
        "configured": config.configured(),
        "ingest": _ingest_health(),
        "pantry": pantry_health,
        "tts": _tts_health(),
    }


def _tts_health() -> dict:
    """Why it sounds like a screen reader again, answered without a log file.

    `available` and `loaded` differ for a few seconds after a restart while
    the warm-up thread runs, and differ permanently on a machine whose weights
    were deleted — which is the case worth being able to see.
    """
    return {
        "available": synth.available(),
        "loaded": synth.loaded(),
        # Both halves, because the voice is both. The model decides the words,
        # the accent and the timing; the reference decides the timbre. Either
        # one being wrong sounds like "the voice is wrong", and this is where
        # you would look to tell which.
        "model": config.TTS_MODEL,
        "reference": config.TTS_REFERENCE,
        "chunked": config.TTS_STREAM_CHUNKS,
        # Two numbers because they answer different questions. `last_synth_ms`
        # is the cost of a reply; `last_first_chunk_ms` is how long the phone
        # waited before it could start playing, which is the one the design is
        # actually trying to move. A big gap between them means chunking is
        # working.
        "last_synth_ms": synth.last_synth_ms,
        "last_first_chunk_ms": synth.last_first_chunk_ms,
    }


# An ingester that stops is the failure ingestion is built against, and it is
# a silent one — the agenda just quietly goes stale. Anything past this is
# treated as broken rather than merely quiet.
INGEST_STALE_AFTER = timedelta(hours=6)


def _ingest_health() -> dict:
    """Per-source sync state, with staleness already worked out.

    `last_run_at` and `last_ok_at` are both reported because they answer
    different questions. Equal means healthy. A gap means it is running and
    failing — a different problem from not running at all, which shows as both
    being old.
    """
    try:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT source, last_run_at, last_ok_at, detail FROM sync_state"
                " ORDER BY source"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        # Before migration 006/005 land, or on a database that can't be read.
        # /health must stay up: it is what launchd and the phone check.
        return {"sources": [], "stale": []}

    now = timeutil.now("UTC")
    sources, stale = [], []
    for row in rows:
        item = dict(row)
        ok_at = item.get("last_ok_at")
        try:
            is_stale = ok_at is None or (now - timeutil.parse(ok_at)) > INGEST_STALE_AFTER
        except ValueError:
            is_stale = True
        item["stale"] = is_stale
        sources.append(item)
        if is_stale:
            stale.append(item["source"])
    return {"sources": sources, "stale": stale}


# Tools handled in this module rather than through handlers.FAST_HANDLERS,
# because both may enqueue a job and so must shape the response themselves.
DEEP_TOOLS = {"escalate", "start_project"}


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
        # Same transaction as the insert: the router needs these, and CLAUDE.md
        # records that /say's four SQLite transactions are inside the noise.
        # Making it five to fetch ten rows would not be.
        reports = handlers.recent_reports(conn)
        active_projects = projects_store.active(conn)
        # The day, handed to the router so a question about it can be answered
        # in this call rather than a second one. ~3ms, in the transaction that
        # is already open.
        today = handlers.today_block(conn, tz_name)

    try:
        tool, args = router.route(req.text, tz_name, reports, active_projects, today)
    except Exception as exc:
        _finish(utterance_id, None, None, "Sorry — something went wrong.", started)
        raise HTTPException(status_code=502, detail=f"router failed: {exc}") from exc

    if tool == "escalate":
        task = args.get("restated_task", req.text)
        named = args.get("job_id")
        outcome = "missing"
        with transaction() as conn:
            # A named report is answered through the same helper the reply box
            # and the notification action use, so a spoken answer and a typed
            # one are indistinguishable by the time they reach the database.
            if named is not None:
                outcome = handlers.reply_to_job(conn, int(named), task)
            if outcome in ("ok", "live"):
                # Both mean the named report is the answer to "which job is
                # this about" — one because we re-queued it, one because it
                # was already working.
                job_id = int(named)
            else:
                job_id = int(
                    conn.execute(
                        "INSERT INTO jobs (utterance_id, prompt, project_id) VALUES (?,?,?)",
                        (utterance_id, task, handlers._project_ref(conn, args)),
                    ).lastrowid
                )
        reply = {
            "ok": "Picking up where we left off. I'll ping you.",
            "live": "That one's still working. I'll leave it be.",
        }.get(outcome, "On it. I'll ping you when it's done.")
        latency = _finish(utterance_id, "deep", tool, reply, started)
        synth.prefetch(reply)
        return {
            "reply": reply,
            "route": "deep",
            "job_id": job_id,
            "utterance_id": utterance_id,
            "latency_ms": latency,
        }

    if tool == "start_project":
        with transaction() as conn:
            try:
                project_id, job_id, reply = handlers.start_project(conn, utterance_id, args)
            except ValueError as exc:
                _finish(utterance_id, None, tool, "Sorry — something went wrong.", started)
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Deep only when research came with it. A project created and nothing
        # queued is a fast-path write like any other, and reporting it as deep
        # would have the phone poll a job that does not exist.
        route = "deep" if job_id is not None else "fast"
        latency = _finish(utterance_id, route, tool, reply, started)
        synth.prefetch(reply)
        response = {
            "reply": reply,
            "route": route,
            "project_id": project_id,
            "utterance_id": utterance_id,
            "latency_ms": latency,
        }
        if job_id is not None:
            response["job_id"] = job_id
        return response

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
    # Start the voice before the phone asks for it. The reply has existed
    # since the handler returned; waiting for /speech to arrive would leave
    # the model idle across a round trip it could have been working through.
    # Non-blocking and unable to raise — /say's budget is untouched, which is
    # the whole reason synthesis is a second endpoint rather than part of this
    # one.
    synth.prefetch(reply)
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


# ── devices ───────────────────────────────────────────────


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


# ── notification actions ──────────────────────────────────


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


# ── ingestion review ──────────────────────────────────────
# The one place email extraction can reach `events`, and it needs a human at
# it. An assistant that invents a dentist appointment from a marketing email is
# worse than one that knows nothing about your mail — not because it is wrong
# occasionally, but because you stop trusting the agenda, and then the whole
# thing is decoration.


@app.get("/proposals", dependencies=[Depends(require_token)])
def proposals(limit: int = 50, tz: str | None = None) -> dict:
    tz_name = tz or config.DEFAULT_TZ
    conn = connect()
    try:
        return {
            "proposals": handlers.pending_proposals(
                conn, max(1, min(limit, 200)), tz_name
            )
        }
    except sqlite3.OperationalError:
        return {"proposals": []}
    finally:
        conn.close()


@app.post("/proposals/{proposal_id}/accept", dependencies=[Depends(require_token)])
def accept_proposal(proposal_id: int, tz: str | None = None) -> dict:
    tz_name = tz or config.DEFAULT_TZ
    with transaction() as conn:
        reply = handlers.accept_proposal(conn, proposal_id, tz_name)
    if reply is None:
        raise HTTPException(status_code=404, detail="no such proposal")
    return {"reply": reply, "proposal_id": proposal_id}


@app.post("/proposals/{proposal_id}/reject", dependencies=[Depends(require_token)])
def reject_proposal(proposal_id: int) -> dict:
    with transaction() as conn:
        reply = handlers.reject_proposal(conn, proposal_id)
    if reply is None:
        raise HTTPException(status_code=404, detail="no such proposal")
    return {"reply": reply, "proposal_id": proposal_id}


@app.get("/inbox", dependencies=[Depends(require_token)])
def inbox(limit: int = 25, unread_only: bool = False, q: str | None = None) -> dict:
    """Recent ingested mail — metadata and Google's snippet, never bodies."""
    limit = max(1, min(limit, 200))
    conn = connect()
    try:
        if q:
            return {"messages": handlers.search_email(conn, q, limit)}
        clause = " WHERE is_unread = 1" if unread_only else ""
        rows = conn.execute(
            "SELECT sender, subject, snippet, received_at, is_unread, thread_id"
            f" FROM email_messages{clause} ORDER BY received_at DESC LIMIT ?",  # noqa: S608
            (limit,),
        ).fetchall()
        return {"messages": [dict(r) for r in rows]}
    except sqlite3.OperationalError:
        return {"messages": []}
    finally:
        conn.close()


# ── pantry ────────────────────────────────────────────────


@app.post("/receipts", dependencies=[Depends(require_token)])
async def upload_receipt(
    background: BackgroundTasks, image: UploadFile = File(...)
) -> dict:
    """Upload a receipt photo. Returns immediately; extraction runs behind it.

    Vision on a receipt is 3-6 seconds. Blocking here would make the capture
    button feel broken, so the client gets a receipt id and polls
    GET /receipts/{id}, the same contract as /jobs/{id}.
    """
    payload = await image.read()
    media_type = image.content_type or ""
    if media_type not in images.MEDIA_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported image type {media_type!r}; "
            f"send one of {sorted(images.MEDIA_TYPES)}",
        )

    sha, path = images.store(payload, media_type)

    with transaction() as conn:
        existing = conn.execute(
            "SELECT id, status FROM receipts WHERE image_sha256 = ?", (sha,)
        ).fetchone()
        if existing:
            # Same photo, same receipt. Notably this is what keeps a receipt
            # you discarded discarded.
            return {"receipt_id": existing["id"], "status": existing["status"]}
        receipt_id = receipts.create(conn, sha, path)

    today = timeutil.now(config.DEFAULT_TZ).date().isoformat()
    background.add_task(receipts.fill, receipt_id, payload, media_type, today)
    return {"receipt_id": receipt_id, "status": "extracting"}


class ManualBatch(BaseModel):
    items: list[dict]
    purchased_on: str | None = None


@app.post("/receipts/manual", dependencies=[Depends(require_token)])
def create_manual_receipt(batch: ManualBatch) -> dict:
    """Type a list of food instead of photographing a receipt.

    Not everything comes with a receipt — a farmers market, a gift, or the
    baseline stocktake on day one. It lands in the same `pending` review
    screen a photograph does, because the dates are still being *proposed* by
    the shelf-life table, and that screen is where you agree to them.
    """
    if not batch.items:
        raise HTTPException(status_code=400, detail="no items")

    purchased_on = batch.purchased_on or timeutil.now(config.DEFAULT_TZ).date().isoformat()
    with transaction() as conn:
        receipt_id, count = receipts.create_manual(conn, batch.items, purchased_on)
    return {"receipt_id": receipt_id, "status": "pending", "items": count}


@app.get("/receipts/{receipt_id}", dependencies=[Depends(require_token)])
def get_receipt(receipt_id: int) -> dict:
    conn = connect()
    try:
        found = receipts.detail(conn, receipt_id)
    finally:
        conn.close()
    if found is None:
        raise HTTPException(status_code=404, detail="no such receipt")
    return found


class ItemEdits(BaseModel):
    items: list[dict]


@app.patch("/receipts/{receipt_id}/items", dependencies=[Depends(require_token)])
def edit_receipt_items(receipt_id: int, edits: ItemEdits) -> dict:
    with transaction() as conn:
        row = conn.execute(
            "SELECT status FROM receipts WHERE id = ?", (receipt_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="no such receipt")
        if row["status"] != "pending":
            # Past confirmation the items are real inventory and are edited as
            # inventory. A confirmed receipt is a record, not a live document.
            raise HTTPException(
                status_code=409, detail=f"receipt is {row['status']}, not pending"
            )
        touched = receipts.patch_items(conn, receipt_id, edits.items)
    return {"updated": touched}


@app.post("/receipts/{receipt_id}/confirm", dependencies=[Depends(require_token)])
def confirm_receipt(receipt_id: int) -> dict:
    with transaction() as conn:
        count = receipts.confirm(conn, receipt_id)
    if count is None:
        raise HTTPException(status_code=409, detail="receipt is not pending")
    return {"items": count, "reply": f"Added {count} items to the pantry."}


@app.post("/receipts/{receipt_id}/discard", dependencies=[Depends(require_token)])
def discard_receipt(receipt_id: int) -> dict:
    with transaction() as conn:
        ok = receipts.discard(conn, receipt_id)
    if not ok:
        raise HTTPException(status_code=409, detail="receipt is not pending")
    return {"discarded": True}


@app.get("/pantry", dependencies=[Depends(require_token)])
def pantry(location: str | None = None) -> dict:
    conn = connect()
    try:
        items = inventory.active(conn, location)
        listed = inventory.open_list(conn)
    finally:
        conn.close()
    return {"items": items, "shopping_list": listed}


class ListEntry(BaseModel):
    name: str


@app.post("/shopping-list", dependencies=[Depends(require_token)])
def add_shopping_entry(entry: ListEntry) -> dict:
    with transaction() as conn:
        entry_id = inventory.add_to_list(conn, None, entry.name, "manual")
    return {"id": entry_id, "added": entry_id is not None}


@app.delete("/shopping-list/{entry_id}", dependencies=[Depends(require_token)])
def resolve_shopping_entry(entry_id: int, purchased: bool = True) -> dict:
    with transaction() as conn:
        ok = inventory.resolve_list_entry(
            conn, None, entry_id, "purchased" if purchased else "removed"
        )
    if not ok:
        raise HTTPException(status_code=404, detail="no such list entry")
    return {"resolved": True}


# ── dashboard reads ───────────────────────────────────────


@app.get("/gratitude", dependencies=[Depends(require_token)])
def gratitude(days: int = 30, tz: str | None = None) -> dict:
    """Today's three, the streak, and the days behind it.

    Today is served separately from `days` rather than as the first group: the
    card and the history render differently, and merging them would make the
    view re-derive which group is 'now'.
    """
    days = max(1, min(days, 366))
    tz_name = tz or config.DEFAULT_TZ
    conn = connect()
    try:
        on = gratitude_entries.day_for(timeutil.now(tz_name))
        return {
            "today": {
                "on": on,
                "target": gratitude_entries.TARGET,
                "entries": gratitude_entries.for_day(conn, on),
            },
            "streak": gratitude_entries.streak(conn, tz_name),
            "days": gratitude_entries.recent(conn, tz_name, days),
        }
    finally:
        conn.close()


# ── projects ──────────────────────────────────────────────
# A project is a named space collecting one thing's notes, reports, dated
# items, links and files. Nothing here computes a status: the screen shows the
# rows, and "where am I on this" is answered live through query(kind='project').


class LinkCreate(BaseModel):
    url: str = Field(min_length=1)
    title: str | None = None

    @field_validator("url")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url cannot be blank")
        return value


class ProjectPatch(BaseModel):
    name: str | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def _known(cls, value: str | None) -> str | None:
        if value is not None and value not in projects_store.STATUSES:
            raise ValueError(f"status must be one of {projects_store.STATUSES}")
        return value


@app.get("/projects", dependencies=[Depends(require_token)])
def list_projects() -> dict:
    conn = connect()
    try:
        return {"projects": projects_store.listing(conn)}
    finally:
        conn.close()


def _project_detail(project_id: int, tz: str | None = None) -> dict:
    conn = connect()
    try:
        detail = projects_store.detail(conn, project_id, tz or config.DEFAULT_TZ)
    finally:
        conn.close()
    if detail is None:
        raise HTTPException(status_code=404, detail="no such project")
    return detail


@app.get("/projects/{project_id}", dependencies=[Depends(require_token)])
def project(project_id: int, tz: str | None = None) -> dict:
    return _project_detail(project_id, tz)


@app.post("/projects/{project_id}/links", dependencies=[Depends(require_token)])
def add_project_link(project_id: int, req: LinkCreate, tz: str | None = None) -> dict:
    """Paste a URL onto a project.

    Through the mutations helper: a human tapped this, and human actions are
    what /undo exists to reverse. A second paste of the same URL is not an
    error and not a second row.
    """
    with transaction() as conn:
        if projects_store.get(conn, project_id) is None:
            raise HTTPException(status_code=404, detail="no such project")
        projects_store.add_link(conn, None, project_id, req.url, req.title)
    return _project_detail(project_id, tz)


@app.patch("/projects/{project_id}", dependencies=[Depends(require_token)])
def edit_project(project_id: int, req: ProjectPatch, tz: str | None = None) -> dict:
    """Rename, or move it between active, paused and done.

    The slug is deliberately untouched by a rename: the working directory is
    named from it and reports already written quote paths inside it.
    """
    with transaction() as conn:
        if projects_store.get(conn, project_id) is None:
            raise HTTPException(status_code=404, detail="no such project")
        if req.name and req.name.strip():
            projects_store.rename(conn, project_id, req.name)
        if req.status:
            projects_store.set_status(conn, project_id, req.status)
    return _project_detail(project_id, tz)


@app.get("/projects/{project_id}/files/{name}", dependencies=[Depends(require_token)])
def project_file(project_id: int, name: str) -> dict:
    """One artifact the agent wrote.

    `workspace.read_text` resolves the name against the project directory and
    refuses anything that escapes it or is not text. Both come back as 404
    rather than 400: from the phone's side there is no such file either way,
    and a distinct error code would tell a caller which guesses were close.
    """
    from projects import workspace

    conn = connect()
    try:
        found = projects_store.get(conn, project_id)
    finally:
        conn.close()
    if found is None:
        raise HTTPException(status_code=404, detail="no such project")

    try:
        return {"name": name, "text": workspace.read_text(found, name)}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=404, detail="no such file") from exc


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


class JobReply(BaseModel):
    # Whitespace is not an answer, and the worker would fall back to re-running
    # the original ask against a session that has already moved past it.
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reply cannot be blank")
        return value


@app.post("/jobs/{job_id}/reply", dependencies=[Depends(require_token)])
def reply_to_job(job_id: int, req: JobReply) -> dict:
    """Answer a report that asked you something.

    The job is re-queued in place and resumes its Claude Code session, so a
    task you had to answer a question about stays one report.
    """
    with transaction() as conn:
        outcome = handlers.reply_to_job(conn, job_id, req.text)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="no such job")
    if outcome == "live":
        raise HTTPException(status_code=409, detail="job is already running")
    return job(job_id)


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


class SpeechRequest(BaseModel):
    # A reply is a sentence or two; the ceiling exists so a pasted deep-path
    # result cannot occupy the synthesizer for a minute.
    text: str = Field(min_length=1, max_length=4000)


# Length-prefixed WAVs, not one WAV: a four-byte big-endian length followed by
# that many bytes of complete, self-contained WAV, repeated until the body
# ends. Framing rather than raw PCM because the phone can then still tell
# audio from anything else — an unframed PCM stream would render a stray error
# body as a burst of noise, where a bad RIFF header is simply not playable and
# takes the Apple-voice path like every other failure.
CHUNKED_WAV = "audio/x-jarvis-chunked-wav"


@app.post("/speech", dependencies=[Depends(require_token)])
def speech(req: SpeechRequest) -> Response:
    """Text in, audio out, in clause-sized pieces. Unaware of `utterances`.

    A reply is not the only thing worth speaking — a job result or a
    notification body would use this too — and keying audio to a row would
    rule that out for no gain.

    The first chunk is pulled here, before the response exists, and that is
    load-bearing: a streaming response commits to its status code the moment
    the headers go out, so anything that can fail has to fail before then. An
    unspeakable voice or a dead worker therefore still reaches the phone as a
    clean 503, which is the signal it turns into "use the Apple voice". It
    costs nothing — the first chunk is what the phone is waiting for anyway.

    `def`, not `async def`: even though inference now happens on its own
    thread, this endpoint blocks waiting for it, and blocking the event loop
    would stall every other request. Starlette runs sync endpoints in a
    threadpool.
    """
    if not synth.available():
        # The phone turns this into "use the Apple voice". It is a normal
        # state on a machine that has never downloaded the weights, not an
        # error worth logging loudly.
        raise HTTPException(status_code=503, detail="tts unavailable")

    chunks = synth.stream(req.text)
    try:
        first = next(chunks)
    except StopIteration:
        raise HTTPException(status_code=503, detail="tts produced no audio") from None
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"tts failed: {exc}") from exc

    return StreamingResponse(
        _framed(first, chunks),
        media_type=CHUNKED_WAV,
        headers={"X-Synth-First-Ms": str(synth.last_first_chunk_ms or 0)},
    )


def _framed(first: bytes, rest: Iterator[bytes]) -> Iterator[bytes]:
    for chunk in itertools.chain([first], rest):
        yield len(chunk).to_bytes(4, "big")
        yield chunk
