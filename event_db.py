"""SQLite event tracing engine — structured event logs with causality chains and CJK search."""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")


def _db_path(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "events.db")


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
        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type      TEXT NOT NULL,
            title           TEXT NOT NULL,
            description     TEXT NOT NULL,
            message_index   INTEGER,
            branch_id       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'planted',
            tags            TEXT NOT NULL DEFAULT '',
            related_titles  TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL
        );
    """)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def insert_event(story_id: str, event: dict, branch_id: str) -> int:
    """Insert a new event. Returns the new event id."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO events (event_type, title, description, message_index,
           branch_id, status, tags, related_titles, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event.get("event_type", "遭遇"),
            event.get("title", ""),
            event.get("description", ""),
            event.get("message_index"),
            branch_id,
            event.get("status", "planted"),
            event.get("tags", ""),
            event.get("related_titles", ""),
            now,
        ),
    )
    conn.commit()
    event_id = cur.lastrowid
    conn.close()
    return event_id


def update_event_status(story_id: str, event_id: int, new_status: str):
    """Update an event's status (planted/triggered/resolved/abandoned)."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    conn.execute("UPDATE events SET status = ? WHERE id = ?", (new_status, event_id))
    conn.commit()
    conn.close()


def get_events(story_id: str, branch_id: str | None = None, limit: int = 50) -> list[dict]:
    """Get events, optionally filtered by branch_id."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    if branch_id:
        rows = conn.execute(
            "SELECT * FROM events WHERE branch_id = ? ORDER BY id DESC LIMIT ?",
            (branch_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    results = [dict(r) for r in rows]
    conn.close()
    return results


def get_event_by_id(story_id: str, event_id: int) -> dict | None:
    """Get a single event by id."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Search (CJK bigram scoring — same pattern as lore_db.py)
# ---------------------------------------------------------------------------

def search_events(story_id: str, query: str, branch_id: str | None = None, limit: int = 5) -> list[dict]:
    """Search events using CJK bigram keyword scoring."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    # Extract CJK bigrams from query
    cjk_runs = re.findall(r'[\u4e00-\u9fff]+', query)
    keywords = set()
    for run in cjk_runs:
        for i in range(len(run) - 1):
            keywords.add(run[i:i+2])
        for i in range(len(run) - 2):
            keywords.add(run[i:i+3])
    if not keywords:
        keywords = {query}

    if branch_id:
        rows = conn.execute(
            "SELECT * FROM events WHERE branch_id = ?", (branch_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events").fetchall()

    scored = []
    for row in rows:
        text = f"{row['title']} {row['description']} {row['tags']} {row['related_titles']}"
        score = 0
        for kw in keywords:
            if kw in text:
                if kw in row['title']:
                    score += 10
                if kw in row['tags']:
                    score += 5
                if kw in row['description']:
                    score += 1
        if score > 0:
            scored.append({**dict(row), "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    conn.close()
    return scored[:limit]


def search_relevant_events(story_id: str, user_message: str, branch_id: str, limit: int = 3) -> str:
    """Search for events relevant to a user message. Returns formatted text for injection."""
    results = search_events(story_id, user_message, branch_id=branch_id, limit=limit)
    if not results:
        return ""

    lines = ["[相關事件追蹤]"]
    for e in results:
        status_label = {"planted": "已埋", "triggered": "已觸發", "resolved": "已解決", "abandoned": "已廢棄"}.get(e["status"], e["status"])
        lines.append(f"- [{e['event_type']}] {e['title']}（{status_label}）：{e['description'][:200]}")
    return "\n".join(lines)


def get_active_foreshadowing(story_id: str, branch_id: str) -> list[dict]:
    """Get planted events not yet triggered for a branch."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT * FROM events WHERE branch_id = ? AND status = 'planted' ORDER BY id",
        (branch_id,),
    ).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results
