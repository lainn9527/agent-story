"""
Dungeon System - Instance/副本 progression tracking and validation.

Provides:
- Dungeon template loading (13 dungeons with difficulty, nodes, areas, progression_rules)
- Per-branch dungeon progress tracking (mainline/exploration progress, discovered areas)
- Hard constraint validation (cap rank/gene_lock growth to dungeon limits)
- System prompt context generation
"""

import os
import json
import re
import logging
import threading
import copy
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Thread-safe locks per story
_locks = {}
_locks_lock = threading.Lock()


def _get_lock(story_id: str, branch_id: str) -> threading.Lock:
    """Get or create a thread-safe lock for a specific branch."""
    key = f"{story_id}:{branch_id}"
    with _locks_lock:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _story_dir(story_id: str) -> str:
    """Get story directory path."""
    return os.path.join("data", "stories", story_id)


def _branch_dir(story_id: str, branch_id: str) -> str:
    """Get branch directory path."""
    return os.path.join(_story_dir(story_id), "branches", branch_id)


def _load_json(path: str, default=None):
    """Load JSON file, return default if not exists."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed to load {path}: {e}")
        return default


def _save_json(path: str, data: dict):
    """Save JSON file with pretty formatting."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ========== Data Loading/Saving ==========

def _load_dungeon_templates(story_id: str) -> dict:
    """Load all dungeon templates for a story."""
    path = os.path.join(_story_dir(story_id), "dungeons_template.json")
    templates = _load_json(path, {"dungeons": []})
    return templates


def _load_dungeon_template(story_id: str, dungeon_id: str) -> Optional[dict]:
    """Load a specific dungeon template by ID."""
    templates = _load_dungeon_templates(story_id)
    for dungeon in templates.get("dungeons", []):
        if dungeon["id"] == dungeon_id:
            return dungeon
    return None


def _load_dungeon_progress(story_id: str, branch_id: str) -> Optional[dict]:
    """Load dungeon progress for a branch."""
    path = os.path.join(_branch_dir(story_id, branch_id), "dungeon_progress.json")
    return _load_json(path)


def _save_dungeon_progress(story_id: str, branch_id: str, progress: dict):
    """Save dungeon progress for a branch."""
    path = os.path.join(_branch_dir(story_id, branch_id), "dungeon_progress.json")
    with _get_lock(story_id, branch_id):
        _save_json(path, progress)


# ========== Helper Functions for Character State ==========

def _load_character_state(story_id: str, branch_id: str) -> dict:
    """Load character state (imported from app.py logic)."""
    path = os.path.join(_branch_dir(story_id, branch_id), "character_state.json")
    return _load_json(path, {})


def _save_character_state(story_id: str, branch_id: str, state: dict):
    """Save character state."""
    path = os.path.join(_branch_dir(story_id, branch_id), "character_state.json")
    _save_json(path, state)


# ========== Rank/Gene Lock Parsing ==========

def _parse_rank(rank_str: str) -> float:
    """Convert rank string (E/D/C/B/A/S/SS/SSS) to numeric value."""
    ranks = {"E": 0, "D": 1, "C": 2, "B": 3, "A": 4, "S": 5, "SS": 6, "SSS": 7}
    return ranks.get(rank_str.upper(), 0)


def _format_rank(rank_value: float) -> str:
    """Convert numeric value to rank string (supports fractional ranks)."""
    if rank_value >= 7:
        return "SSS"
    if rank_value >= 6:
        return "SS"
    if rank_value >= 5:
        return "S"
    if rank_value >= 4:
        return "A"
    if rank_value >= 3:
        return "B"
    if rank_value >= 2:
        return "C"
    if rank_value >= 1:
        return "D"
    return "E"


def _parse_gene_lock_percentage(gene_lock_str: str) -> int:
    """Extract percentage from gene lock string (e.g., '第一階 18%' → 18)."""
    if not gene_lock_str or gene_lock_str == "未開啟":
        return 0
    match = re.search(r'(\d+)%', gene_lock_str)
    return int(match.group(1)) if match else 0


