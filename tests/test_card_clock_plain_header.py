from datetime import datetime

import pytest

from story_core.card_mode import clock


@pytest.mark.parametrize('separator', ['｜', '|'])
def test_plain_header_crosses_day_and_updates_scene(tmp_path, monkeypatch, separator):
    monkeypatch.setattr(clock, '_clock_path', lambda s, b: str(tmp_path / 'clock.json'))
    clock.save_clock('s', 'main', {
        'start_iso': '2026-10-07T23:40:00', 'elapsed_minutes': 0,
        'last_location': '聽雨客棧', 'last_weather': '山雨',
    })
    narration = f'2026/10/08（週四）07:20 {separator} 河鎮渡口 {separator} 雨歇\n歇息一夜後到了渡口。'
    assert clock.sync_from_narration('s', 'main', narration)
    assert clock.now('s') == datetime(2026, 10, 8, 7, 20)
    saved = clock.load_clock('s')
    assert saved['elapsed_minutes'] == 460
    assert saved['last_location'] == '河鎮渡口'
    assert saved['last_weather'] == '雨歇'


@pytest.mark.parametrize('text', [
    '她說：「2026/10/08 07:20 ｜ 渡口 ｜ 雨歇」，那是明天的計畫。',
    '明天 07:20 去渡口。',
    '2026/10/08 07:20 是約定時間。',
])
def test_prose_and_planned_times_are_not_headers(text):
    assert clock.parse_header_time(text) is None


def test_invalid_plain_header_does_not_change_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, '_clock_path', lambda s, b: str(tmp_path / 'clock.json'))
    original = {'start_iso': '2026-10-07T23:40:00', 'elapsed_minutes': 0}
    clock.save_clock('s', 'main', original)
    assert not clock.sync_from_narration('s', 'main', '2026/13/08 25:70 ｜ 渡口 ｜ 雨歇')
    assert clock.load_clock('s')['elapsed_minutes'] == 0


def test_emoji_header_weather_fallback_drops_separator():
    parsed = clock.parse_header_time('📅 2024/3/1 ⏰ 10:00 📌 青雲峰 ｜ 細雨\n正文')
    assert parsed['location'] == '青雲峰' and parsed['weather'] == '細雨'
