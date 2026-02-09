"""Conversation compaction — rolling narrative recap for bounded context.

Replaces unbounded --resume history with:
  system prompt (with {narrative_recap}) + recent N messages + structured data injections.

Storage: branches/<bid>/conversation_recap.json
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

from story_utils import get_character_name

log = logging.getLogger("rpg")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STORIES_DIR = os.path.join(DATA_DIR, "stories")

# Compaction thresholds
RECAP_CHAR_CAP = 8000          # Max chars for recap text
RECAP_META_COMPACT_TARGET = 3000  # Target chars when meta-compacting
MIN_UNCOMPACTED_FOR_TRIGGER = 20  # Need >20 uncompacted msgs to trigger
RECENT_WINDOW = 20               # Keep last 20 messages as raw context

_FALLBACK_RECAP = "（尚無回顧，完整對話記錄已提供。）"

_COMPACT_PROMPT = """\
你是故事摘要助手。以下是文字 RPG 遊戲的對話片段。請用繁體中文寫一份 500-800 字的敘事回顧：

1. 關鍵劇情發展（按時間順序）
2. 玩家的重要決策及後果
3. 情感轉折與角色發展
4. 尚未解決的懸念

重要：玩家角色名為「{character_name}」。摘要中請使用第三人稱「{character_name}」稱呼玩家角色，不要用其他名字替代，也不要使用第一人稱「我」。
注意：角色屬性、道具、NPC 資料、世界設定已由其他系統追蹤，不需列出。
專注於「發生了什麼事」和「故事走向如何」。

{existing_recap}

---
以下是新的對話內容：
{messages}
"""

_META_COMPACT_PROMPT = """\
你是故事摘要助手。以下是一份 RPG 遊戲的累積敘事回顧，已經太長了。
請用繁體中文將它重新精煉為約 800 字的版本，保留：

1. 最關鍵的劇情轉折（按時間順序）
2. 核心角色發展弧線
3. 仍在進行中的懸念和伏筆

重要：玩家角色名為「{character_name}」。請一律使用「{character_name}」稱呼玩家角色，不要用其他名字替代。
可以省略已解決的小事件和重複的細節。

