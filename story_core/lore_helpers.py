from __future__ import annotations

from collections import Counter
import logging
import os
import re
import threading

from story_core.lore_db import get_category_summary, get_entry_count, upsert_entry as upsert_lore_entry
from story_core.lore_organizer import get_lore_lock, try_classify_topic
from story_core.story_io import _branch_dir, _load_json, _save_json, _story_design_dir


log = logging.getLogger("rpg")

BRANCH_LORE_SCORE_FLOOR = 0.02
LORE_QUERY_MAX_RECENT_GM = 5
LORE_QUERY_MAX_TERMS = 12
_LORE_QUERY_STOP_TERMS = {
    "知道", "我們", "你們", "他們", "這裡", "那裡", "這個", "那個",
    "自己", "現在", "什麼", "怎麼", "可以", "需要", "應該", "感覺",
}
_LORE_QUERY_STOP_CHARS = set(
    "，,。！？!?；;：:、／/—-「」『』【】《》()[]{}<>\"' \t\r\n"
)
_LORE_QUERY_INTERNAL_PUNCT = set("，,。！？!?；;：:、／/—-「」『』【】《》")
_LORE_QUERY_QUOTED_RE = re.compile(r"[《「『【]([^\n]{2,20}?)[》」』】]")
_LORE_QUERY_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def _story_lore_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "world_lore.json")


def _load_lore(story_id: str) -> list[dict]:
    return _load_json(_story_lore_path(story_id), [])


_branch_lore_locks: dict[str, threading.Lock] = {}
_branch_lore_locks_meta = threading.Lock()


def _get_branch_lore_lock(story_id: str, branch_id: str) -> threading.Lock:
    """Get or create a per-branch lock for branch_lore.json writes."""
    key = f"{story_id}:{branch_id}"
    with _branch_lore_locks_meta:
        if key not in _branch_lore_locks:
            _branch_lore_locks[key] = threading.Lock()
        return _branch_lore_locks[key]


def _branch_lore_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "branch_lore.json")


def _load_branch_lore(story_id: str, branch_id: str) -> list[dict]:
    return _load_json(_branch_lore_path(story_id, branch_id), [])


def _save_branch_lore(story_id: str, branch_id: str, lore: list[dict]):
    _save_json(_branch_lore_path(story_id, branch_id), lore)


def _save_branch_lore_entry(
    story_id: str,
    branch_id: str,
    entry: dict,
    prefix_registry: dict | None = None,
):
    """Save a lore entry to branch_lore.json, upserting by topic."""
    topic = entry.get("topic", "").strip()
    if not topic:
        return

    category = entry.get("category", "")
    if "：" not in topic and category:
        organized = try_classify_topic(topic, category, story_id, prefix_registry=prefix_registry)
        if organized:
            log.info("    branch_lore auto-classify: '%s' → '%s'", topic, organized)
            topic = organized
            entry["topic"] = topic

    subcategory = entry.get("subcategory", "")
    lock = _get_branch_lore_lock(story_id, branch_id)
    with lock:
        lore = _load_branch_lore(story_id, branch_id)
        for index, existing in enumerate(lore):
            if existing.get("topic") == topic and existing.get("subcategory", "") == subcategory:
                if "category" not in entry and "category" in existing:
                    entry["category"] = existing["category"]
                if "source" not in entry and "source" in existing:
                    entry["source"] = existing["source"]
                if "edited_by" not in entry and "edited_by" in existing:
                    entry["edited_by"] = existing["edited_by"]
                if "subcategory" not in entry and "subcategory" in existing:
                    entry["subcategory"] = existing["subcategory"]
                lore[index] = entry
                _save_branch_lore(story_id, branch_id, lore)
                return
        lore.append(entry)
        _save_branch_lore(story_id, branch_id, lore)


