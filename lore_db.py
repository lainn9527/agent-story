"""SQLite FTS5 search engine for world_lore — supports CJK full-text + embedding hybrid search."""

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading

import numpy as np

log = logging.getLogger("rpg")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STORIES_DIR = os.path.join(DATA_DIR, "stories")
STORY_DESIGN_DIR = os.path.join(BASE_DIR, "story_design")

_TAG_RE = re.compile(r"\[tag:\s*([^\]]+)\]")
_INLINE_META_RE = re.compile(r"\s*\[(?:tag|source):\s*[^\]]*\]")

EMBEDDING_DIM = 768
RRF_K = 60  # Reciprocal Rank Fusion constant
DEFAULT_TOKEN_BUDGET = 3000  # ~3000 CJK chars ≈ 3000 tokens


def _db_path(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "lore.db")


def _lore_json_path(story_id: str) -> str:
    return os.path.join(STORY_DESIGN_DIR, story_id, "world_lore.json")


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
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            category     TEXT NOT NULL,
            subcategory  TEXT NOT NULL DEFAULT '',
            topic        TEXT NOT NULL,
            content      TEXT NOT NULL,
            tags         TEXT NOT NULL DEFAULT '',
            UNIQUE(subcategory, topic)
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
    # Safe migration: add columns if not yet present
    for col, typ in [("embedding", "BLOB"), ("text_hash", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE lore ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Migrate old schema: topic UNIQUE → UNIQUE(subcategory, topic)
    # Detect old schema by trying to insert two rows with same topic but different subcategory
    _migrate_composite_unique(conn)


def _migrate_composite_unique(conn: sqlite3.Connection):
    """Migrate old schema (topic UNIQUE) to new schema (UNIQUE(subcategory, topic)).

    Detects old schema by checking table_info for the unique constraint structure.
    If old schema detected, recreates table preserving all data including embeddings.
    """
    # Check if 'subcategory' column exists
    cols = {row[1] for row in conn.execute("PRAGMA table_info(lore)").fetchall()}
    if "subcategory" not in cols:
        # Very old schema without subcategory — add column first
        try:
            conn.execute("ALTER TABLE lore ADD COLUMN subcategory TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

    # Check if the old topic-only UNIQUE constraint is still in effect
    # by examining the CREATE TABLE SQL
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='lore'"
    ).fetchone()
    if not row:
        return
    create_sql = row[0] or ""
    # Old schema: "topic TEXT NOT NULL UNIQUE" (no composite)
    # New schema: "UNIQUE(subcategory, topic)"
    if "UNIQUE(subcategory,topic)" in create_sql.replace(" ", ""):
        return  # already migrated

    log.info("lore_db: migrating schema from topic UNIQUE to UNIQUE(subcategory, topic)")

    # Recreate table with new schema, preserving embeddings
    conn.executescript("""
        DROP TRIGGER IF EXISTS lore_ai;
        DROP TRIGGER IF EXISTS lore_ad;
        DROP TRIGGER IF EXISTS lore_au;
        DROP TABLE IF EXISTS lore_fts;

        CREATE TABLE lore_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            category     TEXT NOT NULL,
            subcategory  TEXT NOT NULL DEFAULT '',
            topic        TEXT NOT NULL,
            content      TEXT NOT NULL,
            tags         TEXT NOT NULL DEFAULT '',
            embedding    BLOB,
            text_hash    TEXT,
            UNIQUE(subcategory, topic)
        );

        INSERT OR IGNORE INTO lore_new (id, category, subcategory, topic, content, tags, embedding, text_hash)
            SELECT id, category, COALESCE(subcategory, ''), topic, content, tags, embedding, text_hash FROM lore;

        DROP TABLE lore;
        ALTER TABLE lore_new RENAME TO lore;

        CREATE VIRTUAL TABLE lore_fts USING fts5(
            topic, content, category, tags,
            content='lore',
            content_rowid='id',
            tokenize='trigram'
        );

        CREATE TRIGGER lore_ai AFTER INSERT ON lore BEGIN
            INSERT INTO lore_fts(rowid, topic, content, category, tags)
            VALUES (new.id, new.topic, new.content, new.category, new.tags);
        END;

        CREATE TRIGGER lore_ad AFTER DELETE ON lore BEGIN
            INSERT INTO lore_fts(lore_fts, rowid, topic, content, category, tags)
            VALUES ('delete', old.id, old.topic, old.content, old.category, old.tags);
        END;

        CREATE TRIGGER lore_au AFTER UPDATE ON lore BEGIN
            INSERT INTO lore_fts(lore_fts, rowid, topic, content, category, tags)
            VALUES ('delete', old.id, old.topic, old.content, old.category, old.tags);
            INSERT INTO lore_fts(rowid, topic, content, category, tags)
            VALUES (new.id, new.topic, new.content, new.category, new.tags);
        END;
    """)

    # Rebuild FTS content from the new lore table
    conn.execute("INSERT INTO lore_fts(lore_fts) VALUES('rebuild')")
    conn.commit()
    log.info("lore_db: schema migration complete")


# ---------------------------------------------------------------------------
# Embedding cache (in-memory per story_id)
# ---------------------------------------------------------------------------

_embedding_cache: dict[str, dict] = {}  # story_id → {"matrix": ndarray, "ids": list, "categories": list}
_cache_lock = threading.Lock()


def _compute_text_hash(topic: str, content: str) -> str:
    return hashlib.sha256(f"{topic}\n{content}".encode("utf-8")).hexdigest()[:16]


def _load_embedding_cache(story_id: str) -> dict | None:
    """Load all embeddings from SQLite into numpy matrix. Returns cache dict or None."""
    with _cache_lock:
        cached = _embedding_cache.get(story_id)
        if cached is not None:
            return cached

    # Build cache outside lock (DB read + numpy ops are slow)
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT id, category, embedding FROM lore WHERE embedding IS NOT NULL ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        return None

    ids = []
    categories = []
    vectors = []
    for row in rows:
        emb_bytes = row["embedding"]
        if emb_bytes and len(emb_bytes) == EMBEDDING_DIM * 4:
            ids.append(row["id"])
            categories.append(row["category"])
            vectors.append(np.array(np.frombuffer(emb_bytes, dtype=np.float32)))

    if not vectors:
        return None

    matrix = np.stack(vectors)  # (N, 768)
    # Normalize rows for cosine similarity via dot product
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1  # avoid division by zero
    matrix = matrix / norms

    cache = {"matrix": matrix, "ids": ids, "categories": categories}

    # Store under lock — only if not invalidated in the meantime
    with _cache_lock:
        if story_id not in _embedding_cache:
            _embedding_cache[story_id] = cache
        else:
            # Another thread populated it; use theirs
            cache = _embedding_cache[story_id]
    return cache


def _invalidate_cache(story_id: str):
    with _cache_lock:
        _embedding_cache.pop(story_id, None)


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
    """Rebuild the SQLite FTS index from world_lore.json.

    Preserves existing embeddings: updates text columns via UPSERT,
    only clears embedding when content has changed (text_hash mismatch).
    Removes entries no longer in world_lore.json.
    """
    json_path = _lore_json_path(story_id)
    if not os.path.exists(json_path):
        return

    with open(json_path, "r", encoding="utf-8") as f:
        lore_entries = json.load(f)

    conn = _get_conn(story_id)
    _ensure_tables(conn)

    # Track which (subcategory, topic) pairs are in the current JSON
    current_keys = set()

    for entry in lore_entries:
        content = entry.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if content.startswith("（待建立）"):
            continue
        tags = extract_tags(content)
        topic = entry.get("topic", "")
        if not topic:
            continue
        current_keys.add((entry.get("subcategory", ""), topic))
        text_hash = _compute_text_hash(topic, content)
        category = entry.get("category", "其他")
        subcategory = entry.get("subcategory", "")
        tags_str = ",".join(tags)

        existing = conn.execute(
            "SELECT id, text_hash FROM lore WHERE subcategory = ? AND topic = ?", (subcategory, topic)
        ).fetchone()
        if existing:
            if existing["text_hash"] != text_hash:
                # Content changed — update text, clear embedding for re-embed
                conn.execute(
                    "UPDATE lore SET category=?, content=?, tags=?, text_hash=?, embedding=NULL WHERE subcategory=? AND topic=?",
                    (category, content, tags_str, text_hash, subcategory, topic),
                )
            else:
                # Content unchanged — update metadata only, keep embedding
                conn.execute(
                    "UPDATE lore SET category=?, tags=? WHERE subcategory=? AND topic=?",
                    (category, tags_str, subcategory, topic),
                )
        else:
            conn.execute(
                "INSERT INTO lore (category, subcategory, topic, content, tags, text_hash) VALUES (?, ?, ?, ?, ?, ?)",
                (category, subcategory, topic, content, tags_str, text_hash),
            )

    # Remove entries no longer in world_lore.json
    all_keys = [(r[0], r[1]) for r in conn.execute("SELECT subcategory, topic FROM lore").fetchall()]
    for sub, topic in all_keys:
        if (sub, topic) not in current_keys:
            conn.execute("DELETE FROM lore WHERE subcategory = ? AND topic = ?", (sub, topic))

    conn.commit()
    conn.close()

    _invalidate_cache(story_id)

    # Trigger background embedding for entries missing embeddings
    _embed_all_if_needed(story_id)


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

    new_hash = _compute_text_hash(topic, content)
    subcategory = entry.get("subcategory", "")
    existing = conn.execute(
        "SELECT id, text_hash FROM lore WHERE subcategory = ? AND topic = ?", (subcategory, topic)
    ).fetchone()

    hash_changed = True
    if existing:
        if existing["text_hash"] == new_hash:
            hash_changed = False
        conn.execute(
            "UPDATE lore SET category=?, content=?, tags=?, text_hash=? WHERE subcategory=? AND topic=?",
            (entry.get("category", "其他"), content, ",".join(tags), new_hash, subcategory, topic),
        )
    else:
        conn.execute(
            "INSERT INTO lore (category, subcategory, topic, content, tags, text_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (entry.get("category", "其他"), subcategory, topic, content, ",".join(tags), new_hash),
        )
    conn.commit()
    conn.close()

    if hash_changed:
        _invalidate_cache(story_id)
        _embed_single_async(story_id, topic, content)


# ---------------------------------------------------------------------------
# Background embedding
# ---------------------------------------------------------------------------

def _embed_single_async(story_id: str, topic: str, content: str):
    """Embed a single entry in a background daemon thread."""
    def _do():
        try:
            from llm_bridge import embed_text
            text = f"{topic}\n{content}"
            vec = embed_text(text)
            if vec and len(vec) == EMBEDDING_DIM:
                emb_bytes = np.array(vec, dtype=np.float32).tobytes()
                text_hash = _compute_text_hash(topic, content)
                conn = _get_conn(story_id)
                conn.execute(
                    "UPDATE lore SET embedding=?, text_hash=? WHERE topic=?",
                    (emb_bytes, text_hash, topic),
                )
                conn.commit()
                conn.close()
                _invalidate_cache(story_id)
                log.info("lore_db: embedded '%s'", topic)
        except Exception as e:
            log.warning("lore_db: _embed_single_async failed for '%s' — %s", topic, e)

    t = threading.Thread(target=_do, daemon=True)
    t.start()


def _embed_all_if_needed(story_id: str):
    """If any entries lack embeddings, batch-embed all missing ones in background."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    missing = conn.execute(
        "SELECT COUNT(*) as cnt FROM lore WHERE embedding IS NULL"
    ).fetchone()["cnt"]
    conn.close()

    if missing == 0:
        return

    def _do():
        try:
            embed_all_entries(story_id)
        except Exception as e:
            log.warning("lore_db: _embed_all_if_needed failed — %s", e)

    t = threading.Thread(target=_do, daemon=True)
    t.start()


def embed_all_entries(story_id: str):
    """Batch-embed all entries that are missing embeddings. Blocking call."""
    from llm_bridge import embed_texts_batch
    import time

    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT id, topic, content FROM lore WHERE embedding IS NULL ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        return

    log.info("lore_db: embedding %d entries for story %s", len(rows), story_id)
    batch_size = 100

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        texts = [f"{r['topic']}\n{r['content']}" for r in batch]

        vectors = embed_texts_batch(texts)
        if not vectors:
            log.warning("lore_db: batch embed returned None at offset %d", i)
            continue

        conn = _get_conn(story_id)
        try:
            for row, vec in zip(batch, vectors):
                if vec and len(vec) == EMBEDDING_DIM:
                    emb_bytes = np.array(vec, dtype=np.float32).tobytes()
                    text_hash = _compute_text_hash(row["topic"], row["content"])
                    conn.execute(
                        "UPDATE lore SET embedding=?, text_hash=? WHERE id=?",
                        (emb_bytes, text_hash, row["id"]),
                    )
            conn.commit()
        finally:
            conn.close()

        log.info("lore_db: embedded batch %d-%d/%d", i, i + len(batch), len(rows))

    _invalidate_cache(story_id)
    log.info("lore_db: embedding complete for story %s", story_id)


def get_embedding_stats(story_id: str) -> dict:
    """Return embedding coverage stats."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    total = conn.execute("SELECT COUNT(*) as cnt FROM lore").fetchone()["cnt"]
    embedded = conn.execute(
        "SELECT COUNT(*) as cnt FROM lore WHERE embedding IS NOT NULL"
    ).fetchone()["cnt"]
    conn.close()
    return {"total": total, "embedded": embedded}


# ---------------------------------------------------------------------------
# Search — keyword (existing CJK bigram)
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
    rows = conn.execute("SELECT id, category, subcategory, topic, content, tags FROM lore").fetchall()
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
                "id": row["id"],
                "category": row["category"],
                "subcategory": row["subcategory"] or "",
                "topic": row["topic"],
                "content": row["content"],
                "tags": row["tags"],
                "score": score,
            })

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)
    conn.close()
    return scored[:limit]