---
{recap}
"""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _recap_path(story_id: str, branch_id: str) -> str:
    return os.path.join(
        STORIES_DIR, story_id, "branches", branch_id, "conversation_recap.json"
    )


def _default_recap() -> dict:
    return {
        "compacted_through_index": -1,
        "last_compacted_at": None,
        "recap_text": "",
        "total_turns_compacted": 0,
    }


def load_recap(story_id: str, branch_id: str) -> dict:
    path = _recap_path(story_id, branch_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return _default_recap()


def save_recap(story_id: str, branch_id: str, data: dict):
    path = _recap_path(story_id, branch_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_recap_text(story_id: str, branch_id: str) -> str:
    """Return recap text for system prompt injection, or fallback."""
    recap = load_recap(story_id, branch_id)
    text = recap.get("recap_text", "").strip()
    return text if text else _FALLBACK_RECAP


def should_compact(recap: dict, timeline_len: int) -> bool:
    """True when there are >MIN_UNCOMPACTED msgs between compacted_through and recent window."""
    compacted_through = recap.get("compacted_through_index", -1)
    recent_start = timeline_len - RECENT_WINDOW
    uncompacted = recent_start - (compacted_through + 1)
    return uncompacted > MIN_UNCOMPACTED_FOR_TRIGGER


def get_context_window(full_timeline: list[dict]) -> list[dict]:
    """Return the recent message window for LLM context."""
    return full_timeline[-RECENT_WINDOW:]


def copy_recap_to_branch(
    story_id: str, from_bid: str, to_bid: str, branch_point_index: int
):
    """Copy parent recap to new branch, noting divergence point."""
    parent_recap = load_recap(story_id, from_bid)
    if not parent_recap.get("recap_text"):
        return

    new_recap = dict(parent_recap)
    # If branching before compacted_through, keep parent recap as-is
    # (the recap covers the shared history)
    # If branching after, also fine — recap is still valid for the shared part
    if branch_point_index >= 0 and parent_recap.get("compacted_through_index", -1) > branch_point_index:
        # Branch diverges within compacted region — add note
        new_recap["recap_text"] += "\n\n（注意：以下為分支劇情，從此處開始與主線不同。）"

    save_recap(story_id, to_bid, new_recap)


# ---------------------------------------------------------------------------
# Compaction (background)
# ---------------------------------------------------------------------------

# Lock per (story_id, branch_id) to prevent concurrent compaction
_compact_locks: dict[tuple[str, str], threading.Lock] = {}
_compact_locks_meta = threading.Lock()


def _get_lock(story_id: str, branch_id: str) -> threading.Lock:
    key = (story_id, branch_id)
    with _compact_locks_meta:
        if key not in _compact_locks:
            _compact_locks[key] = threading.Lock()
        return _compact_locks[key]


def _format_messages(messages: list[dict]) -> str:
    """Format messages for the compaction prompt."""
    lines = []
    for msg in messages:
        prefix = "【玩家】" if msg.get("role") == "user" else "【GM】"
        content = msg.get("content", "")
        # Truncate very long messages
        if len(content) > 1000:
            content = content[:1000] + "…（略）"
        lines.append(f"{prefix}\n{content}")
    return "\n\n".join(lines)


def compact_async(story_id: str, branch_id: str, full_timeline: list[dict]):
    """Trigger background compaction. Non-blocking."""
    def _do_compact():
        lock = _get_lock(story_id, branch_id)
        if not lock.acquire(blocking=False):
            log.info("    compaction: already running for %s/%s, skipping", story_id, branch_id)
            return
        try:
            _run_compaction(story_id, branch_id, full_timeline)
        except Exception as e:
            log.info("    compaction: EXCEPTION %s", e)
        finally:
            lock.release()

    t = threading.Thread(target=_do_compact, daemon=True)
    t.start()


def _run_compaction(story_id: str, branch_id: str, full_timeline: list[dict]):
    """Actually perform compaction (runs in background thread)."""
    import time as _time
    from llm_bridge import call_oneshot, get_last_usage
    import usage_db

    recap = load_recap(story_id, branch_id)
    compacted_through = recap.get("compacted_through_index", -1)

    # Messages to compact: from compacted_through+1 to len-RECENT_WINDOW
    compact_end = len(full_timeline) - RECENT_WINDOW
    if compact_end <= compacted_through + 1:
        return  # Nothing to compact

    msgs_to_compact = full_timeline[compacted_through + 1 : compact_end]
    if not msgs_to_compact:
        return

    log.info("    compaction: summarizing %d messages (idx %d-%d) for %s/%s",
             len(msgs_to_compact), compacted_through + 1, compact_end - 1,
             story_id, branch_id)

    character_name = get_character_name(story_id, branch_id)

    existing_recap = ""
    if recap.get("recap_text"):
        existing_recap = f"以下是先前的敘事回顧（請在此基礎上延續）：\n\n{recap['recap_text']}"

    prompt = _COMPACT_PROMPT.format(
        character_name=character_name,
        existing_recap=existing_recap,
        messages=_format_messages(msgs_to_compact),
    )

    t0 = _time.time()
    new_recap_text = call_oneshot(prompt)
    _compact_elapsed = _time.time() - t0
    usage = get_last_usage()
    if usage:
        try:
            usage_db.log_usage(
                story_id=story_id, provider=usage.get("provider", ""),
                model=usage.get("model", ""), call_type="compaction",
                prompt_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("output_tokens"),
                total_tokens=usage.get("total_tokens"),
                branch_id=branch_id, elapsed_ms=int(_compact_elapsed * 1000),
            )
        except Exception:
            pass
    if not new_recap_text:
        log.info("    compaction: LLM returned empty, aborting")
        return

    new_recap_text = new_recap_text.strip()

    # Check if meta-compaction needed (recap too long)
    if len(new_recap_text) > RECAP_CHAR_CAP:
        log.info("    compaction: recap too long (%d chars), meta-compacting", len(new_recap_text))
        meta_prompt = _META_COMPACT_PROMPT.format(character_name=character_name, recap=new_recap_text)
        t0 = _time.time()
        meta_result = call_oneshot(meta_prompt)
        _meta_elapsed = _time.time() - t0
        usage = get_last_usage()
        if usage:
            try:
                usage_db.log_usage(
                    story_id=story_id, provider=usage.get("provider", ""),
                    model=usage.get("model", ""), call_type="compaction",
                    prompt_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    branch_id=branch_id, elapsed_ms=int(_meta_elapsed * 1000),
                )
            except Exception:
                pass
        if meta_result and meta_result.strip():
            new_recap_text = meta_result.strip()
            log.info("    compaction: meta-compacted to %d chars", len(new_recap_text))

    turn_count = sum(1 for m in msgs_to_compact if m.get("role") == "user")

    recap["recap_text"] = new_recap_text
    recap["compacted_through_index"] = compact_end - 1
    recap["last_compacted_at"] = datetime.now(timezone.utc).isoformat()
    recap["total_turns_compacted"] = recap.get("total_turns_compacted", 0) + turn_count

    save_recap(story_id, branch_id, recap)
    log.info("    compaction: done — recap=%d chars, compacted_through=%d",
             len(new_recap_text), compact_end - 1)
