"""SQLite FTS5 search engine for world_lore — supports CJK full-text + tag search."""

import json
import logging
import os
import re
import sqlite3

log = logging.getLogger("rpg")

VALID_LORE_CATEGORIES = {
    "主神設定與規則", "體系", "商城", "副本世界觀",
    "場景", "NPC", "故事追蹤",
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STORIES_DIR = os.path.join(DATA_DIR, "stories")

_TAG_RE = re.compile(r"\[tag:\s*([^\]]+)\]")
_INLINE_META_RE = re.compile(r"\s*\[(?:tag|source):\s*[^\]]*\]")


def _db_path(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "lore.db")


def _lore_json_path(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "world_lore.json")


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
        CREATE TABLE IF NOT EXISTS lore (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            category  TEXT NOT NULL,
            topic     TEXT NOT NULL UNIQUE,
            content   TEXT NOT NULL,
            tags      TEXT NOT NULL DEFAULT ''
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS lore_fts USING fts5(
            topic, content, category, tags,
            content='lore',
            content_rowid='id',
            tokenize='trigram'
        );

        CREATE TRIGGER IF NOT EXISTS lore_ai AFTER INSERT ON lore BEGIN
            INSERT INTO lore_fts(rowid, topic, content, category, tags)
            VALUES (new.id, new.topic, new.content, new.category, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS lore_ad AFTER DELETE ON lore BEGIN
            INSERT INTO lore_fts(lore_fts, rowid, topic, content, category, tags)
            VALUES ('delete', old.id, old.topic, old.content, old.category, old.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS lore_au AFTER UPDATE ON lore BEGIN
            INSERT INTO lore_fts(lore_fts, rowid, topic, content, category, tags)
            VALUES ('delete', old.id, old.topic, old.content, old.category, old.tags);
            INSERT INTO lore_fts(rowid, topic, content, category, tags)
            VALUES (new.id, new.topic, new.content, new.category, new.tags);
        END;
    """)


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------

def extract_tags(content: str) -> list[str]:
    """Extract all [tag: x/y/z] from content, return flat deduplicated tag list."""
    tags = []
    for m in _TAG_RE.finditer(content):
        for part in m.group(1).split("/"):
            t = part.strip()
            if t:
                tags.append(t)
    return list(dict.fromkeys(tags))  # deduplicate preserving order


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

def rebuild_index(story_id: str):
    """Rebuild the SQLite FTS index from world_lore.json."""
    json_path = _lore_json_path(story_id)
    if not os.path.exists(json_path):
        return

    with open(json_path, "r", encoding="utf-8") as f:
        lore_entries = json.load(f)

    conn = _get_conn(story_id)
    _ensure_tables(conn)

    # Clear and rebuild
    conn.execute("DELETE FROM lore")
    skipped = 0
    for entry in lore_entries:
        content = entry.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if content.startswith("（待建立）"):
            continue
        category = entry.get("category", "其他").strip().strip("【】").strip()
        if category not in VALID_LORE_CATEGORIES:
            skipped += 1
            log.warning("    lore_rebuild: skipping entry with invalid category '%s' (topic: %s)",
                        category, entry.get("topic", "?")[:40])
            continue
        tags = extract_tags(content)
        conn.execute(
            "INSERT OR REPLACE INTO lore (category, topic, content, tags) VALUES (?, ?, ?, ?)",
            (
                category,
                entry.get("topic", ""),
                content,
                ",".join(tags),
            ),
        )
    if skipped:
        log.info("    lore_rebuild: skipped %d entries with invalid categories", skipped)
    conn.commit()
    conn.close()


def upsert_entry(story_id: str, entry: dict):
    """Insert or update a single lore entry in the index."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    content = entry.get("content", "")
    if not isinstance(content, str):
        content = str(content)
    tags = extract_tags(content)
    topic = entry.get("topic", "").strip()
    if not topic:
        conn.close()
        return

    existing = conn.execute("SELECT id FROM lore WHERE topic = ?", (topic,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE lore SET category=?, content=?, tags=? WHERE topic=?",
            (entry.get("category", "其他"), content, ",".join(tags), topic),
        )
    else:
        conn.execute(
            "INSERT INTO lore (category, topic, content, tags) VALUES (?, ?, ?, ?)",
            (entry.get("category", "其他"), topic, content, ",".join(tags)),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_lore(story_id: str, query: str, limit: int = 5) -> list[dict]:
    """Search lore entries using LIKE matching (reliable for CJK text).

    Scores results by number of keyword matches across topic, content, and tags.
    """
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    # Extract CJK bigrams from query for keyword matching
    cjk_runs = re.findall(r'[\u4e00-\u9fff]+', query)
    keywords = set()
    for run in cjk_runs:
        # Generate bigrams (2-char) and trigrams (3-char) for CJK
        for i in range(len(run) - 1):
            keywords.add(run[i:i+2])
        for i in range(len(run) - 2):
            keywords.add(run[i:i+3])
    if not keywords:
        keywords = {query}

    # Score each entry by how many keywords match
    rows = conn.execute("SELECT category, topic, content, tags FROM lore").fetchall()
    scored = []
    for row in rows:
        text = f"{row['topic']} {row['content']} {row['tags']}"
        score = 0
        for kw in keywords:
            if kw in text:
                # Weight: topic match > tag match > content match
                if kw in row['topic']:
                    score += 10
                if kw in row['tags']:
                    score += 5
                if kw in row['content']:
                    score += 1
        if score > 0:
            scored.append({
                "category": row["category"],
                "topic": row["topic"],
                "content": row["content"],
                "tags": row["tags"],
                "score": score,
            })

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)
    conn.close()
    return scored[:limit]


def search_by_tags(story_id: str, tags: list[str], limit: int = 10) -> list[dict]:
    """Find entries that match any of the given tags."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    placeholders = " OR ".join(["tags LIKE ?" for _ in tags])
    params = [f"%{tag}%" for tag in tags] + [limit]

    rows = conn.execute(
        f"SELECT category, topic, content, tags FROM lore WHERE ({placeholders}) LIMIT ?",
        params,
    ).fetchall()

    results = [
        {"category": r["category"], "topic": r["topic"], "content": r["content"], "tags": r["tags"]}
        for r in rows
    ]
    conn.close()
    return results


def get_all_entries(story_id: str) -> list[dict]:
    """Get all indexed lore entries (non-待建立 only)."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute("SELECT category, topic, content, tags FROM lore ORDER BY id").fetchall()
    results = [
        {"category": r["category"], "topic": r["topic"], "content": r["content"], "tags": r["tags"]}
        for r in rows
    ]
    conn.close()
    return results


def get_toc(story_id: str) -> str:
    """Build a hierarchical table-of-contents string for system prompt injection.

    Topics use full-width colon (：) as hierarchy separator.
    Output is an indented tree so the LLM sees knowledge structure.
    """
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT category, topic, tags FROM lore ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        return "（尚無已確立的世界設定）"

    from collections import OrderedDict

    # Group rows by category
    cat_rows: dict[str, list] = OrderedDict()
    for r in rows:
        cat = r["category"]
        if cat not in cat_rows:
            cat_rows[cat] = []
        cat_rows[cat].append(r)

    lines = []
    for cat, entries in cat_rows.items():
        lines.append(f"### 【{cat}】")

        # Build tree: prefix → list of suffixes
        # A topic like "A：B：C" yields tree node A > B > C
        tree: dict = OrderedDict()  # nested ordered dicts
        for r in entries:
            parts = r["topic"].split("：")
            node = tree
            for i, part in enumerate(parts):
                if part not in node:
                    node[part] = OrderedDict()
                node = node[part]

        def _render(node: dict, depth: int):
            indent = "  " * depth
            for key, child in node.items():
                lines.append(f"{indent}- {key}")
                if child:
                    _render(child, depth + 1)

        _render(tree, 0)
        lines.append("")

    return "\n".join(lines).strip()


def delete_entry(story_id: str, topic: str):
    """Delete a lore entry from the search index by topic."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    conn.execute("DELETE FROM lore WHERE topic = ?", (topic,))
    conn.commit()
    conn.close()


def search_relevant_lore(story_id: str, user_message: str, limit: int = 5) -> str:
    """Search for lore relevant to a user message. Returns formatted text for injection."""
    # Just use the main search function which handles CJK bigram extraction
    results = search_lore(story_id, user_message, limit=limit)

    if not results:
        return ""

    entries = results

    lines = ["[相關世界設定]"]
    for e in entries:
        # Strip inline [tag: ...] and [source: ...] markers — already indexed in tags column
        content = _INLINE_META_RE.sub("", e["content"]).strip()
        if len(content) > 800:
            content = content[:800] + "…（截斷）"
        lines.append(f"#### {e['category']}：{e['topic']}")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)
