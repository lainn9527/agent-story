"""Agent lifecycle management for multi-agent shared universe.

Manages AI agents that run independent adventures in the same 主神空間.
Each agent gets its own auto_play branch and runs in a background thread.

Status flow: created → running → paused ↔ running → stopped
             (also "error" from any state)
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone

log = logging.getLogger("rpg")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
_active_threads: dict[str, threading.Thread] = {}  # agent_id → thread
_tree_locks: dict[str, threading.Lock] = {}         # story_id → lock
_tree_locks_lock = threading.Lock()                  # protects _tree_locks dict


def _get_tree_lock(story_id: str) -> threading.Lock:
    """Get or create a lock for timeline_tree.json writes."""
    with _tree_locks_lock:
        if story_id not in _tree_locks:
            _tree_locks[story_id] = threading.Lock()
        return _tree_locks[story_id]


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _agents_path(story_id: str) -> str:
    return os.path.join(BASE_DIR, "data", "stories", story_id, "agents.json")


def _load_agents(story_id: str) -> dict:
    path = _agents_path(story_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"agents": {}}


def _save_agents(story_id: str, data: dict):
    path = _agents_path(story_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_agent(story_id: str, agent_id: str) -> dict | None:
    """Return a single agent dict or None."""
    return _load_agents(story_id).get("agents", {}).get(agent_id)


def list_agents(story_id: str) -> list[dict]:
    """Return all agents as a list."""
    return list(_load_agents(story_id).get("agents", {}).values())


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_agent(
    story_id: str,
    name: str,
    character_config: dict,
    provider: str = "claude_cli",
    auto_play_config: dict | None = None,
) -> dict:
    """Create a new agent: generate branch, register in agents.json.

    character_config should have: personality, opening_message, character_state
    """
    from auto_play import AutoPlayConfig, setup

    agent_id = f"agent_{uuid.uuid4().hex[:8]}"
    hex_suffix = agent_id.replace("agent_", "")

    ap_cfg = auto_play_config or {}
    config = AutoPlayConfig(
        story_id=story_id,
        blank=True,
        character=character_config.get("character_state"),
        character_personality=character_config.get(
            "personality", "保持角色一致性，做出符合角色性格的選擇。"
        ),
        opening_message=character_config.get(
            "opening_message", "我剛到這裡，準備開始冒險。"
        ),
        provider=provider,
        max_turns=ap_cfg.get("max_turns", 200),
        max_hub_turns=ap_cfg.get("max_hub_turns", 10),
        max_dungeons=ap_cfg.get("max_dungeons"),
        turn_delay=ap_cfg.get("turn_delay", 3.0),
        skip_images=ap_cfg.get("skip_images", True),
        web_search=ap_cfg.get("web_search", True),
        branch_id=f"auto_{hex_suffix}",
    )

    _, branch_id = setup(config)

    agent = {
        "id": agent_id,
        "name": name,
        "branch_id": branch_id,
        "character_config": character_config,
        "status": "created",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "paused_at": None,
        "provider": provider,
        "max_turns": config.max_turns,
        "auto_play_config": ap_cfg,
    }

    agents_data = _load_agents(story_id)
    agents_data["agents"][agent_id] = agent
    _save_agents(story_id, agents_data)

    return agent


# ---------------------------------------------------------------------------
# Start / Resume
# ---------------------------------------------------------------------------

def start_agent(story_id: str, agent_id: str) -> bool:
    """Start or resume an agent in a background thread."""
    from auto_play import AutoPlayConfig, auto_play, load_run_state

    agents_data = _load_agents(story_id)
    agent = agents_data["agents"].get(agent_id)
    if not agent or agent["status"] == "running":
        return False

    # Check if resuming
    branch_id = agent["branch_id"]
    has_state = load_run_state(story_id, branch_id) is not None
    is_resume = has_state and agent["status"] in ("paused", "stopped", "error")

    agent["status"] = "running"
    agent["started_at"] = datetime.now(timezone.utc).isoformat()
    agent["paused_at"] = None
    _save_agents(story_id, agents_data)

    char_config = agent.get("character_config", {})
    ap_cfg = agent.get("auto_play_config", {})

    # Build AutoPlayConfig — only pass fields that exist in the dataclass
    valid_keys = {
        "max_hub_turns", "max_dungeons", "turn_delay",
        "skip_images", "web_search", "max_errors",
    }
    extra = {k: v for k, v in ap_cfg.items() if k in valid_keys and v is not None}

    config = AutoPlayConfig(
        story_id=story_id,
        character=char_config.get("character_state"),
        character_personality=char_config.get("personality", "..."),
        opening_message=char_config.get("opening_message", "..."),
        max_turns=agent.get("max_turns", 200),
        resume=is_resume,
        branch_id=branch_id,
        provider=agent.get("provider"),
        agent_id=agent_id,
        **extra,
    )

    def _run():
        try:
            auto_play(config)
        except Exception as e:
            log.exception("Agent %s crashed: %s", agent_id, e)
        finally:
            data = _load_agents(story_id)
            ag = data["agents"].get(agent_id)
            if ag and ag["status"] == "running":
                ag["status"] = "stopped"
                _save_agents(story_id, data)
            _active_threads.pop(agent_id, None)

    t = threading.Thread(target=_run, daemon=True, name=f"agent-{agent_id}")
    _active_threads[agent_id] = t
    t.start()
    return True


# resume is the same as start — it auto-detects saved state
resume_agent = start_agent


# ---------------------------------------------------------------------------
# Pause / Stop
# ---------------------------------------------------------------------------

def pause_agent(story_id: str, agent_id: str) -> bool:
    """Set status='paused'. The running thread exits on its next turn check."""
    agents_data = _load_agents(story_id)
    agent = agents_data["agents"].get(agent_id)
    if not agent or agent["status"] != "running":
        return False
    agent["status"] = "paused"
    agent["paused_at"] = datetime.now(timezone.utc).isoformat()
    _save_agents(story_id, agents_data)
    return True


def stop_agent(story_id: str, agent_id: str) -> bool:
    """Set status='stopped'. The running thread exits on its next turn check."""
    agents_data = _load_agents(story_id)
    agent = agents_data["agents"].get(agent_id)
    if not agent:
        return False
    agent["status"] = "stopped"
    _save_agents(story_id, agents_data)
    return True


def delete_agent(story_id: str, agent_id: str) -> bool:
    """Stop agent and remove from registry (branch data is kept)."""
    agents_data = _load_agents(story_id)
    agent = agents_data["agents"].pop(agent_id, None)
    if not agent:
        return False
    agent["status"] = "stopped"
    _save_agents(story_id, agents_data)
    _active_threads.pop(agent_id, None)
    return True


# ---------------------------------------------------------------------------
# Migration: import existing auto_ branches as stopped agents
# ---------------------------------------------------------------------------

def migrate_auto_branches(story_id: str):
    """Scan timeline_tree for auto_ branches not in agents.json, import them."""
    from app import _load_tree, _load_character_state

    agents_data = _load_agents(story_id)
    known_branches = {a["branch_id"] for a in agents_data["agents"].values()}

    tree = _load_tree(story_id)
    for bid, branch in tree.get("branches", {}).items():
        if not bid.startswith("auto_") or bid in known_branches:
            continue
        if branch.get("deleted"):
            continue

        # Build minimal agent entry
        char_state = _load_character_state(story_id, bid)
        name = char_state.get("name", "Unknown") if char_state else "Unknown"
        agent_id = f"agent_{bid.replace('auto_', '')}"

        agent = {
            "id": agent_id,
            "name": name,
            "branch_id": bid,
            "character_config": {"character_state": char_state or {}},
            "status": "stopped",
            "created_at": branch.get("created_at", ""),
            "started_at": None,
            "paused_at": None,
            "provider": "claude_cli",
            "max_turns": 200,
            "auto_play_config": {},
        }
        agents_data["agents"][agent_id] = agent

    _save_agents(story_id, agents_data)
