#!/usr/bin/env python3
"""Interactive world-creation wizard.

Walks the user through 5 questions about a new world, calls the LLM once to
expand the answers into a system_prompt body and seed lore entries, then writes:

    story_design/<slug>/system_prompt.txt
    story_design/<slug>/world_lore.json

Does NOT scaffold the runtime or register the story — run this first, then
`python3 scripts/new_story.py <slug>` to register. The new_story scaffold step
will skip files that already exist, so the wizard's output is preserved.

Usage:
    python3 scripts/world_create.py <slug>
    python3 scripts/world_create.py xianxia --provider gemini
    python3 scripts/world_create.py xianxia --dry-run   # show output, don't write
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from typing import Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from story_core.story_io import (  # noqa: E402
    STORY_DESIGN_DIR,
    STORY_TEMPLATE_DIR,
    _is_valid_story_id,
    _save_json,
    _story_design_dir,
)


# ---------------------------------------------------------------------------
# Q&A
# ---------------------------------------------------------------------------

QUESTIONS: list[tuple[str, str, str]] = [
    # (key, prompt, hint)
    ("world_name", "世界名（顯示用，例：仙俠世界）", ""),
    ("pitch", "一句話描述（類型 + 主軸）",
     "例：主角從凡人開始修真，經歷宗門鬥爭與飛升試煉"),
    ("progression", "進階/能力體系",
     "例：練氣→築基→金丹→元嬰→化神  / 一階至四階基因鎖  / 學徒→法師→大法師"),
    ("factions", "主要勢力或地點 3-5 個（每行一個，空行結束）",
     "例：太清宗 / 萬魔窟 / 散修聯盟"),
    ("opening", "起始情境（玩家一開始在哪、做什麼）",
     "例：主角剛入門太清宗，被分配到藏經閣當雜役"),
]


def _ask_line(prompt: str, hint: str = "") -> str:
    if hint:
        print(f"  提示：{hint}")
    while True:
        try:
            value = input(f"> ").strip()
        except EOFError:
            print()
            sys.exit(1)
        if value:
            return value
        print("  （請輸入內容，或 Ctrl-C 中止）")


def _ask_multiline(prompt: str, hint: str = "") -> list[str]:
    if hint:
        print(f"  提示：{hint}")
    print("  （每行一個，空行結束）")
    lines: list[str] = []
    while True:
        try:
            line = input(f"> ").strip()
        except EOFError:
            break
        if not line:
            if lines:
                break
            print("  （至少輸入一行）")
            continue
        lines.append(line)
    return lines


def _collect_answers() -> dict[str, Any]:
    answers: dict[str, Any] = {}
    total = len(QUESTIONS)
    for i, (key, prompt, hint) in enumerate(QUESTIONS, 1):
        print(f"\n[{i}/{total}] {prompt}")
        if key == "factions":
            answers[key] = _ask_multiline(prompt, hint)
        else:
            answers[key] = _ask_line(prompt, hint)
    return answers


# ---------------------------------------------------------------------------
# LLM expansion
# ---------------------------------------------------------------------------

EXPANSION_SYSTEM_PROMPT = """你是一個 RPG 世界設計助手。根據使用者提供的世界骨架，產出兩部分：

1. system_prompt_body: 一段給 GM 的世界觀說明文字（繁體中文，3-6 個 markdown 段落，每段以 ## 開頭）。
   應涵蓋：世界觀核心、進階/能力體系規則、敘事風格與語氣建議、主要禁忌或限制（如有）。
   不要包含 {placeholder} 形式的變數，不要重複使用者輸入的句子。

2. lore_entries: 15-25 條種子 lore 條目，覆蓋世界中的重要勢力、地點、體系、規則、人物。
   每條格式：{"category": "...", "topic": "...", "content": "..."}
   - category 用 2-4 個字的主題分類（例：「勢力」「地點」「體系」「人物」「物品」「規則」）
   - topic 是該條目的具體名稱（例：「太清宗」）
   - content 是 1-3 句話的完整描述

只輸出一個 JSON 物件，不要其他文字、不要程式碼圍欄。
JSON 結構：
{
  "system_prompt_body": "...",
  "lore_entries": [...]
}
"""


def _build_user_prompt(answers: dict[str, Any]) -> str:
    factions = answers["factions"]
    factions_text = "\n".join(f"- {f}" for f in factions) if factions else "（未指定）"
    return textwrap.dedent(f"""
        世界名：{answers['world_name']}
        類型/主軸：{answers['pitch']}
        進階/能力體系：{answers['progression']}
        主要勢力或地點：
        {factions_text}
        起始情境：{answers['opening']}

        請依系統提示產出 JSON。
    """).strip()


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _parse_llm_output(raw: str) -> dict[str, Any]:
    cleaned = _strip_code_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 回傳不是合法 JSON: {exc}\n--- raw ---\n{raw[:500]}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"LLM 回傳頂層不是 object: {type(data).__name__}")
    body = data.get("system_prompt_body", "")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("缺少 system_prompt_body 或內容為空")
    entries = data.get("lore_entries", [])
    if not isinstance(entries, list) or not entries:
        raise ValueError("缺少 lore_entries 或為空")
    cleaned_entries: list[dict[str, str]] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category", "")).strip()
        topic = str(entry.get("topic", "")).strip()
        content = str(entry.get("content", "")).strip()
        if not (category and topic and content):
            continue
        cleaned_entries.append({
            "category": category,
            "topic": topic,
            "content": content,
            "edited_by": "auto",
        })
    if not cleaned_entries:
        raise ValueError("lore_entries 全部缺欄位（需 category/topic/content）")
    return {
        "system_prompt_body": body.strip(),
        "lore_entries": cleaned_entries,
    }


def _call_llm(answers: dict[str, Any], provider: str | None) -> dict[str, Any]:
    from story_core.llm_bridge import call_oneshot
    prompt = _build_user_prompt(answers)
    print("\n正在呼叫 LLM 擴寫世界（約 20-60 秒）...")
    raw = call_oneshot(prompt, system_prompt=EXPANSION_SYSTEM_PROMPT, provider=provider)
    if not raw or not raw.strip():
        raise ValueError("LLM 回傳空字串")
    return _parse_llm_output(raw)


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """# 你是「{world_name}」文字 RPG 的 GM（遊戲主持人）
# {pitch}

