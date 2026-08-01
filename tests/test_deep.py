"""Search fusion and worker mechanics. No network."""

import json
import os
import sqlite3
import subprocess
import tempfile

import pytest

from app import mutations
from app.config import REPO_ROOT
from mcp_server import search
from worker import run as worker


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    for name in ("001_init.sql", "002_scheduler.sql"):
        sql = (REPO_ROOT / "migrations" / name).read_text()
        c.executescript(
            "\n".join(
                l for l in sql.splitlines() if not l.strip().upper().startswith("PRAGMA")
            )
        )
    yield c
    c.close()


# ── reciprocal rank fusion ────────────────────────────────


def test_rrf_merges_two_rankings():
    """A row both rankers like beats one that only tops a single list."""
    fts = [10, 20, 30]
    vec = [20, 40, 10]
    assert search.rrf([fts, vec])[0] == 20


def test_rrf_ignores_incomparable_scores_by_using_rank_only():
    """Position is the only thing comparable across BM25 and cosine distance,
    which is the entire reason RRF is the merge strategy."""
    assert search.rrf([[1, 2]]) == [1, 2]
    assert search.rrf([[2, 1]]) == [2, 1]


def test_rrf_handles_a_single_ranker():
    """Today FTS is the only input — the merge must be a no-op, not a crash."""
    assert search.rrf([[5, 6, 7]]) == [5, 6, 7]


def test_rrf_dedupes_across_rankers():
    merged = search.rrf([[1, 2], [2, 1]])
    assert sorted(merged) == [1, 2] and len(merged) == 2


# ── search ────────────────────────────────────────────────


def test_search_finds_a_note(conn):
    mutations.insert(conn, None, "notes", {"body": "Sarah's kid is named Theo"})
    results = search.search_notes(conn, "Theo")
    assert len(results) == 1 and "Theo" in results[0]["body"]


def test_search_excludes_soft_deleted_notes(conn):
    row_id = mutations.insert(conn, None, "notes", {"body": "obsolete fact"})
    mutations.soft_delete(conn, None, "notes", row_id)
    assert search.search_notes(conn, "obsolete") == []


def test_like_fallback_catches_what_fts_tokenization_misses(conn):
    """FTS5 tokenizes on word boundaries, so a substring inside a longer token
    is invisible to it. The LIKE ranker is what covers that."""
    mutations.insert(conn, None, "notes", {"body": "order number ABC12345XYZ"})
    assert search.fts_rank(conn, "12345XYZ", 10) == []
    assert search.like_rank(conn, "12345XYZ", 10) != []
    assert search.search_notes(conn, "12345XYZ") != []


def test_search_returns_empty_for_nonsense(conn):
    mutations.insert(conn, None, "notes", {"body": "something"})
    assert search.search_notes(conn, "??? !!!") == []


# ── worker ────────────────────────────────────────────────


