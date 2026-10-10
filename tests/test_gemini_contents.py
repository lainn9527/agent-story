"""Conversation boundary regressions shared by Gemini and Vertex."""

import copy
import pytest

from story_core.gemini_bridge import _build_contents


@pytest.mark.parametrize("role", ["gm", "assistant"])
def test_completed_reply_survives_next_turn(role):
    recent = [
        {"role": "user", "content": "把水遞給她。"},
        {"role": role, "content": "她已接過水杯，坐到沙發上。"},
    ]
    original = copy.deepcopy(recent)
    contents = _build_contents(recent, "水溫可以嗎？")
    assert contents == [
        {"role": "user", "parts": [{"text": "把水遞給她。"}]},
        {"role": "model", "parts": [{"text": "她已接過水杯，坐到沙發上。"}]},
        {"role": "user", "parts": [{"text": "水溫可以嗎？"}]},
    ]
    assert recent == original


def test_opening_survives_first_player_turn():
    contents = _build_contents(
        [{"role": "gm", "content": "蘇曼坐在沙發上，杯子放在茶几。"}], "還沒睡？"
    )
    assert [m["role"] for m in contents] == ["user", "model", "user"]
    assert contents[1]["parts"][0]["text"] == "蘇曼坐在沙發上，杯子放在茶几。"


def test_legacy_current_user_is_replaced_by_augmented_input():
    recent = [
        {"role": "user", "content": "你好"},
        {"role": "gm", "content": "晚安"},
        {"role": "user", "content": "水溫可以嗎？"},
    ]
    contents = _build_contents(recent, "[context]\n水溫可以嗎？")
    assert len(contents) == 3
    assert contents[-2]["parts"][0]["text"] == "晚安"
    assert contents[-1]["parts"][0]["text"] == "[context]\n水溫可以嗎？"


def test_empty_history_and_consecutive_roles():
    assert _build_contents([], "你好") == [
        {"role": "user", "parts": [{"text": "你好"}]}
    ]
    contents = _build_contents([
        {"role": "gm", "content": "開場"},
        {"role": "assistant", "content": "後續"},
    ], "你好")
    assert contents[1]["parts"][0]["text"] == "開場\n\n後續"


def test_vertex_uses_same_history_builder():
    from story_core.vertex_bridge import _build_contents as vertex_contents
    assert vertex_contents is _build_contents