@include _shared/gm_rules.txt

{world_body}

## 當前角色狀態
{{character_state}}

## 已知世界設定
{{world_lore}}

## NPC 概覽
{{npc_profiles}}

## 隊伍規則
{{team_rules}}

## 最近劇情回顧
{{narrative_recap}}

## 場景上下文
{{dungeon_context}}

@include _shared/tag_format.txt

@include _shared/fate_system.txt
"""


def _render_system_prompt(answers: dict[str, Any], world_body: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        world_name=answers["world_name"],
        pitch=answers["pitch"],
        world_body=world_body.strip(),
    )


# ---------------------------------------------------------------------------
# Preview & confirm
# ---------------------------------------------------------------------------

def _preview(generated: dict[str, Any], system_prompt_text: str) -> None:
    body = generated["system_prompt_body"]
    entries = generated["lore_entries"]
    print("\n" + "=" * 60)
    print(f"system_prompt 全文約 {len(system_prompt_text)} 字，世界觀段落 {len(body)} 字")
    print(f"lore 條目 {len(entries)} 條")
    print("=" * 60)
    print("\n[system_prompt 世界觀段落 — 前 600 字]")
    print(body[:600] + ("..." if len(body) > 600 else ""))
    print("\n[lore 樣本（前 3 條）]")
    for entry in entries[:3]:
        print(f"  - [{entry['category']}] {entry['topic']}: {entry['content'][:80]}...")
    print("=" * 60)


def _confirm(question: str) -> str:
    while True:
        try:
            ans = input(f"{question} [y]接受 / [r]重產 / [n]中止: ").strip().lower()
        except EOFError:
            return "n"
        if ans in ("y", "yes", ""):
            return "y"
        if ans in ("r", "regen", "regenerate"):
            return "r"
        if ans in ("n", "no", "abort"):
            return "n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("slug", help="story id slug (e.g. xianxia)")
    parser.add_argument("--provider", default=None,
                        help="override LLM provider (gemini / claude_cli / codex_agent)")
    parser.add_argument("--dry-run", action="store_true",
                        help="generate and preview but do not write files")
    parser.add_argument("--overwrite", action="store_true",
                        help="overwrite existing system_prompt.txt / world_lore.json if present")
    args = parser.parse_args()

    slug = args.slug.strip()
    if not _is_valid_story_id(slug):
        print("error: invalid slug — use lowercase [a-z0-9_], must start with a letter, max 40 chars",
              file=sys.stderr)
        sys.exit(1)

    design_dir = _story_design_dir(slug)
    sp_path = os.path.join(design_dir, "system_prompt.txt")
    lore_path = os.path.join(design_dir, "world_lore.json")
    if not args.overwrite and not args.dry_run:
        existing = [p for p in (sp_path, lore_path) if os.path.exists(p)]
        if existing:
            print("error: target files already exist (use --overwrite to replace):", file=sys.stderr)
            for p in existing:
                print(f"  {os.path.relpath(p)}", file=sys.stderr)
            sys.exit(1)

    print(f"建立世界：{slug}")
    print("(任何時候 Ctrl-C 中止)")
    answers = _collect_answers()

    while True:
        try:
            generated = _call_llm(answers, provider=args.provider)
        except (ValueError, Exception) as exc:
            print(f"\nLLM 擴寫失敗：{exc}", file=sys.stderr)
            choice = _confirm("要重試嗎?")
            if choice == "r":
                continue
            sys.exit(1)

        system_prompt_text = _render_system_prompt(answers, generated["system_prompt_body"])
        _preview(generated, system_prompt_text)

        choice = _confirm("\n接受這份結果?")
        if choice == "y":
            break
        if choice == "n":
            print("已中止，未寫入任何檔案。")
            sys.exit(0)
        # choice == 'r' → regenerate

    if args.dry_run:
        print("\n--dry-run：未寫入檔案。")
        return

    os.makedirs(design_dir, exist_ok=True)
    with open(sp_path, "w", encoding="utf-8") as handle:
        handle.write(system_prompt_text)
    _save_json(lore_path, generated["lore_entries"])

    # Seed character_schema.json + default_character_state.json from the
    # shared template if absent. Prevents the runtime DEFAULT_CHARACTER_*
    # fallback from ever being persisted to disk for a fresh story.
    seeded: list[str] = []
    for fname in ("character_schema.json", "default_character_state.json"):
        target = os.path.join(design_dir, fname)
        source = os.path.join(STORY_TEMPLATE_DIR, fname)
        if os.path.exists(target):
            continue
        if not os.path.exists(source):
            continue
        with open(source, "r", encoding="utf-8") as src, open(target, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        seeded.append(fname)

    print(f"\n✓ 寫入 {os.path.relpath(sp_path)}")
    print(f"✓ 寫入 {os.path.relpath(lore_path)} ({len(generated['lore_entries'])} 條)")
    for fname in seeded:
        print(f"✓ 從 _shared/_template 種入 {fname}")
    print(f"\n下一步：python3 scripts/new_story.py {slug} --name \"{answers['world_name']}\" --switch")


if __name__ == "__main__":
    main()
