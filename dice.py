"""命運走向系統 — d100 fate dice with attribute modifiers."""

import random
import re
from typing import Optional

# ── Attribute level → modifier mapping ──────────────────────────

_PHYSIQUE_TABLE: list[tuple[str, int]] = [
    # Tier 3 — superhuman / transcendent
    ("超級戰士", 10),
    ("超級士兵", 10),
    ("始祖", 10),
    ("超凡", 10),
    ("完美適應", 10),
    # Tier 2 — enhanced human
    ("強化人類", 3),
    ("人類極限", 3),
    ("永久提升", 3),
    ("大幅提升", 3),
    ("極大提升", 3),
    ("巔峰", 3),
    ("進一步增強", 3),
    # Tier 1 — slightly above average
    ("稍強", 1),
    ("微幅提升", 1),
    ("基礎優化", 1),
    # Tier 0
    ("普通", 0),
]

_SPIRIT_TABLE: list[tuple[str, int]] = [
    # Tier 3 — transcendent
    ("超強", 10),
    ("心靈鋼鐵", 10),
    ("神性", 10),
    ("昇華", 10),
    ("免疫", 10),
    # Tier 2 — strong
    ("強大", 5),
    ("永久提升", 5),
    ("大幅提升", 5),
    ("巔峰", 5),
    # Tier 1 — above average
    ("偏高", 1),
    ("中等偏上", 1),
    ("微幅提升", 1),
    # Tier 0
    ("普通", 0),
]

_GENE_LOCK_TABLE: list[tuple[str, int]] = [
    ("第四階", 30),
    ("四階", 30),
    ("第三階", 20),
    ("三階", 20),
    ("第二階", 10),
    ("二階", 10),
    ("第一階", 5),
    ("一階", 5),
    ("未開啟", 0),
]

# Strip parenthesized suffixes: "普通人類（稍強）" → "普通人類 稍強"
_PAREN_RE = re.compile(r"[（(]([^）)]*)[）)]")


def _lookup_modifier(raw: str, table: list[tuple[str, int]]) -> int:
    """Fuzzy-match an attribute string against a modifier table.

    Strategy: exact substring match → stripped-paren prefix match → 0.
    """
    if not raw:
        return 0
    # Flatten parens into space-separated text for matching
    flat = _PAREN_RE.sub(r" \1", raw).strip()
    for label, mod in table:
        if label in flat:
            return mod
    return 0


def _get_modifiers(state: dict) -> tuple[int, int, int]:
    """Extract (physique_mod, spirit_mod, gene_lock_mod) from character state."""
    physique = _lookup_modifier(str(state.get("physique", "")), _PHYSIQUE_TABLE)
    spirit = _lookup_modifier(str(state.get("spirit", "")), _SPIRIT_TABLE)
    gene_lock = _lookup_modifier(str(state.get("gene_lock", "")), _GENE_LOCK_TABLE)
    return physique, spirit, gene_lock


# ── Outcome labels ──────────────────────────────────────────────

_OUTCOMES = {
    "天命": "命運強烈眷顧，超乎預期的機緣或收穫。但好運未必沒有隱患——功高震主、樹大招風、樂極生悲皆有可能。",
    "順遂": "事情朝好的方向發展。但順境中也可能暗藏伏筆——過於順利是否意味著某處正在積累風險？",
    "平淡": "命運不偏不倚，一切按常理發展。結果取決於角色自身的能力和選擇。",
    "波折": "遇到阻礙或意外轉折。但塞翁失馬焉知非福——挫折可能帶來意外的發現、盟友、或成長契機。",
    "劫數": "重大考驗降臨，處境艱難。但危機往往是轉機的起點——絕境中的突破、因禍得福、置之死地而後生。",
}


BEGINNER_BONUS_TURNS = 10


def roll_fate(state: dict, cheat_modifier: int = 0,
              always_success: bool = False,
              turn_count: int = 0) -> dict:
    """Roll a d100 fate die with attribute modifiers.

    Args:
        state: Character state dict for attribute-based modifiers.
        cheat_modifier: Extra modifier from /gm dice command (金手指).
        always_success: When True, outcomes are always positive (金手指).
        turn_count: Number of player turns so far (1-based).
            Turns 1–10 get a linearly decaying beginner bonus.

    Returns a dict with all dice info for storage and display.
    """
    p_mod, s_mod, g_mod = _get_modifiers(state)
    attr_bonus = (p_mod + s_mod) // 2 + g_mod

    # Beginner bonus: +10 at turn 1, +9 at turn 2, ..., +1 at turn 10, 0 after
    beginner_bonus = max(0, BEGINNER_BONUS_TURNS + 1 - turn_count) if turn_count > 0 else 0

    raw = random.randint(1, 100)
    effective = raw + attr_bonus + cheat_modifier + beginner_bonus

    if always_success:
        # 金手指模式: 只出正面走向
        # 天命 30% | 順遂 50% | 平淡 20%
        if raw >= 71:
            outcome = "天命"
        elif raw >= 21:
            outcome = "順遂"
        else:
            outcome = "平淡"
    else:
        # 正常模式 — 命運走向
        if raw >= 96:
            outcome = "天命"
        elif raw <= 5:
            outcome = "劫數"
        elif effective >= 70:
            outcome = "順遂"
        elif effective >= 40:
            outcome = "平淡"
        else:
            outcome = "波折"

    result = {
        "raw": raw,
        "attr_bonus": attr_bonus,
        "physique_mod": p_mod,
        "spirit_mod": s_mod,
        "gene_lock_mod": g_mod,
        "effective": effective,
        "outcome": outcome,
    }
    if beginner_bonus:
        result["beginner_bonus"] = beginner_bonus
    if cheat_modifier:
        result["cheat_modifier"] = cheat_modifier
    if always_success:
        result["always_success"] = True
    return result


def format_dice_context(result: dict) -> str:
    """Format dice result as context block for the GM."""
    p = result["physique_mod"]
    s = result["spirit_mod"]
    g = result["gene_lock_mod"]
    bonus_sign = "+" if result["attr_bonus"] >= 0 else ""

    return (
        f"[命運走向]\n"
        f"本回合命運：**{result['outcome']}** — {_OUTCOMES[result['outcome']]}\n"
        f"（這不是行動的成敗判定，而是命運的走向暗示。"
        f"絕對不要在敘事中出現「命運骰」「判定」「骰面」等機制用語。\n"
        f"■ 核心原則：塞翁失馬，焉知非福\n"
        f"  - 命運走向影響的是「事態發展的趨勢」，不是「當前行動的成敗」\n"
        f"  - 順遂不代表沒有隱患，波折不代表沒有收穫\n"
        f"  - 用伏筆、意外發現、NPC反應、環境變化等方式體現命運走向\n"
        f"  - 確定性行為（兌換、購買、查詢）不受命運走向影響\n"
        f"■ 行動合理性影響結果：\n"
        f"  - 玩家描述越周全（利用環境、道具、策略、同伴配合），行動本身越可能成功\n"
        f"  - 玩家描述草率或不合理（越級硬拼、無準備挑戰強敵），即使命運順遂也不該輕鬆碾壓\n"
        f"  - 命運走向 + 行動合理性共同決定最終敘事，兩者獨立\n"
        f"■ 演繹自由度：\n"
        f"  - 你可以自由決定行動本身是否成功，命運走向影響的是連帶效應和後續發展\n"
        f"  - 例：波折 + 周全策略 → 策略成功但過程中遇到意外；順遂 + 魯莽行動 → 僥倖沒死但浪費了好運）"
    )
