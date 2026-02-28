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


def copy_events_for_fork(story_id: str, source_branch_id: str, target_branch_id: str,
                         branch_point_index: int | None):
    """Copy source branch events into a forked branch.

    If branch_point_index is provided, only events at or before that index are
    copied. Legacy events without message_index are kept conservatively.
    """
    if source_branch_id == target_branch_id:
        return

    conn = _get_conn(story_id)
    _ensure_tables(conn)

    if branch_point_index is None:
        rows = conn.execute(
            """SELECT event_type, title, description, message_index,
               status, tags, related_titles, created_at
               FROM events
               WHERE branch_id = ?
               ORDER BY id""",
            (source_branch_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT event_type, title, description, message_index,
               status, tags, related_titles, created_at
               FROM events
               WHERE branch_id = ?
                 AND (message_index <= ? OR message_index IS NULL)
               ORDER BY id""",
            (source_branch_id, branch_point_index),
        ).fetchall()

    if not rows:
        conn.close()
        return

    conn.executemany(
        """INSERT INTO events (event_type, title, description, message_index,
           branch_id, status, tags, related_titles, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                row["event_type"],
                row["title"],
                row["description"],
                row["message_index"],
                target_branch_id,
                row["status"],
                row["tags"],
                row["related_titles"],
                row["created_at"],
            )
            for row in rows
        ],
    )
    conn.commit()
    conn.close()


def merge_events_into(story_id: str, src_branch_id: str, dst_branch_id: str):
    """Merge source branch events into destination by title.

    - New titles in src are inserted into dst.
    - Existing titles in dst have status overwritten by src status.
    """
    if src_branch_id == dst_branch_id:
        return

    conn = _get_conn(story_id)
    _ensure_tables(conn)

    src_rows = conn.execute(
        """SELECT event_type, title, description, message_index,
           status, tags, related_titles, created_at
           FROM events
           WHERE branch_id = ?
           ORDER BY id""",
        (src_branch_id,),
    ).fetchall()
    if not src_rows:
        conn.close()
        return

    dst_rows = conn.execute(
        "SELECT id, title FROM events WHERE branch_id = ?",
        (dst_branch_id,),
    ).fetchall()
    dst_title_to_id = {row["title"]: row["id"] for row in dst_rows}

    # Keep latest src row per title in case src contains historical duplicates.
    src_by_title = {}
    for row in src_rows:
        src_by_title[row["title"]] = row

    inserts = []
    updates = []
    for title, row in src_by_title.items():
        dst_id = dst_title_to_id.get(title)
        if dst_id is None:
            inserts.append(
                (
                    row["event_type"],
                    row["title"],
                    row["description"],
                    row["message_index"],
                    dst_branch_id,
                    row["status"],
                    row["tags"],
                    row["related_titles"],
                    row["created_at"],
                )
            )
        else:
            updates.append((row["status"], dst_id))

    if inserts:
        conn.executemany(
            """INSERT INTO events (event_type, title, description, message_index,
               branch_id, status, tags, related_titles, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            inserts,
        )
    if updates:
        conn.executemany(
            "UPDATE events SET status = ? WHERE id = ?",
            updates,
        )

    conn.commit()
    conn.close()


def delete_events_for_branch(story_id: str, branch_id: str):
    """Delete all events belonging to a branch."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    conn.execute("DELETE FROM events WHERE branch_id = ?", (branch_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Search (CJK bigram scoring — same pattern as lore_db.py)
# ---------------------------------------------------------------------------

def search_events(story_id: str, query: str, branch_id: str | None = None, limit: int = 5, active_only: bool = False) -> list[dict]:
    """Search events using CJK bigram keyword scoring.

    active_only: if True, only return planted/triggered events (for GM context
    injection). Resolved/abandoned events are excluded to prevent the GM from
    re-issuing rewards or repeating completed events.
    """
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

    active_filter = "AND status IN ('planted', 'triggered')" if active_only else ""
    if branch_id:
        rows = conn.execute(
            f"SELECT * FROM events WHERE branch_id = ? {active_filter}", (branch_id,)
        ).fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM events WHERE 1=1 {active_filter}").fetchall()

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
    results = search_events(story_id, user_message, branch_id=branch_id, limit=limit, active_only=True)
    if not results:
        return ""

    lines = ["[相關事件追蹤]"]
    for e in results:
        status_label = {"planted": "已埋", "triggered": "已觸發", "resolved": "已解決", "abandoned": "已廢棄"}.get(e["status"], e["status"])
        lines.append(f"- [{e['event_type']}] {e['title']}（{status_label}）：{e['description'][:200]}")
    return "\n".join(lines)


def get_event_titles(story_id: str, branch_id: str) -> set[str]:
    """Return set of existing event titles for dedup."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT title FROM events WHERE branch_id = ?", (branch_id,)
    ).fetchall()
    titles = {r["title"] for r in rows}
    conn.close()
    return titles


def get_event_title_map(story_id: str, branch_id: str) -> dict[str, dict]:
    """Return map of title → {id, status} for dedup with status update."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT id, title, status FROM events WHERE branch_id = ?", (branch_id,)
    ).fetchall()
    result = {r["title"]: {"id": r["id"], "status": r["status"]} for r in rows}
    conn.close()
    return result


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
