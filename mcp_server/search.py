"""Note search.

Hybrid by construction, even though only one ranker exists today.

The design doc calls for FTS5 and vector search merged with reciprocal rank
fusion. There is no embedding provider configured — Anthropic has no
embeddings API, and adding a second vendor is a decision worth making
deliberately rather than by default. So `rrf()` below is the real merge
function and FTS5 is currently its only input; adding vectors later means
appending a second ranked list, not rewriting search.

The doc's own note is worth keeping in view while deciding whether to bother:
"For personal notes, keyword search alone beats embeddings surprisingly often
— exact names, project titles, and numbers are what you actually search for."
"""

import sqlite3

RRF_K = 60  # standard constant; damps the influence of any single ranker's top hit


def rrf(rankings: list[list[int]], k: int = RRF_K) -> list[int]:
    """Reciprocal rank fusion: score = Σ 1/(k + rank).

    Takes several ranked lists of row ids and returns one merged ranking.
    Deliberately ignores each ranker's raw scores — they're on incompatible
    scales (BM25 vs cosine distance), and rank position is the only thing
    that's comparable across them.
    """
    scores: dict[int, float] = {}
    for ranked in rankings:
        for position, row_id in enumerate(ranked):
            scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (k + position + 1)
    return sorted(scores, key=lambda row_id: -scores[row_id])


def _terms(query: str) -> list[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in query)
    return [w for w in cleaned.split() if len(w) > 2]


def fts_rank(conn: sqlite3.Connection, query: str, limit: int) -> list[int]:
    terms = _terms(query)
    if not terms:
        return []
    try:
        rows = conn.execute(
            """SELECT n.id FROM notes_fts f
                 JOIN notes n ON n.id = f.rowid
                 WHERE notes_fts MATCH ? AND n.deleted_at IS NULL
                 ORDER BY rank LIMIT ?""",
            (" OR ".join(terms), limit),
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        # MATCH throws on syntax it can't parse; fall through to LIKE.
        return []


def like_rank(conn: sqlite3.Connection, query: str, limit: int) -> list[int]:
    """Substring fallback. Catches what FTS5 tokenization drops — partial
    words, and anything inside a term shorter than the tokenizer's minimum."""
    terms = _terms(query)
    if not terms:
        return []
    clause = " OR ".join("body LIKE ?" for _ in terms)
    rows = conn.execute(
        f"""SELECT id FROM notes WHERE deleted_at IS NULL AND ({clause})
              ORDER BY id DESC LIMIT ?""",  # noqa: S608
        (*[f"%{t}%" for t in terms], limit),
    ).fetchall()
    return [r[0] for r in rows]


def search_notes(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    rankings = [
        fts_rank(conn, query, limit * 2),
        like_rank(conn, query, limit * 2),
        # vector_rank(conn, query, limit * 2)  <- drops in here when embeddings exist
    ]
    merged = rrf([r for r in rankings if r])[:limit]
    if not merged:
        return []

    marks = ",".join("?" for _ in merged)
    rows = conn.execute(
        f"""SELECT n.id, n.body, n.tags, n.created_at,
                   p.name AS person, pr.name AS project
              FROM notes n
              LEFT JOIN people p ON p.id = n.person_id
              LEFT JOIN projects pr ON pr.id = n.project_id
              WHERE n.id IN ({marks})""",  # noqa: S608
        merged,
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in merged if i in by_id]  # preserve fused order
