#!/usr/bin/env python3
"""Interactive Card Mode creation wizard.

Walks the user through 6 questions about a new character card, calls the LLM
once to expand the answers into a complete card.json (including opening narrative,
motifs, and persona), validates with validate_card(), then scaffolds:

    story_design/<slug>/card.json
    data/stories/<slug>/ (timeline_tree, branches/main, card_clock, etc.)
    data/stories.json (mode="card")

Usage:
    python3 scripts/card_create.py <slug>
    python3 scripts/card_create.py hanako --dry-run
    python3 scripts/card_create.py hanako --blank
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import re
import sys
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from story_core.card_mode.card import (  # noqa: E402
    init_card_story,
    validate_card,
)
from story_core.story_io import _is_valid_story_id  # noqa: E402


def _make_blank_template(slug: str) -> dict:
    """Generate a blank fill-in template for manual authoring."""
    return {
        "schema_version": 1,
        "id": slug,
        "display_name": "角色名稱",
        "tagline": "一句話標語",
        "pitch": "3-5 行情境簡介，介紹玩家與角色的初遇背景……",
        "player": {
            "role": "玩家扮演的身分",
            "premise": "玩家的動機或處境",
            "address": "你",
            "profile": "",
        },
        "setting": {
            "place": "故事主要發生的地點",
            "era": "現代",
            "start_datetime": "2026-05-16T02:08:00",
            "weather_pool": ["陰", "小雨", "起霧"],
            "ambience": ["安靜的時鐘滴答聲", "微風吹拂窗簾"],
        },
        "persona": {
            "identity": "角色的真實身分",
            "age_note": "外表與心智年齡備註",
            "appearance": "服裝、髮型、體型等外觀描述",
            "body_details": [
                "身高 160cm，身材勻稱，透著性感與反差",
                "肌膚白皙敏感，鎖骨明顯，常穿微敞開的衣服",
            ],
            "fetishes": [
                "性癖與特定心理偏好（例：渴望被強勢命令支配、喜歡被撫摸頭頂與後頸）",
                "肌膚接觸偏好或特定氛圍癖好",
            ],
            "sensitive_spots": [
                "後頸與脊椎上方（輕吹氣或碰觸時會渾身微顫、短暫屏息）",
                "腰肢兩側軟肉（一碰即觸電般劇烈縮起，溢出嬌喘）",
                "耳垂（耳邊低語時會泛紅動搖、聲音變調）",
            ],
            "personality": "具體個性描述（天然呆、執著、刀子嘴豆腐心等）",
            "speech_style": "說話風格口吻（短句、語尾微揚、慢條斯理等）",
            "verbal_tics": ["……嗯？", "真拿你沒辦法"],
            "mannerisms": ["說謊時會撥弄頭髮", "思考時輕敲桌面"],
            "desires": ["最想要的具體事物"],
            "fears": ["最害怕或逃避的事物"],
            "tells": ["緊張時眼神飄忽"],
            "boundaries": ["底線原則"],
            "sample_lines": [
                "「你怎麼會在這裡……？」",
                "「如果幫了你，我能得到什麼好處嗎？」",
            ],
        },
        "opening": "深夜的房間裡……（開場白第一幕敘事）",
        "start": {
            "relationship": {"tier": "陌生", "note": "初次見面"},
            "inventory": {"隨身物品": "口袋裡"},
        },
        "nsfw": {"enabled": False, "notes": "一般向或成人向"},
        "output": {
            "header": True,
            "length_hint": "200-500",
        },
    }


def _ask_line(prompt: str, hint: str = "") -> str:
    print(f"\n> {prompt}")
    if hint:
        print(f"  提示：{hint}")
    while True:
        try:
            val = input("  > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消。")
            sys.exit(0)
        if val:
            return val
        print("  （此欄位為必填，請輸入內容）")


def _collect_answers(slug: str) -> dict[str, str]:
    print("=" * 60)
    print(f"🎭 角色卡模式（Card Mode）建卡精靈 — [{slug}]")
    print("請回答以下 7 個關鍵問題，LLM 將為您展開成完整角色卡：")
    print("=" * 60)

    ans = {}
    ans["name_and_identity"] = _ask_line(
        "1. 角色名 + 一句話身分定位",
        "例：花子，三十年前死在大學的女學生，天然呆只想吃零食",
    )
    ans["player_role"] = _ask_line(
        "2. 玩家扮演誰 / 為什麼會遇到她",
        "例：夜班保全，再抓不到偷吃零食的小偷就要被開除",
    )
    ans["setting"] = _ask_line(
        "3. 場景與時間（地點、時代、開場時間）",
        "例：偏遠郊區的舊大學，現代，2026-05-16T02:08",
    )
    ans["appearance_body"] = _ask_line(
        "4. 外觀特徵與具體肉體細節（身形比例／衣著細節／膚觸）",
        "例：身高142cm纖細單薄、微透明蒼白肌膚、白襯衫第一顆釦子崩落微露鎖骨",
    )
    ans["kinks_and_spots"] = _ask_line(
        "5. 性癖偏好與生理敏感點（心理性癖／被對待的方式／敏感部位與生理反應）",
        "例：渴望被當成活著的女人強勢對待、喜歡被捏後頸；後頸與腰際被碰會渾身發軟微喘",
    )
    ans["desires_and_habits"] = _ask_line(
        "6. 渴望／恐懼／口癖習慣小動作",
        "例：想要抹茶Pocky與被摸頭誇獎；害怕被忘記；口癖「……蛤？」；緊張時影子會自己動",
    )
    ans["rating"] = _ask_line(
        "7. 內容分級與情感基調（一般向 / 成人向 / 情感風格）",
        "例：成人向微色氣，既有靈異危險又充滿親密感",
    )
    return ans


def _expand_with_llm(slug: str, ans: dict[str, str]) -> dict:
    from story_core.llm_bridge import call_oneshot

    system_prompt = (
        "你是專門設計文字 RPG 角色卡的專家架構師。請根據使用者的問答內容，展開成完全符合 Card Mode 規格的 JSON 角色卡。\n"
        "【重要規則】\n"
        "1. 必須嚴格輸出合法的 JSON，不要輸出任何額外 Markdown 解說或代碼塊外文字。\n"
        "2. `schema_version` 必須是 1，`id` 必須是指定的 slug。\n"
        "3. 強調肉體外觀、性癖與敏感點：\n"
        "   - `persona.body_details` 至少 3 筆生動的肉體特徵（身材、衣著細節、膚觸、體溫）。\n"
        "   - `persona.fetishes` 至少 2 筆心理與情境性癖（渴望被如何對待、特殊偏好）。\n"
        "   - `persona.sensitive_spots` 至少 2 筆生理敏感部位與被碰觸時的生理反應描寫。\n"
        "   - `persona.verbal_tics` 至少 2 筆慣用口癖。\n"
        "4. `start.relationship.tier` 必須為：'敵對'、'戒備'、'陌生'、'相識'、'友好'、'信任'、'摯友' 之一。\n"
        "5. `opening` 必須是精彩、具臨場感的開場第一幕敘事，結尾留給玩家回應。"
    )

    user_prompt = f"""
