"""Player↔NPC relationship tiers — the affinity ladder (kernel; cross-world).

性格/口吻 是穩定錨（不變）；關係是動態軸（會變）。關係是『判斷』不是『算術』——所以用
**具名階梯**（GM 設定 tier，引擎驗證/存放/比較），不用 scalar：避免讓 LLM 做加減（＝命運
點數重複扣那類 bug），且設定式天生冪等（設兩次「信任」還是信任）。

Value 形狀：`{"tier": <階梯之一>, "note": "<原因/現況>"}`，向後相容舊的純字串（視為只有
note、無 tier）。所有讀取都經 `normalize()`，所以新舊格式都安全。
"""

# 弱 → 強。"陌生" 是中性基準（剛認識前）；負向在前，正向在後。
TIERS = ["敵對", "戒備", "陌生", "相識", "友好", "信任", "摯友"]
_INDEX = {t: i for i, t in enumerate(TIERS)}
DEFAULT_TIER = "陌生"


def normalize(value) -> dict:
    """str | {tier,note} | None → {tier, note} (both strings, may be empty)."""
    if isinstance(value, dict):
        return {
            "tier": str(value.get("tier") or "").strip(),
            "note": str(value.get("note") or value.get("desc") or "").strip(),
        }
    return {"tier": "", "note": str(value or "").strip()}


def tier_index(tier: str) -> int:
    return _INDEX.get(str(tier or "").strip(), -1)


def is_valid_tier(tier: str) -> bool:
    return str(tier or "").strip() in _INDEX


def display(value) -> str:
    """For grounding/UI: 'tier（note）' / 'tier' / 'note'."""
    v = normalize(value)
    if v["tier"] and v["note"]:
        return f"{v['tier']}（{v['note']}）"
    return v["tier"] or v["note"]


def meets(value, min_tier: str) -> bool:
    """Does the relationship reach at least `min_tier`? Unknown min_tier → no gate."""
    if not is_valid_tier(min_tier):
        return True
    return tier_index(normalize(value).get("tier")) >= tier_index(min_tier)


def ladder_str() -> str:
    return " < ".join(TIERS)


__all__ = ["TIERS", "DEFAULT_TIER", "normalize", "tier_index",
           "is_valid_tier", "display", "meets", "ladder_str"]