def _merge_branch_lore_into(story_id: str, src_branch_id: str, dst_branch_id: str):
    """Merge source branch_lore into destination, upserting by topic."""
    source = _load_branch_lore(story_id, src_branch_id)
    if not source:
        return
    destination = _load_branch_lore(story_id, dst_branch_id)
    destination_keys = {
        (entry.get("subcategory", ""), entry.get("topic", "")): index
        for index, entry in enumerate(destination)
    }
    for entry in source:
        key = (entry.get("subcategory", ""), entry.get("topic", ""))
        if key in destination_keys:
            destination[destination_keys[key]] = entry
        else:
            destination.append(entry)
            destination_keys[key] = len(destination) - 1
    _save_branch_lore(story_id, dst_branch_id, destination)


def _copy_branch_lore_for_fork(
    story_id: str,
    source_branch_id: str,
    target_branch_id: str,
    branch_point_index: int | None,
):
    """Copy source branch lore to a fork, filtered by fork time when possible."""
    source = _load_branch_lore(story_id, source_branch_id)
    if not source:
        return

    if branch_point_index is None:
        _save_branch_lore(story_id, target_branch_id, source)
        return

    filtered: list[dict] = []
    skipped_future = 0
    for entry in source:
        provenance = entry.get("source")
        if not isinstance(provenance, dict):
            filtered.append(entry)
            continue

        message_index = provenance.get("msg_index")
        try:
            message_index_int = int(message_index)
        except (TypeError, ValueError):
            filtered.append(entry)
            continue

        if message_index_int <= branch_point_index:
            filtered.append(entry)
        else:
            skipped_future += 1

    if filtered:
        _save_branch_lore(story_id, target_branch_id, filtered)
    if skipped_future:
        log.info(
            "    branch_lore fork filtered %d future entries (src=%s dst=%s bp=%s)",
            skipped_future,
            source_branch_id,
            target_branch_id,
            branch_point_index,
        )


def _search_branch_lore(
    story_id: str,
    branch_id: str,
    query: str,
    token_budget: int = 1500,
    context: dict | None = None,
) -> str:
    """Search branch_lore.json using CJK bigram scoring."""
    lore = _load_branch_lore(story_id, branch_id)
    if not lore:
        return ""

    cjk_re = re.compile(r"[\u4e00-\u9fff]+")

    def _bigrams(text: str) -> set[str]:
        bigrams = set()
        for run in cjk_re.findall(text):
            for index in range(len(run) - 1):
                bigrams.add(run[index:index + 2])
        return bigrams

    query_bigrams = _bigrams(query)
    query_lower = query.lower()
    expanded_query = query.splitlines()[-1] if "\n" in query else query
    query_terms = []
    seen_terms = set()
    for raw_term in re.split(r"\s+", expanded_query):
        if not _LORE_QUERY_CJK_RE.search(raw_term):
            continue
        term = _normalize_lore_query_term(raw_term)
        if not term or term in seen_terms or _is_noisy_lore_query_term(term):
            continue
        seen_terms.add(term)
        query_terms.append(term)

    current_dungeon = ""
    in_dungeon = False
    if context:
        current_dungeon = context.get("dungeon", "")
        phase = context.get("phase", "")
        in_dungeon = bool(current_dungeon and "副本" in phase)

    scored = []
    for entry in lore:
        topic = entry.get("topic", "")
        content = entry.get("content", "")
        category = entry.get("category", "")
        subcategory = entry.get("subcategory", "")
        text = f"{category} {subcategory} {topic} {content}"

        score = 0.0
        text_bigrams = _bigrams(text)
        if query_bigrams and text_bigrams:
            overlap = query_bigrams & text_bigrams
            score = len(overlap) / max(len(query_bigrams), 1)

        if query_lower and query_lower in topic.lower():
            score += 2.0
        score += sum(1.5 for term in query_terms if term in topic)
        score += sum(2.0 for term in query_terms if term in subcategory)
        score += sum(0.6 for term in query_terms if term in content)

        if in_dungeon and category == "副本世界觀" and subcategory != current_dungeon:
            score *= 0.1

        if score >= BRANCH_LORE_SCORE_FLOOR:
            scored.append((score, entry))

    scored.sort(key=lambda item: -item[0])

    lines = []
    used_tokens = 0
    for _, entry in scored:
        content = entry.get("content", "")
        estimated_tokens = len(content)
        if used_tokens + estimated_tokens > token_budget and lines:
            break
        if len(content) > 1200:
            content = content[:1200] + "…（截斷）"
        category_label = entry.get("category", "")
        subcategory = entry.get("subcategory", "")
        if subcategory:
            category_label = f"{category_label}/{subcategory}"
        lines.append(f"#### {category_label}：{entry.get('topic', '')}")
        lines.append(content)
        lines.append("")
        used_tokens += estimated_tokens

    if not lines:
        return ""
    return "[相關分支設定]\n" + "\n".join(lines)


