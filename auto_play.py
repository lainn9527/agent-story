"""Auto-play script for 主神空間 RPG.

Two Claude instances work together:
  - GM: Uses call_claude_gm() with persistent session (same as normal gameplay)
  - Player AI: Uses call_oneshot() — stateless, receives context each turn

Generated content (lore, NPCs, events) is saved to a dedicated branch
and can later be merged into the main story.

Usage:
    python auto_play.py --character data/auto_play_characters/lin_hao.json
    python auto_play.py --max-turns 50 --turn-delay 5
    python auto_play.py --resume --branch-id auto_abc12345
"""

import argparse
import copy
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("auto_play")

# ---------------------------------------------------------------------------
# Imports from project modules
# ---------------------------------------------------------------------------
from app import (
    _load_json,
    _save_json,
    _load_tree,
    _save_tree,
    _story_dir,
    _branch_dir,
    _story_messages_path,
    _story_character_state_path,
    _story_npcs_path,
    _story_default_character_state_path,
    _blank_character_state,
    _load_character_state,
    _load_summary,
    _build_story_system_prompt,
    get_full_timeline,
    _build_augmented_message,
    _process_gm_response,
    _load_npcs,
    _build_npc_text,
    _find_state_at_index,
    _find_npcs_at_index,
    _load_branch_config,
    _save_branch_config,
    RECENT_MESSAGE_COUNT,
    _IMG_RE,
)
from llm_bridge import call_claude_gm, call_oneshot, set_provider, web_search
from npc_evolution import should_run_evolution, run_npc_evolution_async

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STOP_FILE = "auto_play.stop"
MAX_RETRIES_PER_TURN = 3


class GMError(Exception):
    """Raised when GM returns a system error (timeout, empty response, etc.)."""
    pass

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AutoPlayConfig:
    story_id: str = "story_original"
    parent_branch_id: str = "main"
    branch_point_index: int = 0
    blank: bool = True
    character: dict | None = None
    character_personality: str = "保持角色一致性，做出符合角色性格的選擇。"
    opening_message: str = "我剛到這裡，準備開始冒險。"
    max_turns: int = 200
    max_dungeons: int | None = None
    max_hub_turns: int = 10
    turn_delay: float = 3.0
    skip_images: bool = True
    resume: bool = False
    branch_id: str | None = None  # For resume mode
    provider: str | None = None   # Override LLM provider ("gemini" / "claude_cli")
    max_errors: int = 10          # Max consecutive errors before stopping
    web_search: bool = True       # Enable web search enrichment for lore/dungeons


