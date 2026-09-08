"""Prompt assembly for Card Mode: contracts, persona, and focused 4-item grounding."""

from __future__ import annotations

from datetime import datetime
from story_core.card_mode.contract import ROLEPLAY_CONTRACT
from story_core.gm_v2 import relationship as _rel

_WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def build_card_system_prompt(
    card: dict,
    state: dict,
    npc: dict,
    clock_dt: datetime,
    last_weather: str = "",
    recap_text: str = "",
) -> str:
    """Assemble the complete Card Mode system prompt.

    Contains 4 parts:
    [1] Roleplay Contract
    [2] Card Persona & Setting Details
    [3] Grounding (ONLY 4 items: Relationship, Long-term memory, Clock/Scene, Inventory)
    [4] Output Format specification
    """
    sections = [ROLEPLAY_CONTRACT.strip()]

    # [2] Card Persona & Setting
    persona = card.get("persona", {})
    setting = card.get("setting", {})
    player = card.get("player", {})

    p_lines = [
        "# 角色與情境設定",
        f"**角色姓名**：{card.get('display_name', '')}",
        f"**角色身份**：{persona.get('identity', '')}（{persona.get('age_note', '')}）",
        f"**外觀特徵**：{persona.get('appearance', '')}",
    ]

    body_details = persona.get("body_details") or []
    if body_details:
        p_lines.append("**身體細節**：" + "、".join(body_details))

    fetishes = persona.get("fetishes") or []
    if fetishes:
        p_lines.append("**性癖與偏好情境（Fetishes）**：" + "、".join(fetishes))

    sensitive_spots = persona.get("sensitive_spots") or []
    if sensitive_spots:
        p_lines.append("**生理敏感點與弱點（Sensitive Spots）**：" + "、".join(sensitive_spots))

    p_lines.append(f"**性格本質**：{persona.get('personality', '')}")
    p_lines.append(f"**說話風格**：{persona.get('speech_style', '')}")

    tics = persona.get("verbal_tics") or []
    if tics:
        p_lines.append("**慣用口癖**：" + "、".join(tics))

    mannerisms = persona.get("mannerisms") or []
    if mannerisms:
        p_lines.append("**習慣動作**：" + "、".join(mannerisms))

    desires = persona.get("desires") or []
    if desires:
        p_lines.append("**內心渴望**：" + "、".join(desires))

    fears = persona.get("fears") or []
    if fears:
        p_lines.append("**恐懼弱點**：" + "、".join(fears))

    tells = persona.get("tells") or []
    if tells:
        p_lines.append("**情緒外顯（Tells）**：" + "、".join(tells))

    boundaries = persona.get("boundaries") or []
    if boundaries:
        p_lines.append("**行為底線**：" + "、".join(boundaries))

    samples = persona.get("sample_lines") or []
    if samples:
        p_lines.append("**範例台詞**：\n" + "\n".join(f"- {s}" for s in samples))

    p_lines.append("")
    p_lines.append(f"**玩家身份**：{player.get('role', '')}（處境：{player.get('premise', '')}，敘事人稱：{player.get('address', '你')}）")
    p_lines.append(f"**舞台地點**：{setting.get('place', '')}（時代：{setting.get('era', '')}）")

    ambience = setting.get("ambience") or []
    if ambience:
        p_lines.append("**環境氛圍池**：" + "、".join(ambience))

    sections.append("\n".join(p_lines))

    # [3] Grounding (Strictly 4 items)
    g_lines = ["# 當前狀態（Grounding）", ""]

    # 3.1 關係
    rel_display = _rel.display(npc.get("relationship"))
    g_lines.append("## 與你的關係")
    g_lines.append(rel_display or "陌生")
    g_lines.append("")

    # 3.2 記憶
    g_lines.append("## 她記得的事（長期）")
    memories = (npc.get("memory") or [])[-8:]
    if memories:
        for m in memories:
            g_lines.append(f"- {m}")
    else:
        g_lines.append("（尚無重要長期記憶）")

    if recap_text and recap_text.strip():
        g_lines.append(f"\n【前情提要回顧】\n{recap_text.strip()}")
    g_lines.append("")

    # 3.3 現在 (時鐘/地點/天氣)
    weekday_str = _WEEKDAYS[clock_dt.weekday()]
    time_str = f"{clock_dt.year}/{clock_dt.month}/{clock_dt.day}（{weekday_str}）{clock_dt.strftime('%H:%M')}"
    place_str = setting.get("place", "")
    weather_str = last_weather or (setting.get("weather_pool", ["陰"])[0] if setting.get("weather_pool") else "陰")

    g_lines.append("## 現在")
    g_lines.append(f"{time_str} ｜ {place_str} ｜ {weather_str}")
    g_lines.append("")

    # 3.4 隨身物
    g_lines.append("## 你身上有")
    inventory = state.get("inventory") or {}
    if inventory:
        inv_items = []
        for item, note in inventory.items():
            if str(note).strip():
                inv_items.append(f"{item}（{note}）")
            else:
                inv_items.append(item)
        g_lines.append("、".join(inv_items))
    else:
        g_lines.append("（無隨身物）")

    sections.append("\n".join(g_lines))

    # [4] 輸出格式
    out_cfg = card.get("output", {})
    length_hint = out_cfg.get("length_hint", "200-500")
    req_header = out_cfg.get("header", True)

    fmt_lines = ["# 輸出格式要求"]
    if req_header:
        fmt_lines.append(
            "回覆的第一行必須輸出狀態標頭（包含當前日期、時間、地點、天氣與氛圍）：\n"
            f"📅 {clock_dt.year}/{clock_dt.month}/{clock_dt.day}　⏰ {clock_dt.strftime('%H:%M')}　📌 {place_str}　🌤️ {weather_str}\n\n"
            "標頭結束後請空一行，接著輸出敘事正文。"
        )
    else:
        fmt_lines.append("直接輸出敘事正文，不需要包含標頭。")

    fmt_lines.append(f"本回合建議敘事長度：約 {length_hint} 字。")
    sections.append("\n".join(fmt_lines))

    return "\n\n".join(sections)