def _format_gene_lock(percentage: int) -> str:
    """Format gene lock with stage + percentage."""
    if percentage <= 0:
        return "未開啟"
    elif percentage < 20:
        return f"第一階開啟中（進度 {percentage}%）"
    elif percentage < 100:
        stage = min(int(percentage / 20), 4) + 1
        stage_names = ['一', '二', '三', '四', '五']
        stage_name = stage_names[stage - 1] if stage <= 5 else '五'
        progress = percentage % 20 if percentage < 100 else 0
        return f"第{stage_name}階（進度 {progress}%）"
    else:
        return "第五階（完全開啟）"


# ========== Dungeon Lifecycle ==========

def initialize_dungeon_progress(story_id: str, branch_id: str, dungeon_id: str):
    """Initialize dungeon progress when entering a dungeon."""
    template = _load_dungeon_template(story_id, dungeon_id)
    if not template:
        raise ValueError(f"Dungeon template not found: {dungeon_id}")

    state = _load_character_state(story_id, branch_id)

    # Find initial areas (status = "discovered")
    initial_areas = [a["id"] for a in template.get("areas", []) if a.get("initial_status") == "discovered"]

    # Load or initialize progress
    progress = _load_dungeon_progress(story_id, branch_id) or {
        "history": [],
        "total_dungeons_completed": 0
    }

    # Get current world day (import from world_timer if available)
    try:
        from world_timer import get_world_day
        current_world_day = get_world_day(story_id, branch_id)
    except ImportError:
        current_world_day = {"day": 1, "hour": 0}

    progress["current_dungeon"] = {
        "dungeon_id": dungeon_id,
        "entered_at": datetime.now(timezone.utc).isoformat(),
        "entered_at_world_day": current_world_day.get("day", 1) if isinstance(current_world_day, dict) else 1,
        "current_world_day": current_world_day.get("day", 1) if isinstance(current_world_day, dict) else 1,
        "mainline_progress": 0,
        "exploration_progress": 0,
        "completed_nodes": [],
        "discovered_areas": initial_areas,
        "explored_areas": {aid: 0 for aid in initial_areas},
        "rank_on_enter": state.get("等級", "E"),
        "gene_lock_on_enter": state.get("基因鎖", "未開啟"),
        "reward_points_on_enter": state.get("獎勵點數", 0),
        "growth_budget": {
            "max_rank_progress": template["progression_rules"]["rank_progress"],
            "consumed_rank_progress": 0,
            "max_gene_lock_gain": template["progression_rules"]["gene_lock_gain"],
            "consumed_gene_lock": 0
        }
    }

    _save_dungeon_progress(story_id, branch_id, progress)
    log.info(f"Initialized dungeon progress: {story_id}/{branch_id} → {dungeon_id}")


