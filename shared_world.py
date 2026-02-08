"""Cross-agent awareness for multi-agent shared universe.

Aggregates data from all agents to build:
- Leaderboard (排行榜)
- Hub presence (誰在主神空間)
- Context injection for human player + agent branches
- Adventure summaries
- Bidirectional encounter events
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("rpg")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _shared_world_path(story_id: str) -> str:
    return os.path.join(BASE_DIR, "data", "stories", story_id, "shared_world.json")


def _encounters_path(story_id: str, branch_id: str) -> str:
    return os.path.join(
        BASE_DIR, "data", "stories", story_id,
        "branches", branch_id, "encounters.json"
    )


def _adventure_summary_path(story_id: str, branch_id: str) -> str:
    return os.path.join(
        BASE_DIR, "data", "stories", story_id,
        "branches", branch_id, "adventure_summary.json"
    )


def _load_json(path: str, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_shared_world(story_id: str) -> dict | None:
    return _load_json(_shared_world_path(story_id))


def _save_shared_world(story_id: str, world: dict):
    _save_json(_shared_world_path(story_id), world)


# ---------------------------------------------------------------------------
# Rebuild shared world cache
# ---------------------------------------------------------------------------

def rebuild_shared_world(story_id: str) -> dict:
    """Read every agent's branch data and build shared_world.json cache."""
    from agent_manager import _load_agents
    from auto_play import load_run_state
    from app import _load_character_state

    agents_data = _load_agents(story_id)
    leaderboard = []
    hub_presence = []
    recent_achievements = []

    for agent_id, agent in agents_data.get("agents", {}).items():
        branch_id = agent["branch_id"]

        char_state = _load_character_state(story_id, branch_id)
        if not char_state:
            continue

        try:
            run_state = load_run_state(story_id, branch_id)
        except Exception:
            run_state = None

        entry = {
            "agent_id": agent_id,
            "name": char_state.get("name", agent["name"]),
            "reward_points": char_state.get("reward_points", 0),
            "completed_missions": len(char_state.get("completed_missions", [])),
            "gene_lock": char_state.get("gene_lock", "未開啟"),
            "dungeon_count": run_state.dungeon_count if run_state else 0,
            "current_phase": run_state.phase if run_state else "unknown",
            "current_status": char_state.get("current_status", ""),
            "status": agent["status"],
        }
        leaderboard.append(entry)

        # Hub presence: agent is in hub AND running
        if run_state and run_state.phase == "hub" and agent["status"] == "running":
            hub_presence.append({
                "agent_id": agent_id,
                "name": char_state.get("name", agent["name"]),
                "appearance": char_state.get("physique", ""),
                "current_status": char_state.get("current_status", ""),
            })

        # Recent achievements
        missions = char_state.get("completed_missions", [])
        if missions:
            recent_achievements.append({
                "agent_id": agent_id,
                "name": char_state.get("name", agent["name"]),
                "achievement": missions[-1],
            })

    leaderboard.sort(key=lambda x: x.get("reward_points", 0), reverse=True)

    world = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "leaderboard": leaderboard,
        "hub_presence": hub_presence,
        "recent_achievements": recent_achievements[-10:],
    }
    _save_shared_world(story_id, world)
    return world


# ---------------------------------------------------------------------------
# Context injection for human player
# ---------------------------------------------------------------------------

