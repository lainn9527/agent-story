"""Background NPC evolution — async simulation of NPC autonomous activities."""

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("rpg")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")

NPC_EVOLUTION_INTERVAL = 3       # every N player turns
MIN_COOLDOWN_SECONDS = 120       # minimum gap between calls
CLAUDE_TIMEOUT = 60              # seconds for the evolution call

# Track last run time per (story_id, branch_id) to enforce cooldown
_last_run: dict[tuple[str, str], float] = {}


def _activities_path(story_id: str, branch_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id, "branches", branch_id, "npc_activities.json")


def _load_activities(story_id: str, branch_id: str) -> list[dict]:
    path = _activities_path(story_id, branch_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_activities(story_id: str, branch_id: str, activities: list[dict]):
    path = _activities_path(story_id, branch_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(activities, f, ensure_ascii=False, indent=2)


def should_run_evolution(story_id: str, branch_id: str, turn_index: int) -> bool:
    """Check if NPC evolution should run based on turn interval and cooldown."""
    if turn_index < NPC_EVOLUTION_INTERVAL:
        return False
    if turn_index % NPC_EVOLUTION_INTERVAL != 0:
        return False

    key = (story_id, branch_id)
    last = _last_run.get(key, 0)
    if time.time() - last < MIN_COOLDOWN_SECONDS:
        return False

    return True


def run_npc_evolution_async(
    story_id: str,
    branch_id: str,
    turn_index: int,
    npc_profiles: str,
    recent_context: str,
):
    """Run NPC evolution in a background thread using Claude CLI."""
    key = (story_id, branch_id)
    _last_run[key] = time.time()

    def _run():
        try:
            # Get last round's activities to prevent repetition
            prev_activities = _load_activities(story_id, branch_id)
            prev_summary = ""
            if prev_activities:
                last_entry = prev_activities[-1]
                prev_lines = []
                for act in last_entry.get("activities", []):
                    prev_lines.append(f"- {act.get('npc_name', '?')}：{act.get('activity', '')}，地點：{act.get('location', '')}")
                if prev_lines:
                    prev_summary = "\n## 上一輪活動（不要重複）\n" + "\n".join(prev_lines) + "\n"

            prompt = (
                "你是主神空間 RPG 的 NPC 行為模擬器。根據以下 NPC 資料和最近劇情，"
                "模擬每個 NPC 在當前時間段的自主活動。\n\n"
                f"## NPC 資料\n{npc_profiles}\n\n"
                f"## 最近劇情\n{recent_context}\n\n"
                f"{prev_summary}"
                "## 規則\n"
                "1. 活動要反映角色性格（勇敢的人去訓練場、謹慎的人去研究資料等）\n"
                "2. 不要所有人在同一地點做同一件事\n"
                "3. inner_thought 用角色自己的語氣寫一句內心獨白（帶口語特色）\n"
                "4. 不要重複上一輪的活動內容\n\n"
                "請為每個 NPC 生成一條簡短的自主活動描述，格式為 JSON 陣列：\n"
                '```json\n[\n  {"npc_name": "名字", "activity": "正在做什麼", '
                '"mood": "情緒", "location": "地點", '
                '"inner_thought": "用角色語氣的一句內心獨白"}\n]\n```\n'
                "只輸出 JSON，不要其他文字。"
            )

            from llm_bridge import call_oneshot
            response_text = call_oneshot(prompt)
            if not response_text:
                log.warning("    npc_evolution: LLM returned empty response")
                return

            # Extract JSON array from response
            import re
            json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
            if not json_match:
                log.warning("    npc_evolution: no JSON array found in response")
                return

            activities_data = json.loads(json_match.group())

            entry = {
                "turn_index": turn_index,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "activities": activities_data,
            }

            all_activities = _load_activities(story_id, branch_id)
            all_activities.append(entry)
            # Keep last 20 entries
            if len(all_activities) > 20:
                all_activities = all_activities[-20:]
            _save_activities(story_id, branch_id, all_activities)

            log.info("    npc_evolution: saved %d NPC activities for turn %d",
                     len(activities_data), turn_index)

        except subprocess.TimeoutExpired:
            log.warning("    npc_evolution: timeout after %ds", CLAUDE_TIMEOUT)
        except Exception as e:
            log.warning("    npc_evolution: error — %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def get_recent_activities(story_id: str, branch_id: str, limit: int = 2) -> str:
    """Return formatted NPC activity text for context injection."""
    all_activities = _load_activities(story_id, branch_id)
    if not all_activities:
        return ""

    recent = all_activities[-limit:]
    lines = ["[NPC 近期動態]"]
    for entry in recent:
        for act in entry.get("activities", []):
            name = act.get("npc_name", "?")
            activity = act.get("activity", "")
            mood = act.get("mood", "")
            location = act.get("location", "")
            inner = act.get("inner_thought", "")
            parts = [f"{name}：{activity}"]
            if mood:
                parts.append(f"情緒：{mood}")
            if location:
                parts.append(f"地點：{location}")
            line = "- " + "，".join(parts)
            if inner:
                line += f"\n  *（{name}心想：{inner}）*"
            lines.append(line)

    return "\n".join(lines)


def get_all_activities(story_id: str, branch_id: str) -> list[dict]:
    """Return all stored NPC activities for a branch."""
    return _load_activities(story_id, branch_id)
