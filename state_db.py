"""SQLite state index for per-branch character state + NPC retrieval."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")

_CATEGORY_LABELS = {
    "inventory": "道具",
    "ability": "技能",
    "relationship": "關係",
    "npc": "NPC 檔案",
    "mission": "已完成任務",
    "system": "體系",
}


def _db_path(story_id: str, branch_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "branches", branch_id, "state.db")


def _state_path(story_id: str, branch_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "branches", branch_id, "character_state.json")


def _npcs_path(story_id: str, branch_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "branches", branch_id, "npcs.json")


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _get_conn(story_id: str, branch_id: str) -> sqlite3.Connection:
    path = _db_path(story_id, branch_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS state_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,
            entry_key   TEXT NOT NULL,
            content     TEXT NOT NULL,
            tags        TEXT NOT NULL DEFAULT '',
            updated_at  TEXT NOT NULL,
            UNIQUE(category, entry_key)
        );
        CREATE INDEX IF NOT EXISTS idx_state_entries_category
            ON state_entries(category);
        """
    )


def state_db_exists(story_id: str, branch_id: str) -> bool:
    return os.path.exists(_db_path(story_id, branch_id))


def upsert_entry(
    story_id: str,
    branch_id: str,
    category: str,
    entry_key: str,
    content: str,
    tags: str = "",
):
    key = (entry_key or "").strip()
    cat = (category or "").strip()
    if not key or not cat:
        return
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = str(content)
    if not isinstance(tags, str):
        tags = str(tags)

    conn = _get_conn(story_id, branch_id)
    _ensure_tables(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO state_entries (category, entry_key, content, tags, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(category, entry_key)
        DO UPDATE SET content=excluded.content, tags=excluded.tags, updated_at=excluded.updated_at
        """,
        (cat, key, content, tags, now),
    )
    conn.commit()
    conn.close()


def delete_entry(story_id: str, branch_id: str, category: str, entry_key: str):
    key = (entry_key or "").strip()
    cat = (category or "").strip()
    if not key or not cat:
        return
    conn = _get_conn(story_id, branch_id)
    _ensure_tables(conn)
    conn.execute(
        "DELETE FROM state_entries WHERE category = ? AND entry_key = ?",
        (cat, key),
    )
    conn.commit()
    conn.close()


def bulk_upsert(story_id: str, branch_id: str, rows: list[tuple[str, str, str, str]]):
    if not rows:
        return
    conn = _get_conn(story_id, branch_id)
    _ensure_tables(conn)
    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for category, entry_key, content, tags in rows:
        key = (entry_key or "").strip()
        cat = (category or "").strip()
        if not key or not cat:
            continue
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        if not isinstance(tags, str):
            tags = str(tags)
        payload.append((cat, key, content, tags, now))

    if payload:
        conn.executemany(
            """
            INSERT INTO state_entries (category, entry_key, content, tags, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category, entry_key)
            DO UPDATE SET content=excluded.content, tags=excluded.tags, updated_at=excluded.updated_at
            """,
            payload,
        )
    conn.commit()
    conn.close()


def replace_category(
    story_id: str,
    branch_id: str,
    category: str,
    rows: list[tuple[str, str, str]],
):
    cat = (category or "").strip()
    if not cat:
        return
    conn = _get_conn(story_id, branch_id)
    _ensure_tables(conn)
    conn.execute("DELETE FROM state_entries WHERE category = ?", (cat,))
    now = datetime.now(timezone.utc).isoformat()
    payload = []
    for entry_key, content, tags in rows:
        key = (entry_key or "").strip()
        if not key:
            continue
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        if not isinstance(tags, str):
            tags = str(tags)
        payload.append((cat, key, content, tags, now))
    if payload:
        conn.executemany(
            "INSERT INTO state_entries (category, entry_key, content, tags, updated_at) VALUES (?, ?, ?, ?, ?)",
            payload,
        )
    conn.commit()
    conn.close()


def _to_inventory_map(inv) -> dict[str, str]:
    if isinstance(inv, dict):
        out = {}
        for k, v in inv.items():
            key = str(k).strip()
            if key:
                out[key] = "" if v is None else str(v)
        return out

    out = {}
    if isinstance(inv, list):
        for item in inv:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if not text:
                continue
            if " — " in text:
                key, val = text.split(" — ", 1)
                out[key.strip()] = val.strip()
            else:
                out[text] = ""
    return out


def _npc_content(npc: dict) -> str:
    parts = []
    role = (npc.get("role") or "").strip()
    if role:
        parts.append(f"定位:{role}")
    tier = (npc.get("tier") or "").strip()
    if tier:
        parts.append(f"戰力:{tier}級")
    rel = (npc.get("relationship_to_player") or "").strip()
    if rel:
        parts.append(f"關係:{rel}")
    status = (npc.get("current_status") or "").strip()
    if status:
        parts.append(f"狀態:{status}")
    traits = npc.get("notable_traits") or []
    if isinstance(traits, list) and traits:
        parts.append("特質:" + "、".join(str(x) for x in traits if x))
    return "；".join(parts)


def rebuild_from_json(
    story_id: str,
    branch_id: str,
    state: dict | None = None,
    npcs: list[dict] | None = None,
) -> int:
    if state is None:
        state = _load_json(_state_path(story_id, branch_id), {})
    if npcs is None:
        npcs = _load_json(_npcs_path(story_id, branch_id), [])

    inv_map = _to_inventory_map(state.get("inventory", {}))
    inv_rows = [(k, v, "道具") for k, v in inv_map.items()]

    abilities = state.get("abilities", [])
    ability_rows = []
    if isinstance(abilities, list):
        for item in abilities:
            if isinstance(item, str) and item.strip():
                ability_rows.append((item.strip(), "", "技能"))

    rels = state.get("relationships", {})
    rel_rows = []
    if isinstance(rels, dict):
        for name, rel in rels.items():
            key = str(name).strip()
            if key:
                rel_rows.append((key, "" if rel is None else str(rel), "關係"))

    missions = state.get("completed_missions", [])
    mission_rows = []
    if isinstance(missions, list):
        for item in missions:
            if isinstance(item, str) and item.strip():
                mission_rows.append((item.strip(), "", "任務"))

    systems = state.get("systems", {})
    system_rows = []
    if isinstance(systems, dict):
        for name, lv in systems.items():
            key = str(name).strip()
            if key:
                system_rows.append((key, "" if lv is None else str(lv), "體系"))

    npc_rows = []
    if isinstance(npcs, list):
        for npc in npcs:
            if not isinstance(npc, dict):
                continue
            name = (npc.get("name") or "").strip()
            if not name:
                continue
            npc_rows.append((name, _npc_content(npc), "NPC"))

    replace_category(story_id, branch_id, "inventory", inv_rows)
    replace_category(story_id, branch_id, "ability", ability_rows)
    replace_category(story_id, branch_id, "relationship", rel_rows)
    replace_category(story_id, branch_id, "mission", mission_rows)
    replace_category(story_id, branch_id, "system", system_rows)
    replace_category(story_id, branch_id, "npc", npc_rows)

    return (
        len(inv_rows)
        + len(ability_rows)
        + len(rel_rows)
        + len(mission_rows)
        + len(system_rows)
        + len(npc_rows)
    )


def _extract_keywords(query: str) -> set[str]:
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", query)
    keywords = set()
    for run in cjk_runs:
        for i in range(len(run) - 1):
            keywords.add(run[i:i + 2])
        for i in range(len(run) - 2):
            keywords.add(run[i:i + 3])
    for token in re.findall(r"[A-Za-z0-9_+-]+", query.lower()):
        if len(token) >= 2:
            keywords.add(token)
    if not keywords and query.strip():
        keywords.add(query.strip().lower())
    return keywords


def _score_row(row: sqlite3.Row, keywords: set[str]) -> float:
    key = row["entry_key"] or ""
    tags = row["tags"] or ""
    content = row["content"] or ""
    text = f"{key} {tags} {content}"
    score = 0.0
    for kw in keywords:
        if kw in text:
            if kw in key:
                score += 10
            if kw in tags:
                score += 5
            if kw in content:
                score += 1
    return score


def _apply_context_boost(score: float, category: str, context: dict | None) -> float:
    if not context:
        return score
    phase = str(context.get("phase", ""))
    status = str(context.get("status", ""))
    if "戰鬥" in status and category in {"inventory", "ability", "npc"}:
        score *= 1.4
    if ("主神空間" in phase or "空間" in phase) and category in {"inventory", "mission"}:
        score *= 1.3
    if "副本" in phase and category in {"npc", "ability"}:
        score *= 1.3
    return score


def _line_for_row(category: str, key: str, content: str) -> str:
    if category in {"inventory", "ability", "mission"}:
        return f"- {key}（{content}）" if content else f"- {key}"
    if category in {"relationship", "system", "npc"}:
        return f"- {key}：{content}" if content else f"- {key}"
    return f"- {key}"


def search_state(
    story_id: str,
    branch_id: str,
    query: str,
    token_budget: int | None = None,
    must_include_keys: list[str] | None = None,
    context: dict | None = None,
) -> str:
    if not state_db_exists(story_id, branch_id):
        rebuild_from_json(story_id, branch_id)

    conn = _get_conn(story_id, branch_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT category, entry_key, content, tags FROM state_entries"
    ).fetchall()
    conn.close()
    if not rows:
        return ""

    keywords = _extract_keywords(query)
    forced_keys = {(k or "").strip() for k in (must_include_keys or []) if (k or "").strip()}

    scored = []
    forced = []
    for row in rows:
        key = row["entry_key"] or ""
        category = row["category"] or ""
        if key in forced_keys:
            forced.append(row)
            continue
        score = _score_row(row, keywords)
        score = _apply_context_boost(score, category, context)
        if score > 0:
            scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)

    selected = []
    seen = set()
    for row in forced:
        ident = (row["category"], row["entry_key"])
        if ident not in seen:
            selected.append(row)
            seen.add(ident)
    for _, row in scored:
        ident = (row["category"], row["entry_key"])
        if ident not in seen:
            selected.append(row)
            seen.add(ident)

    if not selected:
        return ""

    grouped: dict[str, list[str]] = {}
    used = 0
    for row in selected:
        category = row["category"]
        key = row["entry_key"]
        content = row["content"] or ""
        line = _line_for_row(category, key, content)
        est = len(line)
        is_forced = key in forced_keys
        if token_budget is not None and token_budget > 0 and used + est > token_budget and grouped and not is_forced:
            continue
        grouped.setdefault(category, []).append(line)
        used += est

    if not grouped:
        return ""

    order = ["inventory", "ability", "npc", "relationship", "mission", "system"]
    lines = ["[相關角色狀態]"]
    for category in order:
        items = grouped.get(category)
        if not items:
            continue
        lines.append(f"#### {_CATEGORY_LABELS.get(category, category)}")
        lines.extend(items)
    return "\n".join(lines)


def get_summary(story_id: str, branch_id: str) -> str:
    if not state_db_exists(story_id, branch_id):
        rebuild_from_json(story_id, branch_id)
    conn = _get_conn(story_id, branch_id)
    _ensure_tables(conn)
    rows = conn.execute(
        "SELECT category, COUNT(*) AS cnt FROM state_entries GROUP BY category"
    ).fetchall()
    conn.close()
    if not rows:
        return "（尚無狀態索引）"
    counts = {row["category"]: int(row["cnt"]) for row in rows}
    parts = []
    for category in ["inventory", "ability", "relationship", "npc", "mission", "system"]:
        cnt = counts.get(category, 0)
        if cnt > 0:
            parts.append(f"{_CATEGORY_LABELS.get(category, category)}{cnt}")
    return "、".join(parts) if parts else "（尚無狀態索引）"