def get_agents_context(
    story_id: str, current_branch_id: str, user_text: str = ""
) -> str:
    """Build [其他輪迴者動態] text for injection into human player messages."""
    from agent_manager import _load_agents

    agents_data = _load_agents(story_id)
    if not agents_data.get("agents"):
        return ""

    world = _load_shared_world(story_id)
    if not world:
        world = rebuild_shared_world(story_id)
    if not world:
        return ""

    lines = ["[其他輪迴者動態]"]

    # Hub presence
    hub = world.get("hub_presence", [])
    if hub:
        lines.append("目前在主神空間的輪迴者：")
        for p in hub:
            lines.append(f"- {p['name']}：{p.get('current_status', '休息中')}")

    # In-dungeon agents
    lb = world.get("leaderboard", [])
    in_dungeon = [
        e for e in lb
        if e["current_phase"] == "dungeon" and e["status"] == "running"
    ]
    if in_dungeon:
        lines.append("正在副本中的輪迴者：")
        for e in in_dungeon:
            lines.append(f"- {e['name']}（已完成{e['completed_missions']}次副本）")

    # Leaderboard top 5
    if lb:
        lines.append("排行榜（獎勵點）：")
        for i, e in enumerate(lb[:5], 1):
            lines.append(f"  {i}. {e['name']} — {e['reward_points']}點")

    # Name-match: detailed profile for agent mentioned by player
    matched_agent = _find_mentioned_agent(user_text, agents_data)
    if matched_agent:
        profile = _build_detailed_profile(story_id, matched_agent)
        if profile:
            lines.append("")
            lines.append(profile)

    return "\n".join(lines) if len(lines) > 1 else ""


def get_hub_presence(story_id: str) -> list[dict]:
    """Return hub presence list from cached shared_world."""
    world = _load_shared_world(story_id)
    if not world:
        world = rebuild_shared_world(story_id)
    return world.get("hub_presence", []) if world else []


def get_leaderboard(story_id: str) -> list[dict]:
    """Return leaderboard from cached shared_world."""
    world = _load_shared_world(story_id)
    if not world:
        world = rebuild_shared_world(story_id)
    return world.get("leaderboard", []) if world else []


# ---------------------------------------------------------------------------
# Context injection for agent branches (encounter events)
# ---------------------------------------------------------------------------

def get_encounter_context(story_id: str, branch_id: str) -> str:
    """Build [與其他輪迴者的互動] text for agent branch context injection."""
    encounters = _load_json(_encounters_path(story_id, branch_id), [])
    if not encounters:
        return ""

    # Show most recent 3 encounters
    recent = encounters[-3:]
    lines = ["[與其他輪迴者的互動]"]
    for enc in recent:
        lines.append(f"- {enc['from_name']}：{enc['summary']}（回合 {enc.get('turn_index', '?')}）")

    return "\n".join(lines)


def write_encounter(
    story_id: str,
    agent_branch_id: str,
    from_name: str,
    from_branch_id: str,
    summary: str,
    turn_index: int = 0,
):
    """Write a cross-branch encounter event to the agent's branch."""
    path = _encounters_path(story_id, agent_branch_id)
    encounters = _load_json(path, [])

    encounters.append({
        "from_name": from_name,
        "from_branch_id": from_branch_id,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "turn_index": turn_index,
    })

    _save_json(path, encounters)


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def _find_mentioned_agent(user_text: str, agents_data: dict) -> dict | None:
    """Check if user message mentions any agent by name."""
    if not user_text:
        return None
    for agent in agents_data.get("agents", {}).values():
        char = agent.get("character_config", {})
        name = char.get("character_state", {}).get("name", "")
        if name and name in user_text:
            return agent
    return None


# ---------------------------------------------------------------------------
# Detailed profile for GM roleplay
# ---------------------------------------------------------------------------

