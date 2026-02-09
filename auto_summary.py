"""Auto-play summary generation — periodic summaries of auto-play progress."""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from story_utils import get_character_name

log = logging.getLogger("auto_play")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")

SUMMARY_INTERVAL = 5        # every N turns
MIN_COOLDOWN_SECONDS = 30   # minimum gap between summary calls

# Track last run time per (story_id, branch_id) to enforce cooldown
_last_run: dict[tuple[str, str], float] = {}


def _summaries_path(story_id: str, branch_id: str) -> str:
    return os.path.join(
        STORIES_DIR, story_id, "branches", branch_id, "auto_play_summaries.json"
    )


def _load_summaries(story_id: str, branch_id: str) -> list[dict]:
    path = _summaries_path(story_id, branch_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_summaries(story_id: str, branch_id: str, summaries: list[dict]):
    path = _summaries_path(story_id, branch_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)


def should_generate_summary(
    story_id: str, branch_id: str, current_turn: int, phase_changed: bool
) -> bool:
    """Check if summary should be generated based on turn interval and cooldown."""
    if current_turn < SUMMARY_INTERVAL - 1:
        return False

    due = (current_turn % SUMMARY_INTERVAL == SUMMARY_INTERVAL - 1) or phase_changed
    if not due:
        return False

    # Cooldown check
    key = (story_id, branch_id)
    last = _last_run.get(key, 0)
    if time.time() - last < MIN_COOLDOWN_SECONDS:
        return False

    # Avoid duplicate: check if we already have a summary ending at this turn
    existing = _load_summaries(story_id, branch_id)
    for s in existing:
        if s.get("turn_end") == current_turn:
            return False

    return True


def generate_summary_async(
    story_id: str,
    branch_id: str,
    turn_start: int,
    turn_end: int,
    phase: str,
    recent_messages: list[dict],
    run_state_dict: dict,
):
    """Generate a summary in a background daemon thread."""
    key = (story_id, branch_id)
    _last_run[key] = time.time()

    def _run():
        try:
            # Build message text for summarization
            msg_lines = []
            for msg in recent_messages:
                role = "【玩家】" if msg.get("role") == "user" else "【GM】"
                content = msg.get("content", "")
                if len(content) > 500:
                    content = content[:500] + "..."
                msg_lines.append(f"{role}\n{content}")

            messages_text = "\n\n".join(msg_lines)

            character_name = get_character_name(story_id, branch_id)

            prompt = (
                "你是主神空間 RPG 的摘要生成器。根據以下遊戲記錄，生成一段精華摘要。\n\n"
                f"## 回合範圍\nTurn {turn_start} ~ {turn_end}\n\n"
                f"## 當前階段\n{phase}\n\n"
                f"## 遊戲記錄\n{messages_text}\n\n"
                f"## 重要規則\n"
                f"記錄中【玩家】的角色名為「{character_name}」。摘要中請一律使用「{character_name}」稱呼玩家角色，不要用其他名字替代。\n\n"
                "請用繁體中文，嚴格按照以下 JSON 格式回覆（不要加其他文字）：\n"
                '{"summary": "2-3句精華摘要，描述這段期間的主要進展", '
                '"key_events": ["事件1", "事件2", ...]}\n'
            )

            from llm_bridge import call_oneshot, get_last_usage
            import usage_db
            t0 = time.time()
            response_text = call_oneshot(prompt)
            _summary_elapsed = time.time() - t0
            usage = get_last_usage()
            if usage:
                try:
                    usage_db.log_usage(
                        story_id=story_id, provider=usage.get("provider", ""),
                        model=usage.get("model", ""), call_type="auto_summary",
                        prompt_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        total_tokens=usage.get("total_tokens"),
                        branch_id=branch_id, elapsed_ms=int(_summary_elapsed * 1000),
                    )
                except Exception:
                    pass
            if not response_text:
                log.warning("    auto_summary: LLM returned empty response")
                return

            # Extract JSON from response (handle markdown code blocks)
            text = response_text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                text = "\n".join(lines)

            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if not json_match:
                log.warning("    auto_summary: no JSON found in response")
                return

            data = json.loads(json_match.group())

            entry = {
                "turn_start": turn_start,
                "turn_end": turn_end,
                "phase": phase,
                "summary": data.get("summary", ""),
                "key_events": data.get("key_events", []),
                "dungeon_count": run_state_dict.get("dungeon_count", 0),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            all_summaries = _load_summaries(story_id, branch_id)
            all_summaries.append(entry)
            _save_summaries(story_id, branch_id, all_summaries)

            log.info(
                "    auto_summary: saved summary for turns %d-%d (%s)",
                turn_start, turn_end, phase,
            )

        except Exception as e:
            log.warning("    auto_summary: error — %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def get_summaries(story_id: str, branch_id: str) -> list[dict]:
    """Return all summaries for a branch (used by API)."""
    return _load_summaries(story_id, branch_id)
