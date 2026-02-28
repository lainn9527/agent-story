"""Tests for recent-context cleanup helpers."""

import pytest

from app import (
    _CHOICE_BLOCK_RE,
    _FATE_LABEL_RE,
    _sanitize_recent_messages,
    _strip_fate_from_messages,
)


# ── _FATE_LABEL_RE matching ──────────────────────────────────────────

@pytest.mark.parametrize("text", [
    # Half-width brackets (the bug that prompted this fix)
    "**[命運走向：順遂]**",
    "**[命運走向：平淡]**",
    "**[命運走向：波折]**",
    "**[命運走向：天命]**",
    "**[命運判定：大成功]**",
    "**[命運判定：失敗]**",
    "**[命運判定：勉強成功]**",
    "**[命運判定效果：深度寫入]**",
    # Full-width brackets
    "**【命運走向：順遂】**",
    "**【命運判定：大成功】**",
    "**【命運判定:勉強成功】**",
    "**【命運判定觸發:嚴重失敗】**",
    "**【命運判定結果:大失敗】**",
    # No bold markers
    "【命運判定:失敗】",
    "【命運判定結果:大失敗】",
    "[命運走向：順遂]",
    # With heading prefix
    "### **【命運判定:趙姐的話術真實性】**",
])
def test_fate_label_matches(text):
    assert _FATE_LABEL_RE.search(text), f"Should match: {text}"


@pytest.mark.parametrize("text", [
    # Inline narrative mentions — must NOT match
    "因為「命運判定」的加持",
    "藉著**命運判定的成功**",
    "就在這命運走向極度順遂的一刻",
    # Normal text
    "你揮出了一劍",
    "",
])
def test_fate_label_rejects_inline(text):
    assert not _FATE_LABEL_RE.search(text), f"Should NOT match: {text}"


# ── _strip_fate_from_messages ────────────────────────────────────────

def test_strip_removes_fate_labels():
    messages = [
        {"role": "gm", "content": "**[命運走向：順遂]**\n\n你成功了。"},
        {"role": "user", "content": "我繼續前進"},
        {"role": "gm", "content": "**【命運判定：大成功】**\n\n一切順利。"},
    ]
    result = _strip_fate_from_messages(messages)
    assert result[0]["content"] == "你成功了。"
    assert result[1]["content"] == "我繼續前進"  # user msg untouched
    assert result[2]["content"] == "一切順利。"


def test_strip_does_not_modify_originals():
    original_content = "**[命運走向：波折]**\n\n遇到了麻煩。"
    messages = [{"role": "gm", "content": original_content}]
    result = _strip_fate_from_messages(messages)
    assert result[0]["content"] == "遇到了麻煩。"
    assert messages[0]["content"] == original_content  # original unchanged


def test_strip_noop_when_no_fate():
    messages = [
        {"role": "gm", "content": "你揮出了一劍。"},
        {"role": "user", "content": "繼續攻擊"},
    ]
    result = _strip_fate_from_messages(messages)
    assert result[0]["content"] == "你揮出了一劍。"
    assert result[1]["content"] == "繼續攻擊"


# ── choice-block stripping for model context ─────────────────────────


def test_choice_block_regex_matches_trailing_options():
    text = "你踏進走廊。\n\n**可選行動：**\n1. 前進\n2. 後退"
    assert _CHOICE_BLOCK_RE.search(text)


def test_sanitize_recent_removes_gm_choice_block_but_keeps_user_text():
    messages = [
        {"role": "gm", "content": "你踏進走廊。\n\n**可選行動：**\n1. 前進\n2. 後退"},
        {"role": "user", "content": "可選行動：我想自由行動"},
    ]
    result = _sanitize_recent_messages(messages, strip_fate=False)
    assert result[0]["content"] == "你踏進走廊。"
    assert result[1]["content"] == "可選行動：我想自由行動"
