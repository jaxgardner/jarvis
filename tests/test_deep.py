"""Phase 3 tests — search fusion and worker mechanics. No network."""

import os
import sqlite3

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


def test_command_pins_mcp_config_strictly():
    """Without --strict-mcp-config the CLI also loads the user's own MCP
    servers, handing the job tools this code never granted."""
    cmd = worker._command({"prompt": "x"}, "s", resume=False)
    assert "--strict-mcp-config" in cmd
    assert "Bash" not in cmd[cmd.index("--allowedTools") + 1]


def test_summary_is_one_line():
    assert worker._summarize("first line\nsecond line\nthird") == "first line"


def test_summary_truncates_rather_than_flooding_the_notification():
    assert len(worker._summarize("x" * 500)) <= 240


def test_summary_survives_empty_output():
    assert worker._summarize("") == "Done."