# ---------------------------------------------------------------------------
# Run State
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    turn: int = 0
    phase: str = "hub"  # "hub" or "dungeon"
    dungeon_count: int = 0
    hub_turns: int = 0
    death_detected: bool = False
    consecutive_errors: int = 0
    started_at: str = ""
    last_turn_at: str = ""

    def to_dict(self) -> dict:
        return {
            "turn": self.turn,
            "phase": self.phase,
            "dungeon_count": self.dungeon_count,
            "hub_turns": self.hub_turns,
            "death_detected": self.death_detected,
            "consecutive_errors": self.consecutive_errors,
            "started_at": self.started_at,
            "last_turn_at": self.last_turn_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunState":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Character Generation
# ---------------------------------------------------------------------------

_CHAR_GEN_PROMPT = """\
為主神空間 RPG 生成一個隨機角色卡。角色固定設定：姓名 Eddy，男性。
其他所有設定請隨機產生，包括年齡、外貌、性格、專長、背景等。
請讓角色有趣且有特色，避免過於平凡的設定。

請嚴格按照以下 JSON 格式回覆，不要加任何其他文字：
{
  "personality": "給 AI 玩家的性格指導，描述這個角色會怎麼行動和說話，要行動導向（2-3句）",
  "opening_message": "角色的第一句話，用第一人稱簡短自我介紹後直接開始行動，不要問問題（1-2句）",
  "character_state": {
    "name": "Eddy",
    "gene_lock": "未開啟",
    "physique": "描述體質（如：退伍軍人/運動員/普通人等）",
    "spirit": "描述精神力（如：普通人類/敏銳直覺等）",
    "reward_points": 0,
    "current_status": "新人，剛進入主神空間",
    "inventory": [],
    "completed_missions": [],
    "relationships": {}
  },
  "summary": "一句話角色概述"
}
"""


def generate_random_character(story_id: str) -> dict:
    """Use LLM to generate a random character card and save it.

    Returns the character data dict (same format as lin_hao.json).
    """
    log.info("Generating random character via LLM...")
    raw = call_oneshot(_CHAR_GEN_PROMPT)

    # Extract JSON from response (handle markdown code blocks)
    text = raw.strip()
    if text.startswith("```"):
        # Remove ```json ... ```
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        char_data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Failed to parse character JSON, using fallback")
        char_data = {
            "personality": "冷靜果決，善於觀察和分析，帶點玩世不恭的表演慾。",
            "opening_message": "我叫 Eddy，剛被丟進這個莫名其妙的地方。看起來得靠自己活下去了。",
            "character_state": {
                "name": "Eddy",
                "gene_lock": "未開啟",
                "physique": "普通人類",
                "spirit": "普通人類",
                "reward_points": 0,
                "current_status": "新人，剛進入主神空間",
                "inventory": [],
                "completed_missions": [],
                "relationships": {},
            },
        }

    # Save to auto_play_characters/
    char_dir = os.path.join("data", "auto_play_characters")
    os.makedirs(char_dir, exist_ok=True)
    filename = f"eddy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = os.path.join(char_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(char_data, f, ensure_ascii=False, indent=2)
    log.info("Character saved: %s", filepath)

    summary = char_data.get("summary", char_data["character_state"].get("physique", ""))
    log.info("Character: Eddy — %s", summary)

    return char_data


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup(config: AutoPlayConfig) -> tuple[str, str]:
    """Create a new branch for auto-play and initialize state.

    Returns (story_id, branch_id).
    """
    story_id = config.story_id
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    branch_id = f"auto_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    if config.blank:
        # Blank branch: fresh start with blank placeholder state, empty NPCs
        if config.character:
            state = copy.deepcopy(config.character)
        else:
            state = _blank_character_state(story_id)
        npcs = []
    else:
        # Fork character state from parent at branch_point_index
        if config.character:
            state = copy.deepcopy(config.character)
        else:
            state = _find_state_at_index(
                story_id, config.parent_branch_id, config.branch_point_index
            )
        # Fork NPCs from parent
        npcs = _find_npcs_at_index(
            story_id, config.parent_branch_id, config.branch_point_index
        )

    # Register branch in timeline_tree
    branch_meta = {
        "id": branch_id,
        "name": f"Auto-Play {datetime.now().strftime('%m/%d %H:%M')}",
        "parent_branch_id": config.parent_branch_id,
        "branch_point_index": -1 if config.blank else config.branch_point_index,
        "created_at": now,
        "session_id": None,
    }
    if config.blank:
        branch_meta["blank"] = True
    branches[branch_id] = branch_meta
    tree["branches"] = branches
    _save_tree(story_id, tree)

    # Save character state
    _save_json(_story_character_state_path(story_id, branch_id), state)

    # Save NPCs
    _save_json(_story_npcs_path(story_id, branch_id), npcs)

    # Copy branch config from parent
    parent_config = _load_branch_config(story_id, config.parent_branch_id)
    if parent_config:
        _save_branch_config(story_id, branch_id, parent_config)

    # Initialize empty messages
    _save_json(_story_messages_path(story_id, branch_id), [])

    log.info("Branch created: %s (parent=%s, fork_at=%d)",
             branch_id, config.parent_branch_id, config.branch_point_index)
    log.info("Character: %s", state.get("name", "?"))

    return story_id, branch_id


# ---------------------------------------------------------------------------
# Web Search Enrichment (uses Gemini Google Search grounding)
# ---------------------------------------------------------------------------

# Search every N turns to avoid excessive API calls
_WEB_SEARCH_INTERVAL = 3
_web_search_turn_counter = 0


def _web_search_enrichment(player_text: str, gm_last: str, state: RunState) -> str:
    """Search the web for relevant lore/dungeon info to enrich GM context.

    Returns a formatted context block or empty string.
    """
    global _web_search_turn_counter
    _web_search_turn_counter += 1

    # Only search every N turns
    if _web_search_turn_counter % _WEB_SEARCH_INTERVAL != 1:
        return ""

    # Build search query based on current game context
    # Combine recent player action + GM response for topic extraction
    context_snippet = f"{gm_last[:300]}\n{player_text[:200]}"

    query = (
        f"根據以下 RPG 遊戲片段，搜尋「無限恐怖」或「諸天無限流」相關的設定資料"
        f"（體系、副本世界觀、能力系統、戰鬥機制等）。"
        f"如果提到特定副本（如咒怨、生化危機、異形等），搜尋該作品的關鍵設定。"
        f"如果涉及修真、鬥氣、魔法等體系，搜尋其等級和運作規則。"
        f"也可以推薦適合作為新副本的恐怖/科幻/奇幻作品世界觀。\n\n"
        f"遊戲片段：\n{context_snippet}\n\n"
        f"請用繁體中文，提供 3-5 條最相關的設定資訊，每條 1-2 句話。"
    )

    result = web_search(query)
    if not result:
        return ""

    log.info("    web_search_enrichment: got %d chars", len(result))
    return f"\n[網路搜尋參考資料]\n{result}\n"


# ---------------------------------------------------------------------------
# Execute Turn (replicates /api/send pipeline)
# ---------------------------------------------------------------------------


def execute_turn(
    story_id: str, branch_id: str, player_text: str,
    skip_images: bool = True, web_search_context: str = "",
) -> str:
    """Execute one turn: save player msg, call GM, process tags, save GM msg.

    Returns the GM response text (cleaned).
    """
    tree = _load_tree(story_id)
    branch = tree.get("branches", {}).get(branch_id)
    if not branch:
        raise ValueError(f"Branch {branch_id} not found")

    # 1. Save player message
    delta_path = _story_messages_path(story_id, branch_id)
    delta_msgs = _load_json(delta_path, [])
    full_timeline = get_full_timeline(story_id, branch_id)

    player_msg = {
        "role": "user",
        "content": player_text,
        "index": len(full_timeline),
    }
    delta_msgs.append(player_msg)
    _save_json(delta_path, delta_msgs)
    full_timeline.append(player_msg)

    # 2. Build system prompt
    state = _load_character_state(story_id, branch_id)
    summary = _load_summary(story_id)
    state_text = json.dumps(state, ensure_ascii=False, indent=2)
    system_prompt = _build_story_system_prompt(story_id, state_text, summary, branch_id)

    # 3. Gather recent context
    recent = full_timeline[-RECENT_MESSAGE_COUNT:]

    # 4. Augment player message with lore/events/NPC activities/dice
    augmented_text, dice_result = _build_augmented_message(
        story_id, branch_id, player_text, state
    )
    if dice_result:
        player_msg["dice"] = dice_result
        _save_json(delta_path, delta_msgs)

    # 4b. Inject web search context if provided
    if web_search_context:
        augmented_text = web_search_context + "\n" + augmented_text

    # 5. Call GM with branch session
    session_id = branch.get("session_id")
    gm_response, new_session_id = call_claude_gm(
        augmented_text, system_prompt, recent, session_id=session_id
    )

    # 5b. If GM returned a system error, rollback player message and raise
    if gm_response.startswith("【系統錯誤】"):
        delta_msgs.pop()  # remove the player message we just appended
        _save_json(delta_path, delta_msgs)
        raise GMError(gm_response)

    # 6. Update session_id if changed
    if new_session_id and new_session_id != session_id:
        tree = _load_tree(story_id)  # Reload in case of concurrent writes
        tree["branches"][branch_id]["session_id"] = new_session_id
        _save_tree(story_id, tree)

    # 7. Strip IMG tags if skip_images
    if skip_images:
        gm_response = _IMG_RE.sub("", gm_response).strip()

    # 8. Extract all tags (STATE, LORE, NPC, EVENT, IMG)
    gm_msg_index = len(full_timeline)
    gm_response, image_info, snapshots = _process_gm_response(
        gm_response, story_id, branch_id, gm_msg_index
    )

    # 9. Save GM message
    gm_msg = {
        "role": "gm",
        "content": gm_response,
        "index": gm_msg_index,
    }
    if image_info:
        gm_msg["image"] = image_info
    gm_msg.update(snapshots)
    delta_msgs.append(gm_msg)
    _save_json(delta_path, delta_msgs)

    # 10. Trigger NPC evolution if due
    turn_count = sum(1 for m in full_timeline if m.get("role") == "user")
    if _load_npcs(story_id, branch_id) and should_run_evolution(
        story_id, branch_id, turn_count
    ):
        npc_text = _build_npc_text(story_id, branch_id)
        recent_text = "\n".join(
            m.get("content", "")[:200] for m in full_timeline[-6:]
        )
        run_npc_evolution_async(
            story_id, branch_id, turn_count, npc_text, recent_text
        )

    return gm_response


# ---------------------------------------------------------------------------
# Player AI
# ---------------------------------------------------------------------------

_PLAYER_SYSTEM_PROMPT = """\
你是主神空間 RPG 的自動玩家 AI。你扮演一名輪迴者。

## 你的性格
{personality}

## 行動原則
1. **直接採取行動**——移動、戰鬥、交涉、探索，推動故事前進
2. 副本中以生存和完成任務為最高優先，遇到危險立刻反應
3. 主神空間中積極兌換裝備、與NPC互動、準備下一次副本
4. 做出合理但有趣的選擇，偶爾冒險
5. 像真正的玩家一樣行動——有情緒、有判斷、有策略
6. 回覆 50-150 字，用第一人稱
7. **主動探索世界設定**——遇到新的體系、規則、地點、NPC時，花時間了解細節（詢問運作原理、嘗試使用、觀察環境描述）
8. 每 3-5 回合至少做一次探索性行動（研究體系、詢問 NPC 背景、調查環境線索等）
9. **明確詢問具體規則**——例如「基因鎖二階的具體條件是什麼？」「這個副本的世界觀背景是什麼？」「商城的兌換規則怎麼運作？」——用提問引出詳細設定

## 當前角色狀態
{character_state}

## 階段提示
{phase_hint}
"""

_PLAYER_TURN_PROMPT = """\
最近的故事進展：
{recent_context}

GM 最後的回覆：
{gm_last}

請輸出你的下一步行動（50-150字，第一人稱）。\
"""


def _get_phase_hint(state: RunState, config: AutoPlayConfig) -> str:
    """Return phase-specific guidance for the Player AI."""
    if state.phase == "dungeon":
        return (
            "你正在副本任務中。優先存活和完成任務目標。"
            "觀察環境、與隊友合作、對威脅保持警惕。"
            "主動尋找支線任務和隱藏事件——探索不尋常的地點、調查可疑線索、與NPC深入對話。"
            "進入新副本時，花一回合詢問這個世界的背景設定和規則。"
        )
    # Hub phase
    if state.hub_turns >= config.max_hub_turns:
        return (
            "你已經在主神空間待了很久。是時候請求下一個副本任務了。"
            "向主神表示你準備好接受新任務。"
        )
    return (
        "你在主神空間。可以兌換裝備、訓練、與NPC互動、收集情報。"
        "主動探索各種體系（基因鎖、血統改造、修真、魔法等）的細節和規則，向NPC請教。"
        "具體提問：「這個體系具體怎麼運作？」「有什麼限制和代價？」「等級劃分是什麼？」"
        "準備好了就向主神請求下一個副本任務。"
    )


def generate_player_action(
    story_id: str, branch_id: str, state: RunState, config: AutoPlayConfig
) -> str:
    """Use Player AI to generate the next player action."""
    # Build character state text
    char_state = _load_character_state(story_id, branch_id)
    state_text = json.dumps(char_state, ensure_ascii=False, indent=2)

    # Build system prompt
    phase_hint = _get_phase_hint(state, config)
    system_prompt = _PLAYER_SYSTEM_PROMPT.format(
        personality=config.character_personality,
        character_state=state_text,
        phase_hint=phase_hint,
    )

    # Build turn prompt with recent context
    full_timeline = get_full_timeline(story_id, branch_id)
    recent = full_timeline[-6:]
    context_lines = []
    for msg in recent:
        prefix = "【玩家】" if msg.get("role") == "user" else "【GM】"
        content = msg.get("content", "")
        if len(content) > 300:
            content = content[:300] + "..."
        context_lines.append(f"{prefix}\n{content}")

    gm_last = ""
    for msg in reversed(full_timeline):
        if msg.get("role") == "gm":
            gm_last = msg.get("content", "")
            if len(gm_last) > 600:
                gm_last = gm_last[:600] + "..."
            break

    turn_prompt = _PLAYER_TURN_PROMPT.format(
        recent_context="\n\n".join(context_lines) if context_lines else "(開局)",
        gm_last=gm_last or "(尚無GM回覆)",
    )

    response = call_oneshot(turn_prompt, system_prompt=system_prompt)
    if not response:
        return "我觀察周圍的環境，思考下一步該怎麼做。"
    return response.strip()


# ---------------------------------------------------------------------------
# Phase & Death Detection
# ---------------------------------------------------------------------------

_DEATH_STATUS_KEYWORD = "end"

_DUNGEON_START_PATTERNS = re.compile(
    r"【主神提示：.*?任務】|傳送開始|副本.*?開啟|進入副本|"
    r"主神.*?傳送|白光.*?吞噬|場景.*?轉換",
    re.IGNORECASE,
)

_DUNGEON_END_PATTERNS = re.compile(
    r"任務完成|返回主神空間|任務評級|副本.*?結束|"
    r"回到.*?主神空間|傳送回.*?主神|主神.*?評分",
    re.IGNORECASE,
)

_HUB_PATTERNS = re.compile(
    r"兌換大廳|主神空間|訓練場|休息區|商城",
    re.IGNORECASE,
)


def analyze_response(
    gm_response: str, story_id: str, branch_id: str
) -> dict:
    """Analyze GM response for phase transitions and death.

    Returns dict with keys: death, dungeon_start, dungeon_end, hub_detected.
    """
    result = {
        "death": False,
        "dungeon_start": False,
        "dungeon_end": False,
        "hub_detected": False,
    }

    # Check death via character state (GM sets current_status to "end")
    state = _load_character_state(story_id, branch_id)
    status = state.get("current_status", "").strip().lower()
    if status == _DEATH_STATUS_KEYWORD:
        result["death"] = True

    # Check phase transitions
    if _DUNGEON_START_PATTERNS.search(gm_response):
        result["dungeon_start"] = True
    if _DUNGEON_END_PATTERNS.search(gm_response):
        result["dungeon_end"] = True
    if _HUB_PATTERNS.search(gm_response):
        result["hub_detected"] = True

    return result


def update_phase(state: RunState, analysis: dict):
    """Update run state phase based on analysis results."""
    if analysis["death"]:
        state.death_detected = True
        return

    if state.phase == "hub":
        if analysis["dungeon_start"]:
            state.phase = "dungeon"
            state.dungeon_count += 1
            state.hub_turns = 0
            log.info(">>> Phase: hub -> dungeon (dungeon #%d)", state.dungeon_count)
        else:
            state.hub_turns += 1
    elif state.phase == "dungeon":
        if analysis["dungeon_end"] or (
            analysis["hub_detected"] and not analysis["dungeon_start"]
        ):
            state.phase = "hub"
            state.hub_turns = 0
            log.info(">>> Phase: dungeon -> hub")


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


def should_stop(state: RunState, config: AutoPlayConfig) -> bool:
    """Check if the auto-play loop should stop."""
    if state.death_detected:
        log.info("STOP: Character death detected")
        return True
    if state.turn >= config.max_turns:
        log.info("STOP: Max turns reached (%d)", config.max_turns)
        return True
    if config.max_dungeons and state.dungeon_count >= config.max_dungeons:
        log.info("STOP: Max dungeons reached (%d)", config.max_dungeons)
        return True
    if os.path.exists(STOP_FILE):
        log.info("STOP: Stop file detected (%s)", STOP_FILE)
        return True
    if state.consecutive_errors >= config.max_errors:
        log.info("STOP: Too many consecutive errors (%d >= %d)", state.consecutive_errors, config.max_errors)
        return True
    return False


# ---------------------------------------------------------------------------
# State Persistence
# ---------------------------------------------------------------------------


def _state_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "auto_play_state.json")


