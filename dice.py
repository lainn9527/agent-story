"""命運骰系統 — d100 fate dice with attribute modifiers."""

import random
import re
from typing import Optional

# ── Attribute level → modifier mapping ──────────────────────────

_PHYSIQUE_TABLE: list[tuple[str, int]] = [
    ("超級戰士", 10),
    ("強化人類", 3),
    ("稍強", 1),
    ("普通", 0),
]

_SPIRIT_TABLE: list[tuple[str, int]] = [
    ("超強", 10),
    ("強大", 5),
    ("偏高", 1),
    ("中等偏上", 1),
    ("普通", 0),
]

_GENE_LOCK_TABLE: list[tuple[str, int]] = [
    ("第四階", 30),
    ("第三階", 20),
    ("第二階", 10),
    ("第一階", 5),
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
    physique = _lookup_modifier(state.get("physique", ""), _PHYSIQUE_TABLE)
    spirit = _lookup_modifier(state.get("spirit", ""), _SPIRIT_TABLE)
    gene_lock = _lookup_modifier(state.get("gene_lock", ""), _GENE_LOCK_TABLE)
    return physique, spirit, gene_lock


# ── Outcome labels ──────────────────────────────────────────────

_OUTCOMES = {
    "大成功": "命運眷顧，超乎預期的完美結果",
    "成功":   "順利達成目標",
    "勉強成功": "險些失敗，但勉強達成，可能有代價或不完美",
    "失敗":   "未能達成目標，可能遭受挫折",
    "大失敗": "災難性的失敗，情況急轉直下",
    "嚴重失敗": "未能達成目標，並帶來額外的負面後果",
}


def roll_fate(state: dict, cheat_modifier: int = 0,
              always_success: bool = False) -> dict:
    """Roll a d100 fate die with attribute modifiers.

    Args:
        state: Character state dict for attribute-based modifiers.
        cheat_modifier: Extra modifier from /gm dice command (金手指).
        always_success: When True, outcomes are always positive (金手指).

    Returns a dict with all dice info for storage and display.
    """
    p_mod, s_mod, g_mod = _get_modifiers(state)
    attr_bonus = (p_mod + s_mod) // 2 + g_mod

    raw = random.randint(1, 100)
    effective = raw + attr_bonus + cheat_modifier

    if always_success:
        # 金手指模式: 只出正面結果
        # 大成功 30% | 成功 50% | 勉強成功 20%
        if raw >= 71:
            outcome = "大成功"
        elif raw >= 21:
            outcome = "成功"
        else:
            outcome = "勉強成功"
    else:
        # 正常模式
        if raw >= 96:
            outcome = "大成功"
        elif raw <= 5:
            outcome = "大失敗"
        elif effective >= 80:
            outcome = "成功"
        elif effective >= 50:
            outcome = "勉強成功"
        elif effective >= 30:
            outcome = "失敗"
        else:
            outcome = "嚴重失敗"

    result = {
        "raw": raw,
        "attr_bonus": attr_bonus,
        "physique_mod": p_mod,
        "spirit_mod": s_mod,
        "gene_lock_mod": g_mod,
        "effective": effective,
        "outcome": outcome,
    }
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
        f"[命運判定]\n"
        f"判定: **{result['outcome']}** — {_OUTCOMES[result['outcome']]}\n"
        f"（此為系統內部判定，請融入敘事中體現結果好壞，"
        f"但絕對不要在敘事中出現「命運骰」「判定結果」「骰面」等詞彙。"
        f"若玩家的行動不涉及需要判定的情境，可忽略。）"
    )