def _normalize_lore_query_term(term: str) -> str:
    return re.sub(r"\s+", "", term).strip("：:，,。！？!?、（）()[]{}").strip()


def _is_noisy_lore_query_term(term: str) -> bool:
    if len(term) < 2 or len(term) > 12:
        return True
    if term in _LORE_QUERY_STOP_TERMS:
        return True
    if any(char in _LORE_QUERY_INTERNAL_PUNCT for char in term):
        return True
    if term[0] in _LORE_QUERY_STOP_CHARS or term[-1] in _LORE_QUERY_STOP_CHARS:
        return True
    if all(char in _LORE_QUERY_STOP_CHARS for char in term):
        return True
    if len(term) == 2 and (term[0] in _LORE_QUERY_STOP_CHARS or term[1] in _LORE_QUERY_STOP_CHARS):
        return True
    return False


def _split_quoted_lore_terms(term: str) -> list[str]:
    parts = [term]
    for separator in ("：", ":", "·", "／", "/", "—", "-"):
        next_parts = []
        for part in parts:
            if separator not in part:
                next_parts.append(part)
                continue
            next_parts.extend(piece for piece in part.split(separator) if piece)
        parts = next_parts

    normalized = []
    seen = set()
    for part in parts:
        clean = _normalize_lore_query_term(part)
        if clean and clean not in seen:
            normalized.append(clean)
            seen.add(clean)
    return normalized


def _extract_recent_lore_terms(
    recent_messages: list[dict] | None,
    limit: int = LORE_QUERY_MAX_TERMS,
) -> list[str]:
    if not recent_messages:
        return []

    gm_messages = [
        message.get("content", "")
        for message in recent_messages
        if message.get("role") in ("gm", "assistant")
        and isinstance(message.get("content"), str)
        and message.get("content")
    ][-LORE_QUERY_MAX_RECENT_GM:]
    if not gm_messages:
        return []

    scores = Counter()
    for index, text in enumerate(gm_messages):
        recency_weight = index + 1
        latest_bonus = 2 if index == len(gm_messages) - 1 else 0

        for raw_term in _LORE_QUERY_QUOTED_RE.findall(text):
            for term in _split_quoted_lore_terms(raw_term):
                if not _is_noisy_lore_query_term(term):
                    scores[term] += recency_weight * 10 + len(term) + latest_bonus * 4

        for run in _LORE_QUERY_CJK_RE.findall(text):
            for size in (3, 4):
                for start in range(len(run) - size + 1):
                    term = run[start:start + size]
                    if _is_noisy_lore_query_term(term):
                        continue
                    scores[term] += recency_weight * (size - 1) + latest_bonus * (size - 1)

    selected = []
    for term, _ in sorted(scores.items(), key=lambda item: (-item[1], -len(item[0]), item[0])):
        if any(term in kept for kept in selected if len(kept) > len(term)):
            continue
        selected.append(term)
        if len(selected) >= limit:
            break
    return selected


