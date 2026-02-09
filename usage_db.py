"""SQLite usage/token tracking — per-story LLM call logs."""

import os
import sqlite3
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")


def _db_path(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "usage.db")


def _get_conn(story_id: str) -> sqlite3.Connection:
    path = _db_path(story_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _ensure_tables(conn: sqlite3.Connection):
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
            story_id        TEXT NOT NULL,
            branch_id       TEXT NOT NULL DEFAULT '',
            elapsed_ms      INTEGER
        );
    """)


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
    branch_id: str = "",
    elapsed_ms: int | None = None,
):
    """Insert a usage log entry."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO usage_log
           (timestamp, provider, model, call_type,
            prompt_tokens, output_tokens, total_tokens,
            story_id, branch_id, elapsed_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now, provider, model, call_type,
         prompt_tokens, output_tokens, total_tokens,
         story_id, branch_id, elapsed_ms),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def get_usage_summary(story_id: str, days: int = 7) -> dict:
    """Aggregate usage stats for a story over the last N days.

    Returns {total, by_day, by_provider, by_type}.
    """
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Total
    row = conn.execute(
        """SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(total_tokens), 0) AS total_tokens,
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
                  COALESCE(SUM(total_tokens), 0) AS total_tokens,
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
                  COALESCE(SUM(total_tokens), 0) AS total_tokens,
                  COUNT(*) AS calls
           FROM usage_log WHERE timestamp >= ?
           GROUP BY call_type
           ORDER BY total_tokens DESC""",
        (since,),
    ).fetchall()
    by_type = [dict(r) for r in rows]

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
    grand = {"prompt_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
    by_story = []

    if not os.path.isdir(STORIES_DIR):
        return {"total": grand, "by_story": by_story}

    for story_id in os.listdir(STORIES_DIR):
        db = _db_path(story_id)
        if not os.path.isfile(db):
            continue
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            _ensure_tables(conn)
            row = conn.execute(
                """SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(total_tokens), 0) AS total_tokens,
                          COUNT(*) AS calls
                   FROM usage_log"""
            ).fetchone()
            d = dict(row)
            grand["prompt_tokens"] += d["prompt_tokens"]
            grand["output_tokens"] += d["output_tokens"]
            grand["total_tokens"] += d["total_tokens"]
            grand["calls"] += d["calls"]
            by_story.append({"story_id": story_id, **d})
            conn.close()
        except Exception:
            continue

    return {"total": grand, "by_story": by_story}
