"""Snapshot-based cross-agent awareness for multi-agent shared universe.

Agent data is accessed via time-indexed snapshots (read-only reference).
No live cross-branch reads, no NPC sync, no bidirectional encounters.

Provides:
- Agent context injection for human player (snapshot-based)
- Leaderboard from snapshots
- Detailed agent profile from snapshot (for GM roleplay)
- Snapshot storage and lookup
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

log = logging.getLogger("rpg")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Per-branch lock for snapshot read-modify-write
_snapshot_locks: dict[str, threading.Lock] = {}
_snapshot_locks_lock = threading.Lock()


def _get_snapshot_lock(story_id: str, branch_id: str) -> threading.Lock:
    key = f"{story_id}/{branch_id}"
    with _snapshot_locks_lock:
        if key not in _snapshot_locks:
            _snapshot_locks[key] = threading.Lock()
        return _snapshot_locks[key]


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _snapshots_path(story_id: str, branch_id: str) -> str:
    return os.path.join(
        BASE_DIR, "data", "stories", story_id,
        "branches", branch_id, "agent_snapshots.json",
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
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Snapshot storage
# ---------------------------------------------------------------------------

def save_agent_snapshot(
    story_id: str, branch_id: str,
    turn: int, phase: str,
    char_state: dict,
    completed_missions: list | None = None,
    summary: str = "",
):
    """Append a snapshot to agent_snapshots.json (thread-safe)."""
    from world_timer import get_world_day

    snapshot = {
        "world_day": get_world_day(story_id, branch_id),
        "turn": turn,
        "phase": phase,
        "character_state": char_state,
        "summary": summary,
        "completed_missions": completed_missions or char_state.get("completed_missions", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    with _get_snapshot_lock(story_id, branch_id):
        path = _snapshots_path(story_id, branch_id)
        snapshots = _load_json(path, [])
        snapshots.append(snapshot)
        _save_json(path, snapshots)

    log.info("    snapshot saved: branch=%s turn=%d world_day=%.1f",
             branch_id, turn, snapshot["world_day"])


def get_agent_snapshot_at(
    story_id: str, agent_branch_id: str, world_day: float,
) -> dict | None:
    """Get the most recent snapshot at or before the given world_day."""
    path = _snapshots_path(story_id, agent_branch_id)
    snapshots = _load_json(path, [])

    best = None
    for s in snapshots:
        if s["world_day"] <= world_day:
            best = s
        else:
            break  # snapshots are chronological
    return best


def get_latest_snapshot(story_id: str, agent_branch_id: str) -> dict | None:
    """Get the most recent snapshot regardless of world_day."""
    path = _snapshots_path(story_id, agent_branch_id)
    snapshots = _load_json(path, [])
    return snapshots[-1] if snapshots else None


# ---------------------------------------------------------------------------
# Snapshot summary generation
# ---------------------------------------------------------------------------

def generate_snapshot_summaries(story_id: str, agent_branch_id: str):
    """Generate narrative summaries for unsummarized snapshots.

    Called after auto_play completes or periodically during play.
    Uses lock to prevent concurrent writes.
    """
    from llm_bridge import call_oneshot

    with _get_snapshot_lock(story_id, agent_branch_id):
        path = _snapshots_path(story_id, agent_branch_id)
        snapshots = _load_json(path, [])
        if not snapshots:
            return

        updated = False
        for snap in snapshots:
            if snap.get("summary"):
                continue

            cs = snap.get("character_state", {})
            name = cs.get("name", "未知")
            missions = snap.get("completed_missions", [])
            phase = snap.get("phase", "unknown")
            turn = snap.get("turn", 0)

            prompt = (
                f"用繁體中文寫一句話（50-80字）總結輪迴者「{name}」在回合{turn}的狀態。\n"
                f"當前階段：{'副本中' if phase == 'dungeon' else '主神空間'}\n"
                f"已完成副本：{', '.join(str(m) for m in missions) if missions else '無'}\n"
                f"獎勵點：{cs.get('reward_points', 0)}\n"
                f"狀態：{cs.get('current_status', '未知')}\n\n"
                f"直接輸出摘要文字，不要加標記。"
            )

            try:
                summary = call_oneshot(prompt)
                if summary:
                    snap["summary"] = summary.strip()
                    updated = True
            except Exception as e:
                log.warning("snapshot summary generation failed: %s", e)

        if updated:
            _save_json(path, snapshots)


# ---------------------------------------------------------------------------
# Context injection for human player
# ---------------------------------------------------------------------------

def get_agents_context(
    story_id: str, branch_id: str, user_text: str = "",
) -> str:
    """Build [其他輪迴者動態] using snapshots at current branch's world_day."""
    from agent_manager import load_agents
    from world_timer import get_world_day

    agents_data = load_agents(story_id)
    if not agents_data.get("agents"):
        return ""

    current_day = get_world_day(story_id, branch_id)
    lines = ["[其他輪迴者動態]"]

    # Single pass: load each agent's snapshot once, cache for reuse
    agent_snapshots: dict[str, dict | None] = {}
    for agent_id, agent in agents_data["agents"].items():
        agent_snapshots[agent_id] = get_agent_snapshot_at(
            story_id, agent["branch_id"], current_day,
        )

    # Status list for each agent
    entries = []
    for agent_id, agent in agents_data["agents"].items():
        snapshot = agent_snapshots[agent_id]
        if not snapshot:
            lines.append(f"- {agent['name']}：剛進入主神空間（新人）")
            continue

        cs = snapshot.get("character_state", {})
        name = cs.get("name", agent["name"])
        phase = snapshot.get("phase", "unknown")
        missions = len(snapshot.get("completed_missions", []))

        if phase == "hub":
            lines.append(f"- {name}：在主神空間（已完成{missions}次副本）")
        elif phase == "dungeon":
            lines.append(f"- {name}：正在副本中")
        else:
            lines.append(f"- {name}：狀態未知")

        entries.append({
            "name": name,
            "reward_points": cs.get("reward_points", 0),
        })

    # Leaderboard from cached snapshots
    entries.sort(key=lambda x: x["reward_points"], reverse=True)
    if entries:
        lines.append("排行榜（獎勵點）：")
        for i, e in enumerate(entries[:5], 1):
            lines.append(f"  {i}. {e['name']} — {e['reward_points']}點")

    # Name-match: detailed profile from cached snapshot
    matched_agent = _find_mentioned_agent(user_text, agents_data)
    if matched_agent:
        snapshot = agent_snapshots.get(matched_agent["id"])
        if snapshot:
            profile = _build_profile_from_snapshot(matched_agent, snapshot)
            lines.append("")
            lines.append(profile)

    return "\n".join(lines) if len(lines) > 1 else ""


