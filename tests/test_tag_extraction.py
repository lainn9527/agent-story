"""Tests for tag extraction regex functions in app.py (Phase 1.1).

Tests all 5 tag extractors: STATE, LORE, NPC, EVENT, IMG.
These are pure functions — no filesystem, no LLM, no mocking needed.
"""

import json
import re

import pytest

from app import (
    _extract_state_tag,
    _extract_lore_tag,
    _extract_npc_tag,
    _extract_event_tag,
    _extract_img_tag,
    _STATE_RE,
    _LORE_RE,
    _NPC_RE,
    _EVENT_RE,
    _IMG_RE,
)


# ===================================================================
# STATE tag
# ===================================================================


class TestExtractStateTag:
    def test_single_tag(self):
        text = '故事繼續...<!--STATE {"reward_points_delta": -500} STATE-->結束'
        clean, updates = _extract_state_tag(text)
        assert "STATE" not in clean
        assert "結束" in clean
        assert "故事繼續" in clean
        assert len(updates) == 1
        assert updates[0]["reward_points_delta"] == -500

    def test_multiple_tags(self):
        text = (
            '<!--STATE {"gene_lock": "已開啟"} STATE-->'
            "中間文字"
            '<!--STATE {"reward_points_delta": -5000} STATE-->'
        )
        clean, updates = _extract_state_tag(text)
        assert len(updates) == 2
        assert updates[0]["gene_lock"] == "已開啟"
        assert updates[1]["reward_points_delta"] == -5000
        assert "中間文字" in clean

    def test_malformed_json_skipped(self):
        text = "前面<!--STATE {bad json} STATE-->後面"
        clean, updates = _extract_state_tag(text)
        assert updates == []
        assert "前面" in clean
        assert "後面" in clean
        assert "STATE" not in clean

    def test_empty_json(self):
        text = "文字<!--STATE {} STATE-->結束"
        clean, updates = _extract_state_tag(text)
        assert len(updates) == 1
        assert updates[0] == {}

    def test_nested_braces(self):
        text = '<!--STATE {"inventory_add": ["劍", "盾"]} STATE-->'
        clean, updates = _extract_state_tag(text)
        assert len(updates) == 1
        assert updates[0]["inventory_add"] == ["劍", "盾"]

    def test_bracket_format(self):
        text = '[STATE {"current_phase": "副本中"} STATE]'
        clean, updates = _extract_state_tag(text)
        assert len(updates) == 1
        assert updates[0]["current_phase"] == "副本中"

    def test_multiline_json(self):
        text = '<!--STATE {\n  "name": "測試",\n  "level": 5\n} STATE-->'
        clean, updates = _extract_state_tag(text)
        assert len(updates) == 1
        assert updates[0]["name"] == "測試"
        assert updates[0]["level"] == 5

    def test_no_tag_returns_original(self):
        text = "這是普通文字，沒有任何標籤"
        clean, updates = _extract_state_tag(text)
        assert clean == text
        assert updates == []

    def test_surrounding_text_preserved(self):
        text = "開頭文字\n\n第二段<!--STATE {} STATE-->第三段\n\n結尾"
        clean, updates = _extract_state_tag(text)
        assert "開頭文字" in clean
        assert "結尾" in clean
        assert len(updates) == 1

    def test_chinese_values(self):
        text = '<!--STATE {"current_status": "正在與伽椰子戰鬥", "current_phase": "副本中"} STATE-->'
        clean, updates = _extract_state_tag(text)
        assert updates[0]["current_status"] == "正在與伽椰子戰鬥"
        assert updates[0]["current_phase"] == "副本中"


# ===================================================================
# LORE tag
# ===================================================================


class TestExtractLoreTag:
    def test_single_lore(self):
        lore = {"topic": "基因鎖", "category": "體系", "content": "基因鎖是人類潛能的封印"}
        text = f"描述...<!--LORE {json.dumps(lore, ensure_ascii=False)} LORE-->結束"
        clean, lores = _extract_lore_tag(text)
        assert len(lores) == 1
        assert lores[0]["topic"] == "基因鎖"
        assert "LORE" not in clean

    def test_multiple_lores(self):
        l1 = {"topic": "A", "category": "體系", "content": "內容A"}
        l2 = {"topic": "B", "category": "商城", "content": "內容B"}
        text = f'<!--LORE {json.dumps(l1, ensure_ascii=False)} LORE-->中間<!--LORE {json.dumps(l2, ensure_ascii=False)} LORE-->'
        clean, lores = _extract_lore_tag(text)
        assert len(lores) == 2

    def test_malformed_json_skipped(self):
        text = "<!--LORE not valid json LORE-->"
        clean, lores = _extract_lore_tag(text)
        assert lores == []

    def test_no_tag(self):
        text = "普通文字"
        clean, lores = _extract_lore_tag(text)
        assert clean == text
        assert lores == []


# ===================================================================
# NPC tag
# ===================================================================


class TestExtractNpcTag:
    def test_single_npc(self):
        npc = {
            "name": "小薇",
            "role": "隊友",
            "personality": {
                "openness": 7,
                "conscientiousness": 8,
                "extraversion": 3,
                "agreeableness": 6,
                "neuroticism": 4,
                "summary": "冷靜理性的少女",
            },
        }
        text = f'角色登場<!--NPC {json.dumps(npc, ensure_ascii=False)} NPC-->繼續'
        clean, npcs = _extract_npc_tag(text)
        assert len(npcs) == 1
        assert npcs[0]["name"] == "小薇"
        assert npcs[0]["personality"]["openness"] == 7
        assert "<!--NPC" not in clean  # tag removed

    def test_partial_npc_data(self):
        npc = {"name": "路人"}
        text = f'<!--NPC {json.dumps(npc)} NPC-->'
        clean, npcs = _extract_npc_tag(text)
        assert len(npcs) == 1
        assert npcs[0]["name"] == "路人"

    def test_malformed_json(self):
        text = "<!--NPC {name: invalid} NPC-->"
        clean, npcs = _extract_npc_tag(text)
        assert npcs == []