def _build_detailed_profile(story_id: str, agent: dict) -> str:
    """Build detailed profile text so GM can roleplay this agent."""
    from app import _load_character_state

    branch_id = agent["branch_id"]
    char_state = _load_character_state(story_id, branch_id)
    if not char_state:
        return ""

    char_config = agent.get("character_config", {})

    # Load adventure summary if it exists
    summary_data = _load_json(_adventure_summary_path(story_id, branch_id))
    summary_text = summary_data.get("summary", "") if summary_data else ""

    name = char_state.get("name", agent["name"])
    lines = [
        f"[輪迴者「{name}」詳細資料 — 供你扮演此角色時參考]",
        f"性格：{char_config.get('personality', '未知')}",
        f"體質：{char_state.get('physique', '未知')}",
        f"精神力：{char_state.get('spirit', '未知')}",
        f"基因鎖：{char_state.get('gene_lock', '未開啟')}",
        f"獎勵點：{char_state.get('reward_points', 0)}",
        f"當前狀態：{char_state.get('current_status', '未知')}",
    ]

    inventory = char_state.get("inventory", [])
    if inventory:
        lines.append(f"裝備：{', '.join(str(i) for i in inventory[:5])}")

    missions = char_state.get("completed_missions", [])
    if missions:
        lines.append(f"已完成副本：{', '.join(str(m) for m in missions)}")

    if summary_text:
        lines.append(f"冒險經歷：{summary_text}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Adventure summary generation
# ---------------------------------------------------------------------------

def generate_adventure_summary(story_id: str, agent_id: str) -> str:
    """Use call_oneshot() to generate a narrative summary of an agent's adventure."""
    from app import get_full_timeline, _load_character_state
    from llm_bridge import call_oneshot
    from agent_manager import _load_agents

    agents_data = _load_agents(story_id)
    agent = agents_data["agents"].get(agent_id)
    if not agent:
        return ""

    branch_id = agent["branch_id"]
    timeline = get_full_timeline(story_id, branch_id)
    if not timeline:
        return ""

    # Take last 50 messages, truncated
    recent = timeline[-50:]
    context_lines = []
    for msg in recent:
        role = "玩家" if msg.get("role") == "user" else "GM"
        content = msg.get("content", "")[:200]
        context_lines.append(f"【{role}】{content}")

    char_state = _load_character_state(story_id, branch_id)

    prompt = (
        "根據以下對話紀錄，用200-300字總結這位輪迴者的冒險經歷。"
        "重點包括：經歷了哪些副本、關鍵抉擇、獲得的能力、遇到的危機。\n\n"
        f"角色名稱：{char_state.get('name', '未知')}\n"
        f"角色狀態：{json.dumps(char_state, ensure_ascii=False)[:500]}\n\n"
        f"對話紀錄：\n" + "\n".join(context_lines) + "\n\n"
        "請直接輸出摘要文字，不要加任何標記。"
    )

    summary = call_oneshot(prompt)
    if not summary:
        return ""

    summary_data = {
        "agent_id": agent_id,
        "name": char_state.get("name", ""),
        "summary": summary.strip(),
        "last_updated_at_turn": len(timeline) // 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json(_adventure_summary_path(story_id, branch_id), summary_data)

    return summary.strip()


# ---------------------------------------------------------------------------
# Update agent NPC data from real character state
# ---------------------------------------------------------------------------

def update_agent_npc(story_id: str, agent_id: str):
    """Update the agent's NPC entry in the player's main branch with fresh data."""
    from agent_manager import _load_agents
    from app import _load_character_state, _save_npc

    agents_data = _load_agents(story_id)
    agent = agents_data["agents"].get(agent_id)
    if not agent:
        return

    branch_id = agent["branch_id"]
    char_state = _load_character_state(story_id, branch_id)
    if not char_state:
        return

    char_config = agent.get("character_config", {})
    name = char_state.get("name", agent["name"])

    npc_update = {
        "name": name,
        "role": "輪迴者（獨立冒險者）",
        "appearance": char_state.get("physique", ""),
        "backstory": char_config.get("personality", ""),
        "current_status": char_state.get("current_status", ""),
        "is_agent": True,
        "agent_id": agent_id,
    }

    missions = char_state.get("completed_missions", [])
    if missions:
        npc_update["traits"] = [
            "獨立冒險者",
            f"已完成{len(missions)}次副本",
            f"最近副本：{missions[-1]}",
        ]

    _save_npc(story_id, npc_update, branch_id="main")