def _select_lore_npc_terms(
    user_text: str,
    recent_messages: list[dict] | None = None,
    npcs: list[dict] | None = None,
) -> list[str]:
    if not npcs:
        return []

    recent_text = "\n".join(
        message.get("content", "")
        for message in (recent_messages or [])
        if message.get("role") in ("gm", "assistant") and isinstance(message.get("content"), str)
    )
    context_text = f"{user_text}\n{recent_text}"
    selected = []
    seen = set()
    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        name = _normalize_lore_query_term(str(npc.get("name", "")))
        if not name or name in seen or _is_noisy_lore_query_term(name) or name not in context_text:
            continue
        seen.add(name)
        selected.append(name)
    return selected


def _extract_user_lore_terms(user_text: str, recent_messages: list[dict] | None = None) -> list[str]:
    if not user_text:
        return []

    recent_text = "\n".join(
        message.get("content", "")
        for message in (recent_messages or [])
        if message.get("role") in ("gm", "assistant") and isinstance(message.get("content"), str)
    )
    selected = []
    seen = set()
    for raw_term in re.split(r"\s+", user_text):
        if not _LORE_QUERY_CJK_RE.search(raw_term):
            continue
        term = _normalize_lore_query_term(raw_term)
        if (
            not term
            or term in seen
            or _is_noisy_lore_query_term(term)
            or (recent_text and term not in recent_text)
        ):
            continue
        seen.add(term)
        selected.append(term)
    return selected


def _build_lore_search_query(
    user_text: str,
    recent_messages: list[dict] | None = None,
    npcs: list[dict] | None = None,
    current_dungeon: str = "",
) -> str:
    from app import (
        _extract_recent_lore_terms as app_extract_recent_lore_terms,
        _extract_user_lore_terms as app_extract_user_lore_terms,
        _select_lore_npc_terms as app_select_lore_npc_terms,
    )

    extras = []
    seen = set()

    def _add(term: str):
        clean = _normalize_lore_query_term(term)
        if not clean or clean in seen or _is_noisy_lore_query_term(clean):
            return
        seen.add(clean)
        extras.append(clean)

    if current_dungeon:
        _add(current_dungeon)

    for npc_name in app_select_lore_npc_terms(user_text, recent_messages=recent_messages, npcs=npcs):
        _add(npc_name)

    for term in app_extract_user_lore_terms(user_text, recent_messages=recent_messages):
        _add(term)

    for term in app_extract_recent_lore_terms(recent_messages):
        _add(term)

    if not extras:
        return user_text
    return user_text + "\n" + " ".join(extras)


def _get_branch_lore_toc(story_id: str, branch_id: str) -> str:
    """Build a simple TOC of branch lore topics for dedup in extraction prompt."""
    lore = _load_branch_lore(story_id, branch_id)
    if not lore:
        return ""
    lines = []
    for entry in lore:
        topic = entry.get("topic", "")
        category = entry.get("category", "")
        subcategory = entry.get("subcategory", "")
        if topic:
            prefix = f"{category}/{subcategory}" if subcategory else category
            lines.append(f"- {prefix}：{topic}")
    return "\n".join(lines)


def _find_similar_topic(
    new_topic: str,
    new_category: str,
    topic_categories: dict[str, str],
    threshold: float = 0.5,
) -> str | None:
    """Find an existing topic with high CJK bigram overlap, scoped to same category."""
    cjk_re = re.compile(r"[\u4e00-\u9fff]+")

    def _bigrams(text: str) -> set[str]:
        bigrams = set()
        for run in cjk_re.findall(text):
            for index in range(len(run) - 1):
                bigrams.add(run[index:index + 2])
        return bigrams

    new_bigrams = _bigrams(new_topic)
    if not new_bigrams:
        return None

    best_topic = None
    best_similarity = 0.0
    for existing, category in topic_categories.items():
        if category != new_category:
            continue
        existing_bigrams = _bigrams(existing)
        if not existing_bigrams:
            continue
        overlap = new_bigrams & existing_bigrams
        if len(overlap) < 2:
            continue
        similarity = len(overlap) / len(new_bigrams | existing_bigrams)
        if similarity > best_similarity:
            best_similarity = similarity
            best_topic = existing

    return best_topic if best_similarity >= threshold else None