指定 slug: {slug}
問答資料：
1. 角色名與身分定位：{ans['name_and_identity']}
2. 玩家角色與處境：{ans['player_role']}
3. 場景與開場時間：{ans['setting']}
4. 外觀特徵與肉體細節：{ans['appearance_body']}
5. 性癖偏好與敏感弱點：{ans['kinks_and_spots']}
6. 渴望／恐懼／口癖習慣：{ans['desires_and_habits']}
7. 分級與基調：{ans['rating']}

請直接輸出對應的完整 card.json（嚴格遵守 Card Mode schema）：
"""
    print("\n正在呼叫 LLM 展開角色卡人設與開場白……")
    raw = call_oneshot(user_prompt, system_prompt=system_prompt)
    if not raw or not raw.strip():
        raise RuntimeError("LLM 未返回任何內容。")

    # Clean JSON markers if present
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        text = match.group(1).strip()

    try:
        card = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"解析 LLM 輸出的 JSON 失敗：{e}\n原始輸出內容：\n{raw}")
        raise

    card["id"] = slug
    card["schema_version"] = 1
    return card


def main():
    parser = argparse.ArgumentParser(description="角色卡模式（Card Mode）建卡精靈")
    parser.add_argument("slug", nargs="?", help="卡片唯一識別碼（英文小寫、數字、底線）")
    parser.add_argument("--blank", action="store_true", help="直接生成空白模板供手動填寫")
    parser.add_argument("--dry-run", action="store_true", help="只印出生成的 card.json，不寫入檔案")
    args = parser.parse_args()

    slug = args.slug
    if not slug:
        while True:
            slug = input("請輸入角色卡 ID (slug, 例: hanako): ").strip().lower()
            if slug and re.match(r"^[a-z0-9_]+$", slug):
                break
            print("ID 必須只包含小寫英文字母、數字或底線。")

    if not re.match(r"^[a-z0-9_]+$", slug):
        print(f"錯誤：ID '{slug}' 不合法，只允許小寫英數字及底線。")
        sys.exit(1)

    if args.blank:
        card = _make_blank_template(slug)
        print(f"已生成 [{slug}] 空白模板。")
    else:
        ans = _collect_answers(slug)
        card = _expand_with_llm(slug, ans)

    # 驗卡
    errors = validate_card(card)
    if errors:
        print("\n❌ 驗卡失敗：")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    if args.dry_run:
        print("\n【--dry-run 預覽生成的 card.json】")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    # 寫入並初始化
    init_card_story(card, overwrite=True)
    print("\n" + "=" * 60)
    print(f"✅ 角色卡 [{card.get('display_name', slug)}] 已成功建立！")
    print(f"  - 角色卡檔案：story_design/{slug}/card.json")
    print(f"  - 執行期存檔：data/stories/{slug}/")
    print(f"  - 對話網址：http://localhost:5051/card/{slug}")
    print("=" * 60)


if __name__ == "__main__":
    main()