def get_leaderboard(story_id: str, world_day: float | None = None) -> list[dict]:
    """Build leaderboard from snapshots. If world_day is None, use latest snapshots."""
    from agent_manager import load_agents

    agents_data = load_agents(story_id)
    entries = []

    for agent_id, agent in agents_data.get("agents", {}).items():
        if world_day is not None:
            snapshot = get_agent_snapshot_at(story_id, agent["branch_id"], world_day)
        else:
            snapshot = get_latest_snapshot(story_id, agent["branch_id"])

        if not snapshot:
            continue

        cs = snapshot.get("character_state", {})
        entries.append({
            "agent_id": agent_id,
            "name": cs.get("name", agent["name"]),
            "reward_points": cs.get("reward_points", 0),
            "completed_missions": len(snapshot.get("completed_missions", [])),
            "gene_lock": cs.get("gene_lock", "未開啟"),
            "current_phase": snapshot.get("phase", "unknown"),
            "current_status": cs.get("current_status", ""),
            "status": agent.get("status", "stopped"),
        })

    entries.sort(key=lambda x: x["reward_points"], reverse=True)
    return entries


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

def _find_mentioned_agent(user_text: str, agents_data: dict) -> dict | None:
    """Check if user message mentions any agent by name (min 2 chars)."""
    if not user_text:
        return None
    for agent in agents_data.get("agents", {}).values():
        char = agent.get("character_config", {})
        name = char.get("character_state", {}).get("name", "")
        if name and len(name) >= 2 and name in user_text:
            return agent
        # Also check agent's display name
        aname = agent.get("name", "")
        if aname and len(aname) >= 2 and aname in user_text:
            return agent
    return None


# ---------------------------------------------------------------------------
# Detailed profile from snapshot
# ---------------------------------------------------------------------------

def _build_profile_from_snapshot(agent: dict, snapshot: dict) -> str:
    """Build detailed profile from a snapshot (not live branch data)."""
    char_config = agent.get("character_config", {})
    cs = snapshot.get("character_state", {})
    name = cs.get("name", agent["name"])

    lines = [
        f"[輪迴者「{name}」詳細資料 — 供你扮演此角色時參考]",
        f"性格：{char_config.get('personality', '未知')}",
        f"體質：{cs.get('physique', '未知')}",
        f"精神力：{cs.get('spirit', '未知')}",
        f"基因鎖：{cs.get('gene_lock', '未開啟')}",
        f"獎勵點：{cs.get('reward_points', 0)}",
        f"當前狀態：{cs.get('current_status', '未知')}",
    ]

    inventory = cs.get("inventory", [])
    if inventory:
        lines.append(f"裝備：{', '.join(str(i) for i in inventory[:5])}")

    missions = cs.get("completed_missions", [])
    if missions:
        lines.append(f"已完成副本：{', '.join(str(m) for m in missions)}")

    summary = snapshot.get("summary", "")
    if summary:
        lines.append(f"冒險經歷：{summary}")

    return "\n".join(lines)