# ---------------------------------------------------------------------------
# Search — embedding (cosine similarity)
# ---------------------------------------------------------------------------

def _search_embedding(story_id: str, query: str, limit: int = 20) -> list[dict]:
    """Search lore by embedding similarity. Returns entries with cosine scores."""
    cache = _load_embedding_cache(story_id)
    if cache is None:
        return []

    from llm_bridge import embed_text
    query_vec = embed_text(query)
    if not query_vec or len(query_vec) != EMBEDDING_DIM:
        return []

    q = np.array(query_vec, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []
    q = q / q_norm

    # Cosine similarity = dot product (both normalized)
    similarities = cache["matrix"] @ q  # (N,)

    # Get top-k indices
    k = min(limit, len(similarities))
    top_indices = np.argpartition(similarities, -k)[-k:]
    top_indices = top_indices[np.argsort(similarities[top_indices])[::-1]]

    # Fetch full entries for matched IDs
    matched_ids = [cache["ids"][i] for i in top_indices]
    scores = [float(similarities[i]) for i in top_indices]

    conn = _get_conn(story_id)
    results = []
    for row_id, score in zip(matched_ids, scores):
        row = conn.execute(
            "SELECT id, category, subcategory, topic, content, tags FROM lore WHERE id=?",
            (row_id,),
        ).fetchone()
        if row:
            results.append({
                "id": row["id"],
                "category": row["category"],
                "subcategory": row["subcategory"] or "",
                "topic": row["topic"],
                "content": row["content"],
                "tags": row["tags"],
                "emb_score": score,
            })
    conn.close()
    return results


# ---------------------------------------------------------------------------
# Search — hybrid (RRF fusion)
# ---------------------------------------------------------------------------

def search_hybrid(
    story_id: str,
    query: str,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    context: dict | None = None,
) -> list[dict]:
    """Hybrid search: combine keyword + embedding results via RRF.

    context: optional dict with "phase" and "status" for category boosting.
    Returns entries up to token_budget.
    """
    # Keyword search (top 20)
    kw_results = search_lore(story_id, query, limit=20)

    # Embedding search (top 20) — returns [] if Gemini is blocked
    emb_results = _search_embedding(story_id, query, limit=20)

    if not kw_results and not emb_results:
        return []

    # Build rank maps: id → rank (1-based)
    kw_rank = {}
    for rank, r in enumerate(kw_results, 1):
        kw_rank[r["id"]] = rank

    emb_rank = {}
    for rank, r in enumerate(emb_results, 1):
        emb_rank[r["id"]] = rank

    # Merge all candidates
    all_ids = set(kw_rank.keys()) | set(emb_rank.keys())
    candidates = {}  # id → entry dict
    for r in kw_results + emb_results:
        if r["id"] not in candidates:
            candidates[r["id"]] = r

    # RRF scoring
    rrf_scores = {}
    for entry_id in all_ids:
        score = 0.0
        if entry_id in emb_rank:
            score += 1.0 / (RRF_K + emb_rank[entry_id])
        if entry_id in kw_rank:
            score += 1.0 / (RRF_K + kw_rank[entry_id])
        rrf_scores[entry_id] = score

    # Location pinning: boost categories based on game phase
    if context:
        phase = context.get("phase", "")
        status = context.get("status", "")
        boost_categories = set()
        if "副本" in phase:
            boost_categories.add("副本世界觀")
        if "主神空間" in phase or "空間" in phase:
            boost_categories.update(["主神設定與規則", "商城", "場景"])
        if "戰鬥" in status:
            boost_categories.add("體系")

        if boost_categories:
            for entry_id, entry in candidates.items():
                if entry["category"] in boost_categories:
                    # Boost phase-relevant categories to float above generic matches
                    rrf_scores[entry_id] *= 1.5

    # Sort by RRF score
    sorted_ids = sorted(all_ids, key=lambda x: rrf_scores[x], reverse=True)

    # Token-budgeted selection
    results = []
    tokens_used = 0
    for entry_id in sorted_ids:
        entry = candidates[entry_id]
        # Estimate: 1 CJK char ≈ 1 token, cap at 1200 (content is truncated at injection)
        content_len = min(len(entry.get("content", "")), 1200)
        entry_tokens = content_len + len(entry.get("topic", "")) + 20  # header overhead
        if tokens_used + entry_tokens > token_budget and results:
            break
        results.append(entry)
        tokens_used += entry_tokens

    return results


# ---------------------------------------------------------------------------
# Search — public API
# ---------------------------------------------------------------------------

def search_by_tags(story_id: str, tags: list[str], limit: int = 10) -> list[dict]:
    """Find entries that match any of the given tags."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)

    placeholders = " OR ".join(["tags LIKE ?" for _ in tags])
    params = [f"%{tag}%" for tag in tags] + [limit]

    rows = conn.execute(
        f"SELECT category, subcategory, topic, content, tags FROM lore WHERE ({placeholders}) LIMIT ?",
        params,
    ).fetchall()

    results = [
        {"category": r["category"], "subcategory": r["subcategory"] or "", "topic": r["topic"], "content": r["content"], "tags": r["tags"]}
        for r in rows
    ]
    conn.close()
    return results


def get_all_entries(story_id: str) -> list[dict]:
    """Get all indexed lore entries (non-待建立 only)."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute("SELECT category, subcategory, topic, content, tags FROM lore ORDER BY id").fetchall()
    results = [
        {"category": r["category"], "subcategory": r["subcategory"] or "", "topic": r["topic"], "content": r["content"], "tags": r["tags"]}
        for r in rows
    ]
    conn.close()
    return results


def get_entry_count(story_id: str) -> int:
    """Return the total number of indexed lore entries."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    count = conn.execute("SELECT COUNT(*) as cnt FROM lore").fetchone()["cnt"]
    conn.close()
    return count


_MIN_CATEGORY_SIZE = 5  # categories with fewer entries are grouped as 其他


def get_category_summary(story_id: str) -> str:
    """Return a compact category summary for system prompt (~50 tokens).

    Only lists major categories (>= 5 entries). Smaller ones are lumped
    into 「其他(N條)」to keep the summary short.
    """
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM lore GROUP BY category ORDER BY COUNT(*) DESC"
    ).fetchall()
    conn.close()

    if not rows:
        return ""

    major = []
    minor_total = 0
    for r in rows:
        if r["cnt"] >= _MIN_CATEGORY_SIZE:
            major.append(f"{r['category']}({r['cnt']})")
        else:
            minor_total += r["cnt"]

    if minor_total > 0:
        major.append(f"其他({minor_total})")

    return "、".join(major)


def get_toc(story_id: str) -> str:
    """Build a hierarchical table-of-contents string for system prompt injection.

    Topics use full-width colon (：) as hierarchy separator.
    Output is an indented tree so the LLM sees knowledge structure.
    """
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT category, subcategory, topic, tags FROM lore ORDER BY id"
    ).fetchall()
    conn.close()

    if not rows:
        return "（尚無已確立的世界設定）"

    from collections import OrderedDict

    # Group rows by category → subcategory
    cat_sub_rows: dict[str, dict[str, list]] = OrderedDict()
    for r in rows:
        cat = r["category"]
        subcat = r["subcategory"] or ""
        if cat not in cat_sub_rows:
            cat_sub_rows[cat] = OrderedDict()
        if subcat not in cat_sub_rows[cat]:
            cat_sub_rows[cat][subcat] = []
        cat_sub_rows[cat][subcat].append(r)

    lines = []
    for cat, sub_groups in cat_sub_rows.items():
        lines.append(f"### 【{cat}】")

        for subcat, entries in sub_groups.items():
            if subcat:
                lines.append(f"  [{subcat}]")
                base_depth = 1
            else:
                base_depth = 0

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

            _render(tree, base_depth)
        lines.append("")

    return "\n".join(lines).strip()


def delete_entry(story_id: str, topic: str, subcategory: str = ""):
    """Delete a lore entry from the search index by (subcategory, topic)."""
    conn = _get_conn(story_id)
    _ensure_tables(conn)
    conn.execute("DELETE FROM lore WHERE subcategory = ? AND topic = ?", (subcategory, topic))
    conn.commit()
    conn.close()
    _invalidate_cache(story_id)


def search_relevant_lore(
    story_id: str,
    user_message: str,
    context: dict | None = None,
) -> str:
    """Search for lore relevant to a user message. Returns formatted text for injection.

    Uses hybrid search (embedding + keyword with RRF fusion) when embeddings
    are available, falls back to keyword-only search otherwise.
    Results are token-budgeted (~3000 tokens) rather than fixed count.
    """
    results = search_hybrid(
        story_id, user_message,
        token_budget=DEFAULT_TOKEN_BUDGET,
        context=context,
    )

    if not results:
        return ""

    lines = ["[相關世界設定]"]
    for e in results:
        # Strip inline [tag: ...] and [source: ...] markers — already indexed in tags column
        content = _INLINE_META_RE.sub("", e["content"]).strip()
        if len(content) > 1200:
            content = content[:1200] + "…（截斷）"
        cat_label = f"{e['category']}/{e['subcategory']}" if e.get('subcategory') else e['category']
        lines.append(f"#### {cat_label}：{e['topic']}")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Duplicate detection (embedding-based)
# ---------------------------------------------------------------------------

def find_duplicates(story_id: str, threshold: float = 0.90) -> list[dict]:
    """Find near-duplicate lore entries via embedding cosine similarity.

    Returns pairs with similarity > threshold, sorted by similarity descending.
    """
    cache = _load_embedding_cache(story_id)
    if cache is None or len(cache["ids"]) < 2:
        return []

    matrix = cache["matrix"]  # already normalized
    ids = cache["ids"]

    # Pairwise cosine similarity (dot product of normalized vectors)
    sim_matrix = matrix @ matrix.T  # (N, N)

    # Vectorized: find pairs above threshold in upper triangle
    i_indices, j_indices = np.where(np.triu(sim_matrix >= threshold, k=1))

    if len(i_indices) == 0:
        return []

    sims = sim_matrix[i_indices, j_indices]
    # Sort by similarity descending
    order = np.argsort(sims)[::-1]

    # Enrich with entry details
    conn = _get_conn(story_id)
    enriched = []
    for idx in order:
        i, j = int(i_indices[idx]), int(j_indices[idx])
        a = conn.execute(
            "SELECT category, subcategory, topic, content FROM lore WHERE id=?", (ids[i],)
        ).fetchone()
        b = conn.execute(
            "SELECT category, subcategory, topic, content FROM lore WHERE id=?", (ids[j],)
        ).fetchone()
        if a and b:
            enriched.append({
                "entry_a": {"category": a["category"], "subcategory": a["subcategory"] or "", "topic": a["topic"], "content": a["content"][:200]},
                "entry_b": {"category": b["category"], "subcategory": b["subcategory"] or "", "topic": b["topic"], "content": b["content"][:200]},
                "similarity": round(float(sims[idx]), 4),
            })
    conn.close()
    return enriched