# ===================================================================
# EVENT tag
# ===================================================================


class TestExtractEventTag:
    def test_single_event(self):
        event = {
            "event_type": "伏筆",
            "title": "神秘組織",
            "description": "背後似乎有一個神秘組織在操控一切",
            "status": "planted",
        }
        text = f'劇情發展<!--EVENT {json.dumps(event, ensure_ascii=False)} EVENT-->下一段'
        clean, events = _extract_event_tag(text)
        assert len(events) == 1
        assert events[0]["event_type"] == "伏筆"
        assert events[0]["title"] == "神秘組織"
        assert "EVENT" not in clean

    def test_all_event_types(self):
        types = ["伏筆", "轉折", "遭遇", "發現", "戰鬥", "獲得", "觸發"]
        for etype in types:
            event = {"event_type": etype, "title": f"測試{etype}", "description": "描述"}
            text = f'<!--EVENT {json.dumps(event, ensure_ascii=False)} EVENT-->'
            _, events = _extract_event_tag(text)
            assert len(events) == 1
            assert events[0]["event_type"] == etype

    def test_multiple_events(self):
        e1 = {"event_type": "戰鬥", "title": "A", "description": "a"}
        e2 = {"event_type": "發現", "title": "B", "description": "b"}
        text = f'<!--EVENT {json.dumps(e1)} EVENT-->中間<!--EVENT {json.dumps(e2)} EVENT-->'
        clean, events = _extract_event_tag(text)
        assert len(events) == 2

    def test_malformed_json(self):
        text = "<!--EVENT invalid EVENT-->"
        clean, events = _extract_event_tag(text)
        assert events == []


# ===================================================================
# IMG tag
# ===================================================================


class TestExtractImgTag:
    def test_single_img(self):
        text = "場景描述<!--IMG prompt: a dark forest with glowing crystals IMG-->繼續"
        clean, prompt = _extract_img_tag(text)
        assert prompt == "a dark forest with glowing crystals"
        assert "IMG" not in clean
        assert "場景描述" in clean
        assert "繼續" in clean

    def test_no_img(self):
        text = "普通文字"
        clean, prompt = _extract_img_tag(text)
        assert clean == text
        assert prompt is None

    def test_multiple_imgs_returns_first(self):
        text = "<!--IMG prompt: first scene IMG-->中間<!--IMG prompt: second scene IMG-->"
        clean, prompt = _extract_img_tag(text)
        assert prompt == "first scene"

    def test_empty_prompt(self):
        text = "<!--IMG prompt:  IMG-->"
        clean, prompt = _extract_img_tag(text)
        # Empty prompt stripped → None (empty string after strip)
        assert prompt is None

    def test_bracket_format(self):
        text = "[IMG prompt: anime style warrior IMG]"
        clean, prompt = _extract_img_tag(text)
        assert prompt == "anime style warrior"


# ===================================================================
# Mixed tags in single response
# ===================================================================


class TestMixedTags:
    def test_all_tag_types_in_one_response(self):
        state = {"reward_points_delta": 1000}
        lore = {"topic": "新發現", "category": "體系", "content": "內容"}
        npc = {"name": "新NPC", "role": "敵人"}
        event = {"event_type": "遭遇", "title": "伏擊", "description": "被伏擊了"}
        img_prompt = "battle scene"

        text = (
            f"GM的回覆開始。"
            f'<!--STATE {json.dumps(state)} STATE-->'
            f"然後發生了一些事。"
            f'<!--LORE {json.dumps(lore, ensure_ascii=False)} LORE-->'
            f"遇到了新角色。"
            f'<!--NPC {json.dumps(npc, ensure_ascii=False)} NPC-->'
            f"發生了戰鬥！"
            f'<!--EVENT {json.dumps(event, ensure_ascii=False)} EVENT-->'
            f"場景很壯觀。"
            f"<!--IMG prompt: {img_prompt} IMG-->"
            f"結束。"
        )

        # Extract each type independently (same pattern as _process_gm_response)
        text, states = _extract_state_tag(text)
        text, lores = _extract_lore_tag(text)
        text, npcs = _extract_npc_tag(text)
        text, events = _extract_event_tag(text)
        text, prompt = _extract_img_tag(text)

        assert len(states) == 1
        assert len(lores) == 1
        assert len(npcs) == 1
        assert len(events) == 1
        assert prompt == img_prompt

        # Narrative text preserved
        assert "GM的回覆開始" in text
        assert "結束" in text
        # All tags stripped
        assert "<!--" not in text
        assert "STATE" not in text


# ===================================================================
# Regex pattern tests
# ===================================================================


class TestRegexPatterns:
    def test_state_re_html_comment(self):
        assert _STATE_RE.search('<!--STATE {} STATE-->')

    def test_state_re_bracket(self):
        assert _STATE_RE.search('[STATE {} STATE]')

    def test_state_re_no_match(self):
        assert _STATE_RE.search('STATE {} STATE') is None

    def test_img_re_requires_prompt_keyword(self):
        assert _IMG_RE.search('<!--IMG prompt: test IMG-->') is not None
        assert _IMG_RE.search('<!--IMG test IMG-->') is None

    def test_dotall_multiline(self):
        text = '<!--STATE {\n"a": 1\n} STATE-->'
        assert _STATE_RE.search(text) is not None