def archive_current_dungeon(story_id: str, branch_id: str, exit_reason: str = "normal"):
    """Archive current dungeon to history when returning to Main God Space."""
    progress = _load_dungeon_progress(story_id, branch_id)
    if not progress or not progress.get("current_dungeon"):
        log.warning(f"No current dungeon to archive: {story_id}/{branch_id}")
        return

    current = progress.pop("current_dungeon")
    state = _load_character_state(story_id, branch_id)

    history_entry = {
        "dungeon_id": current["dungeon_id"],
        "entered_at": current["entered_at"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "exit_reason": exit_reason,
        "final_progress": current["mainline_progress"],
        "final_exploration": current["exploration_progress"],
        "rank_before": current.get("rank_on_enter", "E"),
        "rank_after": state.get("等級", "E"),
        "gene_lock_before": current.get("gene_lock_on_enter", "未開啟"),
        "gene_lock_after": state.get("基因鎖", "未開啟"),
        "reward_points_earned": state.get("獎勵點數", 0) - current.get("reward_points_on_enter", 0),
        "completed_nodes": current.get("completed_nodes", [])
    }

    progress["history"].append(history_entry)
    progress["total_dungeons_completed"] = len(progress["history"])
    _save_dungeon_progress(story_id, branch_id, progress)
    log.info(f"Archived dungeon: {current['dungeon_id']} (reason: {exit_reason})")


# ========== Progress Updates (called by async tag extraction) ==========

def update_dungeon_progress(story_id: str, branch_id: str, update: dict):
    """Update mainline progress and completed nodes."""
    # BE-C2: lock the entire read-modify-write to prevent race conditions
    with _get_lock(story_id, branch_id):
        path = os.path.join(_branch_dir(story_id, branch_id), "dungeon_progress.json")
        progress = _load_json(path)
        if not progress or not progress.get("current_dungeon"):
            return

        current = progress["current_dungeon"]

        # Mark newly completed nodes
        new_nodes = update.get("nodes_completed", [])
        for node_id in new_nodes:
            if node_id not in current["completed_nodes"]:
                current["completed_nodes"].append(node_id)
                log.info(f"Completed node: {node_id}")

        # Update progress (delta or absolute)
        if "progress_delta" in update:
            current["mainline_progress"] = min(100, current["mainline_progress"] + update["progress_delta"])
        elif "progress" in update:
            current["mainline_progress"] = min(100, update["progress"])
        else:
            # Auto-calculate: (completed / total) * 100
            template = _load_dungeon_template(story_id, current["dungeon_id"])
            if template:
                total_nodes = len(template["mainline"]["nodes"])
                completed = len(current["completed_nodes"])
                current["mainline_progress"] = int((completed / total_nodes) * 100) if total_nodes > 0 else 0

        # Update current world day
        try:
            from world_timer import get_world_day
            world_day = get_world_day(story_id, branch_id)
            current["current_world_day"] = world_day.get("day", 1) if isinstance(world_day, dict) else 1
        except ImportError:
            pass

        _save_json(path, progress)
    log.info(f"Updated dungeon progress: {progress['current_dungeon']['mainline_progress']}%")


def update_dungeon_area(story_id: str, branch_id: str, update: dict):
    """Update area discovery and exploration."""
    # BE-C2: lock the entire read-modify-write to prevent race conditions
    with _get_lock(story_id, branch_id):
        path = os.path.join(_branch_dir(story_id, branch_id), "dungeon_progress.json")
        progress = _load_json(path)
        if not progress or not progress.get("current_dungeon"):
            return

        current = progress["current_dungeon"]

        # Mark newly discovered areas
        new_areas = update.get("discovered_areas", [])
        for area_id in new_areas:
            if area_id not in current["discovered_areas"]:
                current["discovered_areas"].append(area_id)
                current["explored_areas"][area_id] = 0
                log.info(f"Discovered area: {area_id}")

        # Update exploration (incremental)
        area_updates = update.get("explored_area_updates", {})
        for area_id, delta in area_updates.items():
            if area_id in current["explored_areas"]:
                current["explored_areas"][area_id] = min(100, current["explored_areas"][area_id] + delta)

        # Recalculate total exploration (average of all areas)
        template = _load_dungeon_template(story_id, current["dungeon_id"])
        if template:
            all_areas = [a["id"] for a in template.get("areas", [])]
            if all_areas:
                total_exp = sum(current["explored_areas"].get(aid, 0) for aid in all_areas)
                current["exploration_progress"] = int(total_exp / len(all_areas))

        _save_json(path, progress)
    log.info(f"Updated area exploration: {progress['current_dungeon']['exploration_progress']}%")


# ========== Hard Constraint Validation (CRITICAL) ==========

def validate_dungeon_progression(story_id: str, branch_id: str, new_state: dict, old_state: dict):
    """
    Hard validation - cap growth to dungeon limits.
    This is called from _apply_state_update() in app.py.
    Modifies new_state in-place; caller must save new_state to disk afterward (BE-C1).
    """
    # BE-C2: lock the read-modify-write on dungeon_progress.json
    with _get_lock(story_id, branch_id):
        path = os.path.join(_branch_dir(story_id, branch_id), "dungeon_progress.json")
        progress = _load_json(path)
        if not progress or not progress.get("current_dungeon"):
            return  # Not in dungeon, no limits

        current = progress["current_dungeon"]
        template = _load_dungeon_template(story_id, current["dungeon_id"])
        if not template:
            return

        rules = template["progression_rules"]
        # BE-M1: use .get() with fallback to avoid KeyError
        growth = current.get("growth_budget", {
            "max_rank_progress": rules.get("rank_progress", 0),
            "consumed_rank_progress": 0,
            "max_gene_lock_gain": rules.get("gene_lock_gain", 0),
            "consumed_gene_lock": 0
        })

        # Validate rank growth
        old_rank = _parse_rank(old_state.get("等級", "E"))
        new_rank = _parse_rank(new_state.get("等級", "E"))
        rank_gain = new_rank - old_rank

        if rank_gain > 0:
            max_rank = growth.get("max_rank_progress", rules.get("rank_progress", 0))
            consumed_rank = growth.get("consumed_rank_progress", 0)
            remaining_budget = max_rank - consumed_rank
            if rank_gain > remaining_budget:
                log.warning(
                    f"Rank gain {rank_gain:.1f} exceeds remaining budget {remaining_budget:.1f}, capping to {remaining_budget:.1f}"
                )
                new_state["等級"] = _format_rank(old_rank + remaining_budget)
                growth["consumed_rank_progress"] = max_rank
            else:
                growth["consumed_rank_progress"] = consumed_rank + rank_gain

        # Validate gene lock growth
        old_gene_lock = _parse_gene_lock_percentage(old_state.get("基因鎖", "未開啟"))
        new_gene_lock = _parse_gene_lock_percentage(new_state.get("基因鎖", "未開啟"))
        gene_gain = new_gene_lock - old_gene_lock

        if gene_gain > 0:
            max_gene = growth.get("max_gene_lock_gain", rules.get("gene_lock_gain", 0))
            consumed_gene = growth.get("consumed_gene_lock", 0)
            remaining_budget = max_gene - consumed_gene
            if gene_gain > remaining_budget:
                log.warning(f"Gene lock gain {gene_gain}% exceeds budget {remaining_budget}%, capping")
                new_state["基因鎖"] = _format_gene_lock(old_gene_lock + remaining_budget)
                growth["consumed_gene_lock"] = max_gene
            else:
                growth["consumed_gene_lock"] = consumed_gene + gene_gain

        # Persist updated growth_budget back to dungeon_progress.json
        current["growth_budget"] = growth
        _save_json(path, progress)


# ========== System Prompt Context ==========

def build_dungeon_context(story_id: str, branch_id: str) -> str:
    """Generate dungeon context for system prompt {dungeon_context} placeholder."""
    progress = _load_dungeon_progress(story_id, branch_id)
    if not progress or not progress.get("current_dungeon"):
        return "（目前不在副本中，處於主神空間）"

    current = progress["current_dungeon"]
    template = _load_dungeon_template(story_id, current["dungeon_id"])
    if not template:
        return "（副本資料載入失敗）"

    # BE-M5: fix node status logic — first non-completed node is "active"
    completed_nodes = set(current.get("completed_nodes", []))
    nodes_status = []
    found_active = False
    for node in template["mainline"]["nodes"][:3]:  # Limit to 3 nodes
        if node["id"] in completed_nodes:
            status = "✓"
        elif not found_active:
            status = "▶"  # Next (active) node — only the first non-completed
            found_active = True
        else:
            status = "○"  # Locked
        nodes_status.append(f"{status} {node['title']}")

    # Growth budget
    rules = template["progression_rules"]
    growth = current.get("growth_budget", {})
    rank_budget = f"{growth.get('consumed_rank_progress', 0):.1f} / {rules['rank_progress']}"
    gene_budget = f"{growth.get('consumed_gene_lock', 0)} / {rules['gene_lock_gain']}"

    context = f"""【副本】：{template['name']}（難度 {template['difficulty']}）
【主線進度】：{current['mainline_progress']}%
{chr(10).join(nodes_status)}

【成長限制】：
- 本副本最多提升：{rules['rank_progress']} 個等級，已消耗 {rank_budget}
- 基因鎖上限：{rules['gene_lock_gain']}%，已消耗 {gene_budget}
- 階段限制：{rules['gene_lock_stage_cap']}

【重要指示】：
- 引導玩家沿著主線節點推進，但**不強制**（玩家可自由探索）
- 主線進度達到 100% 後，提示玩家可以「回歸主神空間」
- **嚴格遵守成長限制**（代碼會自動 cap 超限成長，但請在敘事中合理化）
- 獎勵公式：基礎 {rules['base_reward']} + 主線完成度 × {rules['mainline_multiplier']} + 探索度 × {rules['exploration_multiplier']}
"""
    return context.strip()


# ========== Branch Inheritance ==========

def copy_dungeon_progress(story_id: str, from_bid: str, to_bid: str):
    """Copy dungeon progress to new branch (for edit/regen/fork)."""
    progress = _load_dungeon_progress(story_id, from_bid)
    if progress:
        # Deep copy to avoid reference issues
        _save_dungeon_progress(story_id, to_bid, copy.deepcopy(progress))
        log.info(f"Copied dungeon progress: {from_bid} → {to_bid}")


def get_dungeon_progress_snapshot(story_id: str, branch_id: str) -> dict:
    """Get dungeon progress snapshot for message metadata."""
    return _load_dungeon_progress(story_id, branch_id) or {
        "history": [],
        "current_dungeon": None,
        "total_dungeons_completed": 0
    }


# ========== Initialization ==========

def ensure_dungeon_templates(story_id: str):
    """Create default dungeons_template.json if not exists."""
    path = os.path.join(_story_dir(story_id), "dungeons_template.json")
    if os.path.exists(path):
        return

    log.info(f"Creating default dungeon templates for story: {story_id}")

    # Default 13 dungeons (complete definitions from plan)
    default_templates = {
        "dungeons": [
            {
                "id": "ju_on",
                "name": "咒怨",
                "difficulty": "D",
                "description": "日本怨靈副本，伽椰子的詛咒纏繞的佐伯家",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "初入佐伯家", "hint": "調查一樓，尋找線索", "order": 1},
                        {"id": "node_2", "title": "遭遇怨靈", "hint": "第一次與伽椰子接觸，避免直接對抗", "order": 2},
                        {"id": "node_final", "title": "封印儀式", "hint": "尋找神道協會的幫助", "order": 3, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "saeki_1f", "name": "佐伯家 — 一樓", "type": "mainline", "danger": 5, "initial_status": "discovered"},
                    {"id": "saeki_2f", "name": "佐伯家 — 二樓", "type": "mainline", "danger": 7, "initial_status": "undiscovered"},
                    {"id": "saeki_attic", "name": "佐伯家 — 閣樓", "type": "side", "danger": 9, "initial_status": "hidden"},
                    {"id": "tokyo_streets", "name": "東京街區", "type": "side", "danger": 2, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 0.5,
                    "gene_lock_gain": 15,
                    "gene_lock_stage_cap": "第一階 50%",
                    "time_cost_days": 5,
                    "base_reward": 2000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"E": 1.0, "D": 0.7, "C": 0.3}
                },
                "prerequisites": {"min_rank": "E", "completed_dungeons": []}
            },
            {
                "id": "texas_chainsaw",
                "name": "德州電鋸殺人狂",
                "difficulty": "D",
                "description": "孤立農莊中的生存恐怖",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "逃離農莊", "order": 1},
                        {"id": "node_2", "title": "尋找武器", "order": 2},
                        {"id": "node_final", "title": "擊敗皮臉", "order": 3, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "farmhouse", "name": "農莊主屋", "type": "mainline", "danger": 6, "initial_status": "discovered"},
                    {"id": "barn", "name": "穀倉", "type": "mainline", "danger": 7, "initial_status": "undiscovered"},
                    {"id": "basement", "name": "地下室", "type": "side", "danger": 9, "initial_status": "hidden"}
                ],
                "progression_rules": {
                    "rank_progress": 0.5,
                    "gene_lock_gain": 15,
                    "gene_lock_stage_cap": "第一階 50%",
                    "time_cost_days": 5,
                    "base_reward": 2000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"E": 1.0, "D": 0.7, "C": 0.3}
                },
                "prerequisites": {"min_rank": "E"}
            },
            {
                "id": "resident_evil",
                "name": "生化危機",
                "difficulty": "C",
                "description": "浣熊市的喪屍末日與安布雷拉的陰謀",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "逃出市區", "order": 1},
                        {"id": "node_2", "title": "尋找疫苗", "order": 2},
                        {"id": "node_3", "title": "對抗追跡者", "order": 3},
                        {"id": "node_final", "title": "撤離浣熊市", "order": 4, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "downtown", "name": "市中心", "type": "mainline", "danger": 6, "initial_status": "discovered"},
                    {"id": "police_station", "name": "警察局", "type": "mainline", "danger": 5, "initial_status": "undiscovered"},
                    {"id": "umbrella_lab", "name": "安布雷拉實驗室", "type": "mainline", "danger": 8, "initial_status": "undiscovered"},
                    {"id": "hospital", "name": "醫院", "type": "side", "danger": 7, "initial_status": "hidden"}
                ],
                "progression_rules": {
                    "rank_progress": 0.6,
                    "gene_lock_gain": 18,
                    "gene_lock_stage_cap": "第二階 30%",
                    "time_cost_days": 7,
                    "base_reward": 3500,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"E": 1.2, "D": 1.0, "C": 0.7, "B": 0.3}
                },
                "prerequisites": {"min_rank": "D"}
            },
            {
                "id": "kisaragi_station",
                "name": "如月車站",
                "difficulty": "C",
                "description": "異空間車站的都市傳說",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "探索車站", "order": 1},
                        {"id": "node_2", "title": "破解謎題", "order": 2},
                        {"id": "node_final", "title": "找到出口", "order": 3, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "platform", "name": "月台", "type": "mainline", "danger": 5, "initial_status": "discovered"},
                    {"id": "waiting_room", "name": "候車室", "type": "mainline", "danger": 6, "initial_status": "undiscovered"},
                    {"id": "tunnel", "name": "隧道", "type": "side", "danger": 8, "initial_status": "hidden"}
                ],
                "progression_rules": {
                    "rank_progress": 0.6,
                    "gene_lock_gain": 18,
                    "gene_lock_stage_cap": "第二階 30%",
                    "time_cost_days": 7,
                    "base_reward": 3500,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"D": 1.0, "C": 0.7, "B": 0.3}
                },
                "prerequisites": {"min_rank": "D"}
            },
            {
                "id": "jurassic",
                "name": "侏羅紀公園",
                "difficulty": "C",
                "description": "失控的恐龍島嶼生存",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "逃離訪客中心", "order": 1},
                        {"id": "node_2", "title": "啟動通訊系統", "order": 2},
                        {"id": "node_3", "title": "躲避暴龍", "order": 3},
                        {"id": "node_final", "title": "離開島嶼", "order": 4, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "visitor_center", "name": "訪客中心", "type": "mainline", "danger": 5, "initial_status": "discovered"},
                    {"id": "raptor_paddock", "name": "迅猛龍圍欄", "type": "mainline", "danger": 8, "initial_status": "undiscovered"},
                    {"id": "trex_territory", "name": "暴龍領地", "type": "mainline", "danger": 9, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 0.6,
                    "gene_lock_gain": 18,
                    "gene_lock_stage_cap": "第二階 30%",
                    "time_cost_days": 8,
                    "base_reward": 4000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"D": 1.0, "C": 0.7, "B": 0.3}
                },
                "prerequisites": {"min_rank": "D"}
            },
            {
                "id": "alien",
                "name": "異形",
                "difficulty": "B",
                "description": "太空船上的完美生物獵殺",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "發現異形", "order": 1},
                        {"id": "node_2", "title": "啟動自毀程序", "order": 2},
                        {"id": "node_3", "title": "撤離到逃生艙", "order": 3},
                        {"id": "node_final", "title": "對抗異形女王", "order": 4, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "cargo_bay", "name": "貨艙", "type": "mainline", "danger": 7, "initial_status": "discovered"},
                    {"id": "bridge", "name": "指揮橋", "type": "mainline", "danger": 6, "initial_status": "undiscovered"},
                    {"id": "engine_room", "name": "引擎室", "type": "mainline", "danger": 9, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 0.7,
                    "gene_lock_gain": 20,
                    "gene_lock_stage_cap": "第二階 70%",
                    "time_cost_days": 10,
                    "base_reward": 6000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"C": 1.0, "B": 0.7, "A": 0.3}
                },
                "prerequisites": {"min_rank": "C"}
            },
            {
                "id": "ju",
                "name": "咒",
                "difficulty": "B",
                "description": "錄影帶的詛咒與殺人鬼的追殺",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "調查咒怨錄影帶", "order": 1},
                        {"id": "node_2", "title": "追蹤殺人鬼", "order": 2},
                        {"id": "node_final", "title": "打破詛咒循環", "order": 3, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "apartment", "name": "公寓", "type": "mainline", "danger": 7, "initial_status": "discovered"},
                    {"id": "forest", "name": "森林", "type": "mainline", "danger": 8, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 0.7,
                    "gene_lock_gain": 20,
                    "gene_lock_stage_cap": "第二階 70%",
                    "time_cost_days": 10,
                    "base_reward": 6000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"C": 1.0, "B": 0.7, "A": 0.3}
                },
                "prerequisites": {"min_rank": "C"}
            },
            {
                "id": "attack_on_titan",
                "name": "進擊的巨人",
                "difficulty": "B",
                "description": "對抗巨人的絕望戰爭",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "加入調查兵團", "order": 1},
                        {"id": "node_2", "title": "奪回瑪利亞之牆", "order": 2},
                        {"id": "node_3", "title": "擊敗女巨人", "order": 3},
                        {"id": "node_final", "title": "發現地下室的真相", "order": 4, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "wall_rose", "name": "羅塞之牆", "type": "mainline", "danger": 7, "initial_status": "discovered"},
                    {"id": "titan_forest", "name": "巨木森林", "type": "mainline", "danger": 8, "initial_status": "undiscovered"},
                    {"id": "wall_maria", "name": "瑪利亞之牆", "type": "mainline", "danger": 9, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 0.7,
                    "gene_lock_gain": 20,
                    "gene_lock_stage_cap": "第二階 70%",
                    "time_cost_days": 12,
                    "base_reward": 7000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"C": 1.0, "B": 0.7, "A": 0.3}
                },
                "prerequisites": {"min_rank": "C"}
            },
            {
                "id": "shushan",
                "name": "蜀山",
                "difficulty": "A",
                "description": "修真世界的劍仙爭鬥",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "拜入蜀山", "order": 1},
                        {"id": "node_2", "title": "習得御劍術", "order": 2},
                        {"id": "node_3", "title": "鎮壓妖魔", "order": 3},
                        {"id": "node_final", "title": "渡劫飛升", "order": 4, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "shushan_gate", "name": "蜀山山門", "type": "mainline", "danger": 8, "initial_status": "discovered"},
                    {"id": "sword_peak", "name": "劍峰", "type": "mainline", "danger": 9, "initial_status": "undiscovered"},
                    {"id": "demon_valley", "name": "魔谷", "type": "mainline", "danger": 10, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 0.8,
                    "gene_lock_gain": 22,
                    "gene_lock_stage_cap": "第三階 50%",
                    "time_cost_days": 15,
                    "base_reward": 10000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"B": 1.0, "A": 0.7, "S": 0.3}
                },
                "prerequisites": {"min_rank": "B"}
            },
            {
                "id": "jujutsu_kaisen",
                "name": "咒術迴戰",
                "difficulty": "A",
                "description": "咒靈橫行的現代東京",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "加入咒術高專", "order": 1},
                        {"id": "node_2", "title": "收集宿儺手指", "order": 2},
                        {"id": "node_3", "title": "對抗特級咒靈", "order": 3},
                        {"id": "node_final", "title": "渋谷事變", "order": 4, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "tokyo_jujutsu_high", "name": "東京咒術高專", "type": "mainline", "danger": 8, "initial_status": "discovered"},
                    {"id": "shibuya", "name": "澀谷", "type": "mainline", "danger": 10, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 0.8,
                    "gene_lock_gain": 22,
                    "gene_lock_stage_cap": "第三階 50%",
                    "time_cost_days": 15,
                    "base_reward": 10000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"B": 1.0, "A": 0.7, "S": 0.3}
                },
                "prerequisites": {"min_rank": "B"}
            },
            {
                "id": "naruto",
                "name": "火影忍者",
                "difficulty": "A",
                "description": "忍者世界的戰爭與羈絆",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "加入木葉", "order": 1},
                        {"id": "node_2", "title": "中忍考試", "order": 2},
                        {"id": "node_3", "title": "曉組織襲來", "order": 3},
                        {"id": "node_4", "title": "第四次忍界大戰", "order": 4},
                        {"id": "node_final", "title": "對抗輝夜", "order": 5, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "konoha", "name": "木葉村", "type": "mainline", "danger": 7, "initial_status": "discovered"},
                    {"id": "akatsuki_hideout", "name": "曉組織據點", "type": "mainline", "danger": 10, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 0.8,
                    "gene_lock_gain": 22,
                    "gene_lock_stage_cap": "第三階 50%",
                    "time_cost_days": 18,
                    "base_reward": 12000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"B": 1.0, "A": 0.7, "S": 0.3}
                },
                "prerequisites": {"min_rank": "B"}
            },
            {
                "id": "scp",
                "name": "SCP 基金會",
                "difficulty": "S",
                "description": "收容、控制、保護 — 應對異常威脅",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "加入基金會", "order": 1},
                        {"id": "node_2", "title": "應對收容失效", "order": 2},
                        {"id": "node_3", "title": "對抗GOC", "order": 3},
                        {"id": "node_final", "title": "阻止XK級情景", "order": 4, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "site_19", "name": "Site-19", "type": "mainline", "danger": 10, "initial_status": "discovered"},
                    {"id": "scp_682_chamber", "name": "SCP-682收容室", "type": "mainline", "danger": 12, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 1.0,
                    "gene_lock_gain": 25,
                    "gene_lock_stage_cap": "第四階 30%",
                    "time_cost_days": 21,
                    "base_reward": 18000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"A": 1.0, "S": 0.7, "SS": 0.3}
                },
                "prerequisites": {"min_rank": "A"}
            },
            {
                "id": "three_body",
                "name": "三體",
                "difficulty": "S",
                "description": "面對三體文明的降維打擊",
                "mainline": {
                    "nodes": [
                        {"id": "node_1", "title": "發現三體文明", "order": 1},
                        {"id": "node_2", "title": "面壁計劃", "order": 2},
                        {"id": "node_3", "title": "黑暗森林法則", "order": 3},
                        {"id": "node_4", "title": "二向箔降維", "order": 4},
                        {"id": "node_final", "title": "逃離太陽系", "order": 5, "is_final": True}
                    ]
                },
                "areas": [
                    {"id": "earth", "name": "地球", "type": "mainline", "danger": 9, "initial_status": "discovered"},
                    {"id": "trisolaris", "name": "三體星系", "type": "mainline", "danger": 12, "initial_status": "undiscovered"}
                ],
                "progression_rules": {
                    "rank_progress": 1.0,
                    "gene_lock_gain": 25,
                    "gene_lock_stage_cap": "第四階 30%",
                    "time_cost_days": 25,
                    "base_reward": 20000,
                    "mainline_multiplier": 1.5,
                    "exploration_multiplier": 1.3,
                    "difficulty_scaling": {"A": 1.0, "S": 0.7, "SS": 0.3}
                },
                "prerequisites": {"min_rank": "A"}
            }
        ]
    }

    _save_json(path, default_templates)