def test_child_env_strips_the_api_key(monkeypatch):
    """The deep path must run on the OAuth subscription. app.config loads .env
    into os.environ, and a child inherits it — leaving the key set would
    silently bill every job to the API credit balance instead."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    env = worker._child_env({"utterance_id": None})
    assert "ANTHROPIC_API_KEY" not in env
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-should-not-leak"  # parent intact


def test_child_env_passes_utterance_id_for_the_audit_trail(monkeypatch):
    env = worker._child_env({"utterance_id": 7})
    assert env["JARVIS_UTTERANCE_ID"] == "7"


def test_new_job_assigns_a_session_id_rather_than_resuming():
    cmd = worker._command({"prompt": "x"}, "abc-123", resume=False)
    assert "--session-id" in cmd and "--resume" not in cmd


def test_follow_up_job_resumes_the_stored_session():
    cmd = worker._command({"prompt": "x"}, "abc-123", resume=True)
    assert "--resume" in cmd and "--session-id" not in cmd


def test_command_uses_auto_permission_mode():
    """Auto mode judges each action in context, the same way an interactive
    session does. --allowedTools still bounds the tool set independently."""
    cmd = worker._command({"prompt": "x"}, "s", resume=False)
    assert cmd[cmd.index("--permission-mode") + 1] == "auto"


def test_command_pins_mcp_config_strictly():
    """Without --strict-mcp-config the CLI also loads the user's own MCP
    servers, handing the job tools this code never granted."""
    assert "--strict-mcp-config" in worker._command({"prompt": "x"}, "s", resume=False)


def test_full_access_omits_the_allowlist_entirely():
    """ALLOWED_TOOLS=None means no --allowedTools flag, so the agent gets the
    default tool set including Bash, gated by auto mode rather than by name."""
    cmd = worker._command({"prompt": "x"}, "s", resume=False)
    assert worker.ALLOWED_TOOLS is None
    assert "--allowedTools" not in cmd


def test_allowlist_is_applied_when_set(monkeypatch):
    """Re-narrowing must still work — this is the one-line rollback."""
    monkeypatch.setattr(worker, "ALLOWED_TOOLS", "Read,WebSearch")
    cmd = worker._command({"prompt": "x"}, "s", resume=False)
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,WebSearch"


def test_jobs_do_not_run_inside_the_repo():
    """With a shell available, cwd must not be the directory holding .env."""
    assert worker.WORK_DIR != REPO_ROOT
    assert REPO_ROOT not in worker.WORK_DIR.parents


def test_mcp_server_starts_from_outside_the_repo():
    """The MCP server has to import from the job's working directory.

    Regression, and an expensive one to diagnose: jobs run in WORK_DIR, and the
    `cwd` key in mcp.json is not honoured — Claude Code spawns the server with
    the parent's working directory. `python -m mcp_server.server` then died with
    ModuleNotFoundError, the CLI reported no MCP failure at all, and the agent
    simply had no tools. The job "succeeded", answering that it had no way to
    search email. PYTHONPATH is what makes the import cwd-independent.

    Spawning it for real rather than asserting on the JSON: the thing that broke
    was whether the process starts, which only running it can answer.
    """
    config = json.loads(worker.MCP_CONFIG.read_text())["mcpServers"]["jarvis"]
    env = {**os.environ, **config.get("env", {})}
    env.pop("PYTHONHOME", None)

    with tempfile.TemporaryDirectory() as elsewhere:
        completed = subprocess.run(  # noqa: S603
            [config["command"], *config["args"]],
            cwd=elsewhere,
            env=env,
            input="",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    # A stdio server given a closed stdin shuts down cleanly; what matters is
    # that it got far enough to import itself.
    assert "ModuleNotFoundError" not in completed.stderr, completed.stderr
    assert completed.returncode == 0, completed.stderr


def test_summary_is_one_line():
    assert worker._summarize("first line.\nsecond line.\nthird.").startswith("first line.")


def test_summary_drops_a_lead_in_that_promises_a_table():
    """Real failure: the agent opened "Saved as note 3. Here's the comparison:"
    with a markdown table under it. Keeping the colon clause either dangles
    mid-thought or glues the label onto the next paragraph, asserting a
    comparison the agent never made."""
    out = worker._summarize(
        "Saved as note 3. Here's the comparison:\n\n| A | B |\n|---|---|\n| 1 | 2 |"
    )
    assert out == "Saved as note 3."


def test_summary_does_not_split_decimals():
    """A naive [.!?] split ends the summary at "Its 27." mid-measurement."""
    assert worker._summarize("The desk is 27.2 inches tall.") == "The desk is 27.2 inches tall."


def test_summary_strips_markdown_emphasis():
    assert worker._summarize("Saved **three** desks to `notes`.") == "Saved three desks to notes."


def test_summary_of_structure_only_output_says_something():
    assert worker._summarize("- one\n- two") == "Done."
    assert worker._summarize("Here are the results:") == "Done."


def test_auth_configured_reports_headless_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert worker.auth_configured() is False
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    assert worker.auth_configured() is True


def test_child_env_passes_the_headless_token(monkeypatch):
    """A LaunchDaemon cannot read the login keychain, so the token from
    `claude setup-token` is the only way the worker authenticates."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    assert worker._child_env({"utterance_id": None})["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-x"


def test_summary_truncates_rather_than_flooding_the_notification():
    assert len(worker._summarize("x" * 500)) <= 240


def test_summary_survives_empty_output():
    assert worker._summarize("") == "Done."


# ── pantry, over the deep path ────────────────────────────


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A file-backed database, because the MCP tools call app.db.connect()
    themselves rather than taking a connection."""
    from tests.helpers import apply_migrations

    path = tmp_path / "deep.db"
    apply_migrations(path)

    import app.db as appdb

    monkeypatch.setattr(appdb, "DB_PATH", path)
    return path


def test_pantry_inventory_leads_with_what_is_dying(db):
    """The sort is the whole point. The agent sees 'spinach: 2 days' first
    and builds the suggestion around it instead of producing something
    generic from an unordered list."""
    from app.db import transaction
    from mcp_server import server

    with transaction() as conn:
        for name, expires in [
            ("dried pasta", None),
            ("whole milk", "2099-08-07"),
            ("spinach", "2099-08-02"),
        ]:
            conn.execute(
                """INSERT INTO pantry_items (name, expires_on, status, location)
                     VALUES (?,?, 'active', 'fridge')""",
                (name, expires),
            )

    output = server.pantry_inventory()

    assert output.index("spinach") < output.index("whole milk") < output.index("dried pasta")


def test_pantry_inventory_reports_days_left_not_iso_dates(db):
    """The agent is being asked what to cook tonight. 'in 2 days' is the
    useful framing; a date makes it do arithmetic it gets wrong."""
    from app.db import transaction
    from mcp_server import server

    with transaction() as conn:
        conn.execute(
            """INSERT INTO pantry_items (name, expires_on, status, location)
                 VALUES ('spinach', date('now','+2 days'), 'active', 'fridge')"""
        )

    output = server.pantry_inventory()
    assert "2 days" in output


def test_pantry_inventory_excludes_unreviewed_items(db):
    from app.db import transaction
    from mcp_server import server

    with transaction() as conn:
        conn.execute(
            "INSERT INTO pantry_items (name, status) VALUES ('unreviewed', 'pending')"
        )

    assert "unreviewed" not in server.pantry_inventory()


def test_pantry_inventory_says_so_when_the_fridge_is_empty(db):
    from mcp_server import server

    assert "nothing" in server.pantry_inventory().lower()


def test_pantry_inventory_includes_the_shopping_list(db):
    from app.db import transaction
    from mcp_server import server

    with transaction() as conn:
        conn.execute("INSERT INTO shopping_list (name, reason) VALUES ('eggs','out')")

    assert "eggs" in server.pantry_inventory()
