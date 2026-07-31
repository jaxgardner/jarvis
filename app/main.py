"""FastAPI entrypoint.

    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

Bind 0.0.0.0 so the phone can reach it over Tailscale. Nothing is exposed to
the public internet — there is no port forward, and /say requires a bearer
token regardless.
"""

import sqlite3

from fastapi import FastAPI

from app import config
from app.db import connect

app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)


@app.get("/health")
def health() -> dict:
    """Liveness for launchd, uptime checks, and the cellular smoke test.

    Reports DB reachability and which secrets are configured (never their
    values) so a half-configured install is obvious from the phone.
    """
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
