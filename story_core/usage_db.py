"""SQLite usage/token tracking — per-story LLM call logs."""

import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta

log = logging.getLogger("rpg")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")


def _db_path(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "usage.db")


def _get_conn(story_id: str) -> sqlite3.Connection:
    path = _db_path(story_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Track which DBs have been initialized this process lifetime
_initialized: set[str] = set()


def _ensure_tables(conn: sqlite3.Connection, story_id: str = ""):
    if story_id and story_id in _initialized:
        return
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            provider        TEXT NOT NULL,
            model           TEXT NOT NULL,
            call_type       TEXT NOT NULL,
            prompt_tokens   INTEGER,
            output_tokens   INTEGER,
            total_tokens    INTEGER,
            cached_tokens   INTEGER,
            reasoning_tokens INTEGER,
            cost_usd        REAL,
            story_id        TEXT NOT NULL,
            branch_id       TEXT NOT NULL DEFAULT '',
            elapsed_ms      INTEGER
        );
        -- ISO-8601 timestamps sort lexicographically, so string comparison works
        CREATE INDEX IF NOT EXISTS idx_usage_log_timestamp ON usage_log(timestamp);
    """)
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(usage_log)").fetchall()
    }
    for column, definition in (
        ("cached_tokens", "INTEGER"),
        ("reasoning_tokens", "INTEGER"),
        ("cost_usd", "REAL"),
    ):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE usage_log ADD COLUMN {column} {definition}")
    conn.commit()
    if story_id:
        _initialized.add(story_id)


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def log_usage(
    story_id: str,
    provider: str,
    model: str,
    call_type: str,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    cost_usd: float | None = None,
    branch_id: str = "",
    elapsed_ms: int | None = None,
):
    """Insert a usage log entry."""
    conn = _get_conn(story_id)
    try:
        _ensure_tables(conn, story_id)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO usage_log
               (timestamp, provider, model, call_type,
                prompt_tokens, output_tokens, total_tokens,
                cached_tokens, reasoning_tokens, cost_usd,
                story_id, branch_id, elapsed_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, provider, model, call_type,
             prompt_tokens, output_tokens, total_tokens,
             cached_tokens, reasoning_tokens, cost_usd,
             story_id, branch_id, elapsed_ms),
        )
        conn.commit()
    finally:
        conn.close()


def log_from_bridge(
    story_id: str,
    call_type: str,
    elapsed_s: float,
    branch_id: str = "",
    usage: dict | None = None,
):
    """Convenience wrapper for background callers.

    Reads usage from llm_bridge.get_last_usage() if not provided,
    then calls log_usage() with the standard field mapping.
    """
    if usage is None:
        from story_core.llm_bridge import get_last_usage
        usage = get_last_usage()
    if usage is None:
        return
    try:
        log_usage(
            story_id=story_id,
            provider=usage.get("provider", ""),
            model=usage.get("model", ""),
            call_type=call_type,
            prompt_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            cached_tokens=usage.get("cached_tokens"),
            reasoning_tokens=usage.get("reasoning_tokens"),
            cost_usd=usage.get("cost_usd"),
            branch_id=branch_id,
            elapsed_ms=int(elapsed_s * 1000) if elapsed_s else None,
        )
    except Exception as e:
        log.debug("usage_db: log_from_bridge failed — %s", e)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def get_usage_summary(story_id: str, days: int = 7) -> dict:
    """Aggregate usage stats for a story over the last N days.

    Returns {total, by_day, by_provider, by_type}.
    """
    conn = _get_conn(story_id)
    try:
        _ensure_tables(conn, story_id)

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Total
        row = conn.execute(
            """SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                      COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                      COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                      COUNT(*) AS calls
               FROM usage_log WHERE timestamp >= ?""",
            (since,),
        ).fetchone()
        total = dict(row)

        # By day
        rows = conn.execute(
            """SELECT DATE(timestamp) AS date,
                      COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                      COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                      COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                      COUNT(*) AS calls
               FROM usage_log WHERE timestamp >= ?
               GROUP BY DATE(timestamp)
               ORDER BY date""",
            (since,),
        ).fetchall()
        by_day = [dict(r) for r in rows]

        # By provider/model
        rows = conn.execute(
            """SELECT provider, model,
                      COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                      COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                      COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                      COUNT(*) AS calls
               FROM usage_log WHERE timestamp >= ?
               GROUP BY provider, model
               ORDER BY total_tokens DESC""",
            (since,),
        ).fetchall()
        by_provider = [dict(r) for r in rows]

        # By call_type
        rows = conn.execute(
            """SELECT call_type,
                      COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                      COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                      COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                      COUNT(*) AS calls
               FROM usage_log WHERE timestamp >= ?
               GROUP BY call_type
               ORDER BY total_tokens DESC""",
            (since,),
        ).fetchall()
        by_type = [dict(r) for r in rows]
    finally:
        conn.close()

    return {
        "total": total,
        "by_day": by_day,
        "by_provider": by_provider,
        "by_type": by_type,
    }


def get_total_usage() -> dict:
    """Cross-story totals — scans all usage.db files.

    Returns {total: {prompt_tokens, output_tokens, total_tokens, calls},
             by_story: [{story_id, total_tokens, calls}]}.
    """
    grand = {
        "prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "cached_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0,
        "calls": 0,
    }
    by_story = []

    if not os.path.isdir(STORIES_DIR):
        return {"total": grand, "by_story": by_story}

    for story_id in os.listdir(STORIES_DIR):
        db = _db_path(story_id)
        if not os.path.isfile(db):
            continue
        try:
            conn = sqlite3.connect(db)
            try:
                conn.row_factory = sqlite3.Row
                _ensure_tables(conn, story_id)
                row = conn.execute(
                    """SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                              COALESCE(SUM(output_tokens), 0) AS output_tokens,
                              COALESCE(SUM(total_tokens), 0) AS total_tokens,
                              COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                              COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                              COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                              COUNT(*) AS calls
                       FROM usage_log"""
                ).fetchone()
                d = dict(row)
                grand["prompt_tokens"] += d["prompt_tokens"]
                grand["output_tokens"] += d["output_tokens"]
                grand["total_tokens"] += d["total_tokens"]
                grand["cached_tokens"] += d["cached_tokens"]
                grand["reasoning_tokens"] += d["reasoning_tokens"]
                grand["cost_usd"] += d["cost_usd"]
                grand["calls"] += d["calls"]
                by_story.append({"story_id": story_id, **d})
            finally:
                conn.close()
        except Exception as e:
            log.debug("usage_db: failed to read %s — %s", story_id, e)
            continue

    return {"total": grand, "by_story": by_story}