def _save_lore_entry(story_id: str, entry: dict, prefix_registry: dict | None = None):
    """Save a lore entry, upserting JSON and lore.db."""
    topic = entry.get("topic", "").strip()
    if not topic:
        return

    category = entry.get("category", "")
    if "：" not in topic and category:
        organized = try_classify_topic(topic, category, story_id, prefix_registry=prefix_registry)
        if organized:
            log.info("    lore auto-classify: '%s' → '%s'", topic, organized)
            topic = organized
            entry["topic"] = topic

    subcategory = entry.get("subcategory", "")
    lock = get_lore_lock(story_id)
    with lock:
        lore = _load_lore(story_id)
        for index, existing in enumerate(lore):
            if existing.get("topic") == topic and existing.get("subcategory", "") == subcategory:
                if "category" not in entry and "category" in existing:
                    entry["category"] = existing["category"]
                if "source" not in entry and "source" in existing:
                    entry["source"] = existing["source"]
                if "edited_by" not in entry and "edited_by" in existing:
                    entry["edited_by"] = existing["edited_by"]
                if "subcategory" not in entry and "subcategory" in existing:
                    entry["subcategory"] = existing["subcategory"]
                lore[index] = entry
                _save_json(_story_lore_path(story_id), lore)
                upsert_lore_entry(story_id, entry)
                return
        lore.append(entry)
        _save_json(_story_lore_path(story_id), lore)
        upsert_lore_entry(story_id, entry)


def _build_lore_text(story_id: str, branch_id: str = "main") -> str:
    """Build compact lore summary for system prompt."""
    count = get_entry_count(story_id)
    if count == 0:
        lore = _load_lore(story_id)
        if not lore:
            return "（尚無已確立的世界設定）"
        count = len(lore)
    category_summary = get_category_summary(story_id)
    note = f"世界設定共 {count} 條，會根據每回合對話內容自動檢索並注入相關條目。"
    if category_summary:
        note += f"\n知識分類：{category_summary}"
    branch_lore = _load_branch_lore(story_id, branch_id)
    if branch_lore:
        note += f"\n另有 {len(branch_lore)} 條分支專屬設定（本次冒險中累積的觀察與發現）。"
    return note


__all__ = [
    "BRANCH_LORE_SCORE_FLOOR",
    "LORE_QUERY_MAX_RECENT_GM",
    "LORE_QUERY_MAX_TERMS",
    "_LORE_QUERY_STOP_TERMS",
    "_LORE_QUERY_STOP_CHARS",
    "_LORE_QUERY_INTERNAL_PUNCT",
    "_LORE_QUERY_QUOTED_RE",
    "_LORE_QUERY_CJK_RE",
    "_branch_lore_locks",
    "_branch_lore_locks_meta",
    "_story_lore_path",
    "_load_lore",
    "_get_branch_lore_lock",
    "_branch_lore_path",
    "_load_branch_lore",
    "_save_branch_lore",
    "_save_branch_lore_entry",
    "_merge_branch_lore_into",
    "_copy_branch_lore_for_fork",
    "_search_branch_lore",
    "_normalize_lore_query_term",
    "_is_noisy_lore_query_term",
    "_split_quoted_lore_terms",
    "_extract_recent_lore_terms",
    "_select_lore_npc_terms",
    "_extract_user_lore_terms",
    "_build_lore_search_query",
    "_get_branch_lore_toc",
    "_find_similar_topic",
    "_save_lore_entry",
    "_build_lore_text",
]