def _transcript_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "auto_play_transcript.md")


def save_run_state(story_id: str, branch_id: str, state: RunState):
    _save_json(_state_path(story_id, branch_id), state.to_dict())


def load_run_state(story_id: str, branch_id: str) -> RunState | None:
    data = _load_json(_state_path(story_id, branch_id), None)
    if data is None:
        return None
    return RunState.from_dict(data)


# ---------------------------------------------------------------------------
# Logging / Transcript
# ---------------------------------------------------------------------------


def log_turn(
    story_id: str,
    branch_id: str,
    state: RunState,
    player_text: str,
    gm_response: str,
):
    """Log turn to console and append to transcript file."""
    # Console summary
    p_preview = player_text[:80].replace("\n", " ")
    g_preview = gm_response[:80].replace("\n", " ")
    print(
        f"\n{'='*60}\n"
        f"Turn {state.turn} | Phase: {state.phase} | Dungeon #{state.dungeon_count}\n"
        f"{'─'*60}\n"
        f"Player: {p_preview}{'...' if len(player_text) > 80 else ''}\n"
        f"{'─'*60}\n"
        f"GM:     {g_preview}{'...' if len(gm_response) > 80 else ''}\n"
        f"{'='*60}"
    )

    # Transcript file
    path = _transcript_path(story_id, branch_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## Turn {state.turn} [{state.phase}]\n\n")
        f.write(f"**Player:**\n{player_text}\n\n")
        f.write(f"**GM:**\n{gm_response}\n\n")
        f.write("---\n")


def print_summary(state: RunState, story_id: str, branch_id: str):
    """Print final summary when auto-play ends."""
    print(
        f"\n{'#'*60}\n"
        f"  AUTO-PLAY COMPLETE\n"
        f"{'#'*60}\n"
        f"  Turns played:  {state.turn}\n"
        f"  Dungeons:      {state.dungeon_count}\n"
        f"  Final phase:   {state.phase}\n"
        f"  Death:         {'Yes' if state.death_detected else 'No'}\n"
        f"  Started:       {state.started_at}\n"
        f"  Ended:         {state.last_turn_at}\n"
        f"  Branch:        {branch_id}\n"
        f"  Transcript:    {_transcript_path(story_id, branch_id)}\n"
        f"{'#'*60}\n"
    )


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------


def auto_play(config: AutoPlayConfig):
    """Run the auto-play loop."""
    # Apply provider override for this process
    if config.provider:
        set_provider(config.provider)

    # Setup or resume
    if config.resume and config.branch_id:
        story_id = config.story_id
        branch_id = config.branch_id
        state = load_run_state(story_id, branch_id)
        if state is None:
            log.error("No saved state found for branch %s", branch_id)
            sys.exit(1)
        # Clear stale death flag (detection logic may have changed)
        state.death_detected = False
        log.info("Resuming from turn %d on branch %s", state.turn, branch_id)
    else:
        story_id, branch_id = setup(config)
        state = RunState(
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        # Write transcript header
        with open(_transcript_path(story_id, branch_id), "w", encoding="utf-8") as f:
            f.write(f"# Auto-Play Transcript\n\n")
            f.write(f"- Story: {story_id}\n")
            f.write(f"- Branch: {branch_id}\n")
            f.write(f"- Started: {state.started_at}\n\n")
            f.write("---\n")

    log.info("Auto-play started: story=%s branch=%s", story_id, branch_id)

    # Clean up stop file if it exists from previous run
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)

    while not should_stop(state, config):
        try:
            # A. Generate player action
            if state.turn == 0 and not config.resume:
                player_text = config.opening_message
            else:
                player_text = generate_player_action(
                    story_id, branch_id, state, config
                )

            # A2. Web search enrichment (every few turns)
            ws_context = ""
            if config.web_search:
                # Get last GM message for context
                full_tl = get_full_timeline(story_id, branch_id)
                gm_msgs = [m for m in full_tl if m.get("role") == "gm"]
                gm_last = gm_msgs[-1]["content"][:500] if gm_msgs else ""
                ws_context = _web_search_enrichment(player_text, gm_last, state)

            # B. Execute one turn (with retry on GM error)
            gm_response = None
            for attempt in range(1, MAX_RETRIES_PER_TURN + 1):
                try:
                    gm_response = execute_turn(
                        story_id, branch_id, player_text, config.skip_images,
                        web_search_context=ws_context,
                    )
                    break  # success
                except GMError as e:
                    log.warning(
                        "Turn %d attempt %d/%d failed: %s",
                        state.turn, attempt, MAX_RETRIES_PER_TURN, e,
                    )
                    if attempt < MAX_RETRIES_PER_TURN:
                        backoff = config.turn_delay * (2 ** (attempt - 1))
                        log.info("Retrying in %.1fs...", backoff)
                        time.sleep(backoff)

            if gm_response is None:
                # All retries exhausted
                state.consecutive_errors += 1
                log.error(
                    "Turn %d failed after %d retries (consecutive: %d)",
                    state.turn, MAX_RETRIES_PER_TURN, state.consecutive_errors,
                )
                state.last_turn_at = datetime.now(timezone.utc).isoformat()
                save_run_state(story_id, branch_id, state)
                if state.consecutive_errors >= config.max_errors:
                    break
                # Exponential backoff on consecutive errors
                backoff = config.turn_delay * (2 ** min(state.consecutive_errors, 6))
                log.info("Waiting %.1fs before next attempt (consecutive errors: %d)", backoff, state.consecutive_errors)
                time.sleep(backoff)
                continue

            # C. Turn succeeded — reset consecutive errors
            state.consecutive_errors = 0

            # D. Analyze & update phase
            analysis = analyze_response(gm_response, story_id, branch_id)
            update_phase(state, analysis)

            # E. Log
            log_turn(story_id, branch_id, state, player_text, gm_response)

            # F. Save state
            state.last_turn_at = datetime.now(timezone.utc).isoformat()
            save_run_state(story_id, branch_id, state)

            state.turn += 1
            time.sleep(config.turn_delay)

        except KeyboardInterrupt:
            log.info("Interrupted by user (Ctrl+C)")
            state.last_turn_at = datetime.now(timezone.utc).isoformat()
            save_run_state(story_id, branch_id, state)
            break
        except Exception as e:
            state.consecutive_errors += 1
            log.exception("Error on turn %d: %s", state.turn, e)
            state.last_turn_at = datetime.now(timezone.utc).isoformat()
            save_run_state(story_id, branch_id, state)
            if state.consecutive_errors >= config.max_errors:
                break
            backoff = config.turn_delay * (2 ** min(state.consecutive_errors, 6))
            log.info("Waiting %.1fs before next attempt (consecutive errors: %d)", backoff, state.consecutive_errors)
            time.sleep(backoff)

    print_summary(state, story_id, branch_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> AutoPlayConfig:
    parser = argparse.ArgumentParser(
        description="Auto-play 主神空間 RPG with AI player",
    )
    parser.add_argument(
        "--story-id", default="story_original",
        help="Story ID to use (default: story_original)",
    )
    parser.add_argument(
        "--parent-branch", default="main",
        help="Parent branch to fork from (default: main)",
    )
    parser.add_argument(
        "--branch-point", type=int, default=0,
        help="Message index to fork at (default: 0 = fresh start)",
    )
    parser.add_argument(
        "--no-blank", action="store_true",
        help="Fork from parent branch instead of creating a blank branch (default: blank)",
    )
    parser.add_argument(
        "--character", type=str, default=None,
        help="Path to character JSON file",
    )
    parser.add_argument(
        "--personality", type=str, default=None,
        help="Player AI personality description",
    )
    parser.add_argument(
        "--opening", type=str, default=None,
        help="Opening message for the first turn",
    )
    parser.add_argument(
        "--max-turns", type=int, default=200,
        help="Maximum number of turns (default: 200)",
    )
    parser.add_argument(
        "--max-dungeons", type=int, default=None,
        help="Maximum number of dungeons (default: unlimited)",
    )
    parser.add_argument(
        "--max-hub-turns", type=int, default=10,
        help="Hub turns before nudging next dungeon (default: 10)",
    )
    parser.add_argument(
        "--turn-delay", type=float, default=3.0,
        help="Seconds between turns (default: 3.0)",
    )
    parser.add_argument(
        "--with-images", action="store_true",
        help="Enable image generation (default: skip images)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume a previous auto-play run",
    )
    parser.add_argument(
        "--branch-id", type=str, default=None,
        help="Branch ID to resume (required with --resume)",
    )
    parser.add_argument(
        "--provider", type=str, default=None, choices=["gemini", "claude_cli"],
        help="Override LLM provider (default: use llm_config.json)",
    )
    parser.add_argument(
        "--max-errors", type=int, default=10,
        help="Max consecutive errors before stopping (default: 10)",
    )
    parser.add_argument(
        "--no-web-search", action="store_true",
        help="Disable web search enrichment (default: enabled)",
    )

    args = parser.parse_args()

    config = AutoPlayConfig(
        story_id=args.story_id,
        parent_branch_id=args.parent_branch,
        branch_point_index=args.branch_point,
        blank=not args.no_blank,
        max_turns=args.max_turns,
        max_dungeons=args.max_dungeons,
        max_hub_turns=args.max_hub_turns,
        turn_delay=args.turn_delay,
        skip_images=not args.with_images,
        resume=args.resume,
        branch_id=args.branch_id,
        provider=args.provider,
        max_errors=args.max_errors,
        web_search=not args.no_web_search,
    )

    # Load character from file
    if args.character:
        if not os.path.exists(args.character):
            log.error("Character file not found: %s", args.character)
            sys.exit(1)
        with open(args.character, "r", encoding="utf-8") as f:
            char_data = json.load(f)
        # Support both flat state and wrapped format
        if "character_state" in char_data:
            config.character = char_data["character_state"]
            if "personality" in char_data:
                config.character_personality = char_data["personality"]
            if "opening_message" in char_data:
                config.opening_message = char_data["opening_message"]
        else:
            config.character = char_data

    if args.personality:
        config.character_personality = args.personality
    if args.opening:
        config.opening_message = args.opening

    # Generate random character if none specified
    if not args.character and not config.resume:
        char_data = generate_random_character(config.story_id)
        config.character = char_data["character_state"]
        if "personality" in char_data:
            config.character_personality = char_data["personality"]
        if "opening_message" in char_data:
            config.opening_message = char_data["opening_message"]

    # Validate resume mode
    if config.resume and not config.branch_id:
        log.error("--branch-id is required with --resume")
        sys.exit(1)

    return config


if __name__ == "__main__":
    config = parse_args()
    auto_play(config)
