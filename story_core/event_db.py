"""SQLite event tracing engine — structured event logs with causality chains and CJK search."""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

_STATUS_LABELS = {
    "planted": "已埋",
    "triggered": "已觸發",
    "resolved": "已解決",
    "abandoned": "已廢棄",
}


def _parse_sticky_flag(raw_flag: object) -> bool | None:
    if raw_flag is None:
        return None
    if isinstance(raw_flag, bool):
        return raw_flag
    if isinstance(raw_flag, (int, float)):
        return raw_flag != 0
    if isinstance(raw_flag, str):
        value = raw_flag.strip().lower()
        if value in {"", "none", "null"}:
            return None
        if value in {"1", "true", "yes", "y", "on"}:
            return True
        if value in {"0", "false", "no", "n", "off"}:
            return False
    return bool(raw_flag)


def _normalize_sticky_priority(raw_priority: object, raw_sticky: object | None = None) -> int:
    if raw_priority is None:
        sticky_flag = _parse_sticky_flag(raw_sticky)
        if sticky_flag is not None:
            raw_priority = 1 if sticky_flag else 0
    try:
        priority = int(raw_priority or 0)
    except (TypeError, ValueError):
        priority = 0
    return max(0, min(3, priority))


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
            created_at      TEXT NOT NULL,
            sticky_priority INTEGER NOT NULL DEFAULT 0
        );
    """)
    try:
        conn.execute("ALTER TABLE events ADD COLUMN sticky_priority INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def insert_event(story_id: str, event: dict, branch_id: str) -> int:
    """Insert a new event. Returns the new event id."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    now = datetime.now(timezone.utc).isoformat()
    sticky_priority = _normalize_sticky_priority(
        event.get("sticky_priority"),
        event.get("sticky"),
    )
    cur = conn.execute(
        """INSERT INTO events (event_type, title, description, message_index,
           branch_id, status, tags, related_titles, created_at, sticky_priority)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            sticky_priority,
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


def update_event_sticky_priority(story_id: str, event_id: int, sticky_priority: int):
    """Update an event's sticky priority (0-3)."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    conn.execute(
        "UPDATE events SET sticky_priority = ? WHERE id = ?",
        (_normalize_sticky_priority(sticky_priority), event_id),
    )
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
               status, tags, related_titles, created_at, sticky_priority
               FROM events
               WHERE branch_id = ?
               ORDER BY id""",
            (source_branch_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT event_type, title, description, message_index,
               status, tags, related_titles, created_at, sticky_priority
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
           branch_id, status, tags, related_titles, created_at, sticky_priority)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                _normalize_sticky_priority(row["sticky_priority"]),
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
           status, tags, related_titles, created_at, sticky_priority
           FROM events
           WHERE branch_id = ?
           ORDER BY id""",
        (src_branch_id,),
    ).fetchall()
    if not src_rows:
        conn.close()
        return

    dst_rows = conn.execute(
        "SELECT id, title, sticky_priority FROM events WHERE branch_id = ?",
        (dst_branch_id,),
    ).fetchall()
    dst_title_to_meta = {
        row["title"]: {
            "id": row["id"],
            "sticky_priority": _normalize_sticky_priority(row["sticky_priority"]),
        }
        for row in dst_rows
    }

    # Keep latest src row per title in case src contains historical duplicates.
    src_by_title = {}
    for row in src_rows:
        src_by_title[row["title"]] = row

    inserts = []
    updates = []
    for title, row in src_by_title.items():
        dst_meta = dst_title_to_meta.get(title)
        if dst_meta is None:
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
                    _normalize_sticky_priority(row["sticky_priority"]),
                )
            )
        else:
            updates.append(
                (
                    row["status"],
                    _normalize_sticky_priority(row["sticky_priority"]),
                    dst_meta["id"],
                )
            )

    if inserts:
        conn.executemany(
            """INSERT INTO events (event_type, title, description, message_index,
               branch_id, status, tags, related_titles, created_at, sticky_priority)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            inserts,
        )
    if updates:
        conn.executemany(
            "UPDATE events SET status = ?, sticky_priority = ? WHERE id = ?",
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
        status_label = _STATUS_LABELS.get(e["status"], e["status"])
        lines.append(f"- [{e['event_type']}] {e['title']}（{status_label}）：{e['description'][:200]}")
    return "\n".join(lines)


def get_sticky_events(story_id: str, branch_id: str, limit: int = 4) -> list[dict]:
    """Return always-on sticky events for prompt injection."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM events
        WHERE branch_id = ? AND sticky_priority > 0
        ORDER BY sticky_priority DESC, id DESC
        LIMIT ?
        """,
        (branch_id, limit),
    ).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results


def format_sticky_events(story_id: str, branch_id: str, limit: int = 4) -> str:
    """Return formatted always-on sticky events for GM prompt injection."""
    results = get_sticky_events(story_id, branch_id, limit=limit)
    if not results:
        return ""
    lines = ["[長期關鍵事件]"]
    for event in results:
        status_label = _STATUS_LABELS.get(event["status"], event["status"])
        lines.append(
            f"- [{event['event_type']}] {event['title']}（{status_label}）：{event['description'][:200]}"
        )
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
        "SELECT id, title, status, sticky_priority FROM events WHERE branch_id = ?", (branch_id,)
    ).fetchall()
    result = {
        r["title"]: {
            "id": r["id"],
            "status": r["status"],
            "sticky_priority": _normalize_sticky_priority(r["sticky_priority"]),
        }
        for r in rows
    }
    conn.close()
    return result


def get_active_events(story_id: str, branch_id: str, limit: int = 40) -> list[dict]:
    """Return active events (planted/triggered) for extraction prompt context."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        """
        SELECT id, title, status, event_type
        FROM events
        WHERE branch_id = ? AND status IN ('planted', 'triggered')
        ORDER BY CASE status WHEN 'triggered' THEN 0 ELSE 1 END, id DESC
        LIMIT ?
        """,
        (branch_id, limit),
    ).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results


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
