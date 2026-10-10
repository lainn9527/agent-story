"""Unit tests for Card Mode: schema, loader, clock, contract, prompt, ops, pipeline, routes."""

import copy
from datetime import datetime, timedelta
import json
import os
import pytest

from story_core.card_mode.card import init_card_story, load_card, validate_card
from story_core.card_mode.clock import advance, load_clock, now, parse_header_time, sync_from_narration
from story_core.gm_v2 import relationship as _rel


@pytest.fixture(autouse=True)
def isolate_card_recap(tmp_path, monkeypatch):
    monkeypatch.setattr("story_core.compaction.STORIES_DIR", str(tmp_path / "stories"))


@pytest.fixture
def sample_card():
    return {
        "schema_version": 1,
        "id": "test_hanako",
        "display_name": "花子",
        "tagline": "只要膽子大，女鬼放產假",
        "pitch": "你是舊大學的夜班保全，抓不到偷零食的小偷就要被開除……",
        "player": {
            "role": "舊大學夜班保全",
            "premise": "再抓不到小偷就要被開除",
            "address": "你",
        },
        "setting": {
            "place": "偏遠郊區的舊大學",
            "era": "現代",
            "start_datetime": "2026-05-16T02:08:00",
            "weather_pool": ["陰", "小雨", "起霧"],
            "ambience": ["黑冠麻鷺的呱呱叫", "日光燈管嗡嗡聲"],
        },
        "persona": {
            "identity": "三十年前死在這所學校的女學生",
            "age_note": "死時 23 歲，思考速度停在那個年紀",
            "appearance": "白襯衫＋紅色吊帶裙，黑色短髮凌亂，身體微透明",
            "body_details": ["身高 142cm", "襯衫第一顆釦子早就崩掉了", "笑起來有顆小虎牙"],
            "personality": "天然呆、好奇心強、對自己是鬼這件事很隨便",
            "speech_style": "短句、語尾拖長、想不通就直接問",
            "verbal_tics": ["......蛤？", "哦——"],
            "mannerisms": ["歪頭", "腮幫子鼓起來"],
            "desires": ["零食（尤其抹茶 Pocky）"],
            "fears": ["被忘記"],
            "tells": ["緊張時影子會自己動"],
            "boundaries": ["不會主動殺人"],
            "sample_lines": ["「妖孽...？」"],
        },
        "opening": "深夜兩點的舊校舍……「……蛤？」",
        "start": {
            "relationship": {"tier": "陌生", "note": "剛被你抓到偷零食"},
            "inventory": {"警棍": "腰間", "手電筒": ""},
        },
        "nsfw": {"enabled": True, "notes": "成年人設定"},
        "output": {
            "header": True,
            "length_hint": "200-500",
        },
    }


def test_validate_card_success(sample_card):
    errors = validate_card(sample_card)
    assert errors == []


def test_validate_card_missing_required_fields(sample_card):
    # Invalid slug
    bad = copy.deepcopy(sample_card)
    bad["id"] = "Invalid ID With Space"
    errs = validate_card(bad)
    assert any("id" in e for e in errs)

    # Optional verbal_tics; reject invalid entries
    bad = copy.deepcopy(sample_card)
    bad["persona"]["verbal_tics"] = [123]
    errs = validate_card(bad)
    assert any("verbal_tics" in e for e in errs)

    # Missing body_details
    bad = copy.deepcopy(sample_card)
    bad["persona"]["body_details"] = [""]
    errs = validate_card(bad)
    assert any("body_details" in e for e in errs)

    # Invalid relationship tier
    bad = copy.deepcopy(sample_card)
    bad["start"]["relationship"]["tier"] = "超級喜歡"
    errs = validate_card(bad)
    assert any("relationship.tier" in e for e in errs)


def test_init_card_story(tmp_path, sample_card, monkeypatch):
    stories_dir = str(tmp_path / "stories")
    design_dir = str(tmp_path / "design")
    registry_file = str(tmp_path / "stories.json")

    with open(registry_file, "w", encoding="utf-8") as f:
        json.dump({"stories": {}}, f)

    monkeypatch.setattr("story_core.story_io.STORIES_REGISTRY_PATH", registry_file)
    monkeypatch.setattr("story_core.card_mode.card.STORY_DESIGN_DIR", design_dir)

    init_card_story(sample_card, stories_dir=stories_dir, design_dir=design_dir)

    # Check card.json was saved
    loaded = load_card("test_hanako", design_dir=design_dir)
    assert loaded["id"] == "test_hanako"

    # Check runtime files
    b_dir = os.path.join(stories_dir, "test_hanako", "branches", "main")
    assert os.path.exists(os.path.join(b_dir, "card_clock.json"))
    assert os.path.exists(os.path.join(b_dir, "character_state.json"))
    assert os.path.exists(os.path.join(b_dir, "npcs.json"))
    assert os.path.exists(os.path.join(b_dir, "messages.json"))

    # Check messages has opening message at index 0
    with open(os.path.join(b_dir, "messages.json"), encoding="utf-8") as f:
        msgs = json.load(f)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "gm"
    assert msgs[0]["index"] == 0
    assert "clock_snapshot" in msgs[0]

    # Check registry marked mode="card"
    with open(registry_file, encoding="utf-8") as f:
        reg = json.load(f)
    assert reg["stories"]["test_hanako"]["mode"] == "card"
    assert reg["stories"]["test_hanako"]["name"] == "花子"


def test_clock_parse_and_advance(tmp_path, monkeypatch):
    b_dir = str(tmp_path / "branch")
    os.makedirs(b_dir, exist_ok=True)
    clock_file = os.path.join(b_dir, "card_clock.json")

    init_clock = {
        "start_iso": "2026-05-16T02:08:00",
        "elapsed_minutes": 0,
        "last_weather": "陰",
    }
    with open(clock_file, "w", encoding="utf-8") as f:
        json.dump(init_clock, f)

    monkeypatch.setattr("story_core.card_mode.clock._clock_path", lambda s, b: clock_file)

    # 1. Check now()
    cur = now("test_s", "main")
    assert cur == datetime(2026, 5, 16, 2, 8)

    # 2. Advance 15 minutes
    adv = advance("test_s", "main", 15)
    assert adv == datetime(2026, 5, 16, 2, 23)

    # 3. Parse header time
    header_text = "📅 2026/5/16　⏰ 02:32　📌 舊大學一樓女廁　🌤️ 陰，水管低沉的咕嚕聲\n\n她抬起頭看著你。"
    parsed = parse_header_time(header_text)
    assert parsed is not None
    assert parsed["hour"] == 2
    assert parsed["minute"] == 32
    assert parsed["weather"] == "陰，水管低沉的咕嚕聲"
    assert parsed["location"] == "舊大學一樓女廁"

    # 4. Sync from narration (2:23 -> 2:32 is +9 mins jump, <= 12h)
    sync_from_narration("test_s", "main", header_text)
    synced = now("test_s", "main")
    assert synced == datetime(2026, 5, 16, 2, 32)
    loaded = load_clock("test_s", "main")
    assert loaded["last_weather"] == "陰，水管低沉的咕嚕聲"

    # 5. Excessive jump (> 12h) should be ignored as hallucination
    hallucination_header = "⏰ 18:00　📌 校門口　🌤️ 晴天\n\n傍晚了。"
    # 02:32 -> 18:00 is ~15.5 hours jump (> 12h)
    sync_from_narration("test_s", "main", hallucination_header)
    cur_after_hallucination = now("test_s", "main")
    assert cur_after_hallucination == datetime(2026, 5, 16, 2, 32)  # Unchanged!


def test_build_card_system_prompt(sample_card):
    from story_core.card_mode.prompt import build_card_system_prompt

    state = {"inventory": {"警棍": "腰間", "手電筒": ""}}
    npc = {
        "name": "花子",
        "memory": ["你給了她一包 Pocky"],
        "relationship": {"tier": "相識", "note": "她覺得你是好人"},
    }
    clock_dt = datetime(2026, 5, 16, 2, 32)

    prompt = build_card_system_prompt(sample_card, state, npc, clock_dt, last_weather="陰，水管在咕嚕叫")

    # Verify persona & contract
    assert "花子" in prompt
    assert "角色扮演契約" in prompt
    assert "角色沒有新的內容要說時，不為了收尾再補一句" in prompt
    assert "不要求每輪都沉默或以動作結尾" in prompt
    assert "口癖" not in prompt
    assert "天然呆" in prompt
    assert "......蛤？" not in prompt
    assert "身高 142cm" in prompt

    # Verify 4 grounding sections exist
    assert "## 與你的關係" in prompt
    assert "相識（她覺得你是好人）" in prompt
    assert "## 她記得的事（長期）" in prompt
    assert "你給了她一包 Pocky" in prompt
    assert "## 現在" in prompt
    assert "2026/5/16" in prompt
    assert "## 你身上有" in prompt
    assert "警棍（腰間）" in prompt

    # STRICTLY NO World simulator blocks
    forbidden = ["domains", "vendor", "功法", "任務", "副本", "<!--ACTIONS-->", "<!--STATE-->", "<!--DELTA"]
    for word in forbidden:
        assert word not in prompt


def test_ops_and_gate():
    from story_core.card_mode.ops import (
        gate_relationship,
        gate_remember,
        gate_advance_time,
        gate_inventory,
    )

    # 1. gate_relationship: valid 1-step upgrade
    clean_rel, err = gate_relationship("相識", "剛認識", "陌生")
    assert err is None
    assert clean_rel["tier"] == "相識"

    # Excessive upgrade should clamp to +1 step
    clean_rel, err = gate_relationship("信任", "直接變好", "陌生")
    assert err is None
    assert clean_rel["tier"] == "相識"  # Clamped from 陌生 to 相識!

    # Invalid tier
    clean_rel, err = gate_relationship("天下第一好", "亂填", "陌生")
    assert err is not None

    # 2. gate_remember
    existing = ["第一件事"]
    raw_facts = ["  第二件事  ", "第一件事", "", "x" * 250]
    clean_mem, dropped = gate_remember(raw_facts, existing)
    assert clean_mem[0] == "第二件事"
    assert len(clean_mem[1]) == 200  # Capped
    assert len(clean_mem) == 2

    # 3. gate_advance_time
    mins, err = gate_advance_time(45)
    assert mins == 45
    assert err is None
    mins, err = gate_advance_time(-10)
    assert err is not None

    # 4. gate_inventory (junk key filtering & recover)
    raw_inv = {
        "手電筒": "微光",
        "$append": "便利商店塑膠袋",
        "a": "單字垃圾鍵",
        "keys": "方法垃圾鍵",
    }
    clean_inv, dropped = gate_inventory(raw_inv)
    assert "手電筒" in clean_inv
    assert "便利商店塑膠袋" in clean_inv  # Recovered from $append!
    assert "a" not in clean_inv
    assert "keys" not in clean_inv


def test_pipeline_turn_execution(tmp_path, sample_card, monkeypatch):
    from story_core.card_mode.pipeline import run_card_turn

    stories_dir = str(tmp_path / "stories")
    design_dir = str(tmp_path / "design")
    registry_file = str(tmp_path / "stories.json")

    with open(registry_file, "w", encoding="utf-8") as f:
        json.dump({"stories": {}}, f)

    monkeypatch.setattr("story_core.story_io.STORIES_REGISTRY_PATH", registry_file)
    monkeypatch.setattr("story_core.story_io.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("story_core.story_io.STORIES_DIR", stories_dir)
    monkeypatch.setattr("story_core.card_mode.card.STORY_DESIGN_DIR", design_dir)

    init_card_story(sample_card, stories_dir=stories_dir, design_dir=design_dir)

    # Mock PASS 1 LLM fn
    mock_header = "📅 2026/5/16　⏰ 02:25　📌 舊大學一樓福利社　🌤️ 陰，日光燈嗡嗡響\n\n花子歪著頭看著你：「……蛤？你真的不開除我嗎？」"

    def mock_llm(user_msg, sys_prompt, recent):
        return mock_header

    # Mock PASS 2 Recorder fn
    from story_core.card_mode.ops import execute_card_tool

    def mock_recorder(story_id, branch_id, narration, gm_idx):
        execute_card_tool(story_id, branch_id, "set_relationship", {"tier": "相識", "note": "覺得你不是壞人"})
        execute_card_tool(story_id, branch_id, "remember", {"facts": ["保全答應不開除她"]})
        execute_card_tool(story_id, branch_id, "add_inventory", {"綠茶餅乾": "剩半包"})

    res = run_card_turn(
        story_id="test_hanako",
        branch_id="main",
        user_message="別緊張，我不會開除妳的。",
        llm_fn=mock_llm,
        recorder_fn=mock_recorder,
        async_recorder=False,  # sync for test
    )

    assert res["ok"] is True
    assert res["gm_index"] == 2  # user was index 1, gm is index 2

    # Check state and npcs were updated by recorder
    b_dir = os.path.join(stories_dir, "test_hanako", "branches", "main")
    with open(os.path.join(b_dir, "npcs.json"), encoding="utf-8") as f:
        npcs = json.load(f)
    assert npcs[0]["relationship"]["tier"] == "相識"
    assert "保全答應不開除她" in npcs[0]["memory"]

    with open(os.path.join(b_dir, "character_state.json"), encoding="utf-8") as f:
        st = json.load(f)
    assert st["inventory"]["綠茶餅乾"] == "剩半包"

    # Check clock was synced from header (02:08 -> 02:25 = +17 mins)
    cur_clock = load_clock("test_hanako", "main")
    assert cur_clock["elapsed_minutes"] == 17


@pytest.mark.parametrize('recall_mode', ['off', 'auto'])
def test_curated_lore_is_in_narration_not_recorder(tmp_path, sample_card, monkeypatch, recall_mode):
    from story_core.card_mode import lore, recall
    from story_core.card_mode.pipeline import run_card_turn
    monkeypatch.setattr('story_core.story_io.STORIES_REGISTRY_PATH', str(tmp_path / 'stories.json'))
    monkeypatch.setattr('story_core.story_io.STORIES_DIR', str(tmp_path / 'stories'))
    monkeypatch.setattr('story_core.card_mode.card.STORY_DESIGN_DIR', str(tmp_path / 'design'))
    init_card_story(sample_card, stories_dir=str(tmp_path / 'stories'), design_dir=str(tmp_path / 'design'))
    from story_core import story_io as io
    io._save_json(str(lore.path('test_hanako', 'main')), {'schema_version': 1, 'entries': [
        {'id': 'canon', 'title': '學校', 'text': '圖書館九點關門', 'keywords': ['圖書館'],
         'sources': [{'index': 0, 'hash': lore.content_hash(sample_card['opening'])}]},
    ]})
    narration = []; recording = []
    def generate(user, system, recent):
        narration.append(system)
        return '她點了點頭。'
    def recorder(**kwargs):
        recording.append(kwargs['system_prompt'])
    monkeypatch.setattr('story_core.gemini_bridge.call_gemini_gm_tools', recorder)
    actions = iter([{'action': 'lore_read', 'id': 'l:canon'}, {'action': 'finish'}])
    def ask(slug, branch, system, context):
        assert json.loads(context)['lore_catalog'][0]['id'] == 'l:canon'
        return json.dumps(next(actions))
    monkeypatch.setattr(recall, '_ask', ask)
    result = run_card_turn('test_hanako', user_message='去圖書館', llm_fn=generate,
                           async_recorder=False, recall_mode=recall_mode)
    assert result['ok'] and '圖書館九點關門' in narration[0]
    assert narration[0].count('圖書館九點關門') == 1
    assert ('本回合自動查得的世界設定' in narration[0]) == (recall_mode == 'auto')
    assert all('圖書館九點關門' not in p for p in recording)


def test_card_api_routes(tmp_path, sample_card, monkeypatch):
    sample_card["relationship_enabled"] = False
    from app import app

    stories_dir = str(tmp_path / "stories")
    design_dir = str(tmp_path / "design")
    registry_file = str(tmp_path / "stories.json")

    with open(registry_file, "w", encoding="utf-8") as f:
        json.dump({
            "stories": {
                "rpg_story": {"id": "rpg_story", "name": "世界模擬器RPG", "mode": "standard"},
            }
        }, f)

    monkeypatch.setattr("story_core.story_io.STORIES_REGISTRY_PATH", registry_file)
    monkeypatch.setattr("story_core.story_io.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("story_core.story_io.STORIES_DIR", stories_dir)
    monkeypatch.setattr("story_core.card_mode.card.STORY_DESIGN_DIR", design_dir)
    monkeypatch.setattr("routes.card_routes.STORY_DESIGN_DIR", design_dir)

    init_card_story(sample_card, stories_dir=stories_dir, design_dir=design_dir)

    client = app.test_client()

    # 1. /api/stories includes card mode story
    res_stories = client.get("/api/stories")
    assert res_stories.status_code == 200
    stories_data = res_stories.get_json()["stories"]
    assert "rpg_story" in stories_data
    assert "test_hanako" not in stories_data

    # 2. /api/card/list must return card mode story
    res_cards = client.get("/api/card/list")
    assert res_cards.status_code == 200
    cards_list = res_cards.get_json()["cards"]
    assert any(c["id"] == "test_hanako" for c in cards_list)
    assert not any(c["id"] == "rpg_story" for c in cards_list)

    # 3. /api/card/<slug>/state
    res_state = client.get("/api/card/test_hanako/state")
    assert res_state.status_code == 200
    st_json = res_state.get_json()
    assert st_json["ok"] is True
    assert st_json["card"]["display_name"] == "花子"
    assert st_json["card"]["relationship_enabled"] is False
    assert st_json["relationship"]["tier"] == "陌生"
    assert len(st_json["messages"]) == 1
    assert st_json["messages"][0]["index"] == 0

    # 4. /api/card/<slug>/send/stream (SSE)
    mock_header = "📅 2026/5/16　⏰ 02:15　📌 舊大學一樓福利社　🌤️ 陰\n\n花子揉了揉眼睛：「誰啊……」"
    monkeypatch.setattr("story_core.card_mode.pipeline._generate_pass1", lambda **kw: (mock_header, "mock"))
    monkeypatch.setattr("routes.card_routes.run_card_turn", lambda **kw: {"ok": True, "narration": mock_header, "gm_index": 2, "user_index": 1})

    res_stream = client.post("/api/card/test_hanako/send/stream?sync=1", json={"message": "是我，夜班保全。"})
    assert res_stream.status_code == 200
    assert "text/event-stream" in res_stream.content_type

    # 5. /api/card/<slug>/reset
    res_reset = client.post("/api/card/test_hanako/reset")
    assert res_reset.status_code == 200
    assert res_reset.get_json()["ok"] is True

    # 6. HTML page routes
    res_index = client.get("/card")
    assert res_index.status_code == 200
    assert "角色卡模式" in res_index.get_data(as_text=True)

    res_chat = client.get("/card/test_hanako")
    assert res_chat.status_code == 200
    assert "card.js" in res_chat.get_data(as_text=True)

    # 7. Test rewind
    # First inject 3 messages
    b_dir = os.path.join(stories_dir, "test_hanako", "branches", "main")
    msgs_path = os.path.join(b_dir, "messages.json")
    mock_history = [
        {"role": "gm", "content": "第一則", "index": 0, "state_snapshot": {"inventory": {"手電筒": ""}}},
        {"role": "user", "content": "第二則", "index": 1, "state_snapshot": {"inventory": {"手電筒": ""}}},
        {"role": "gm", "content": "第三則", "index": 2, "state_snapshot": {"inventory": {"手電筒": "", "餅乾": "1"}}, "pre_state_snapshot": {"inventory": {"手電筒": ""}}},
    ]
    with open(msgs_path, "w", encoding="utf-8") as f:
        json.dump(mock_history, f)

    from story_core import compaction
    compaction.save_recap("test_hanako", "main", {"recap_text": "opening recap", "compacted_through_index": 0})
    res_rewind = client.post("/api/card/test_hanako/rewind", json={"to_index": 0})
    assert res_rewind.status_code == 200
    assert compaction.load_recap("test_hanako", "main")["recap_text"] == "opening recap"
    with open(msgs_path, encoding="utf-8") as f:
        rewound_msgs = json.load(f)
    assert len(rewound_msgs) == 1
    assert rewound_msgs[0]["index"] == 0

    # 8. Test regenerate
    # Re-inject 3 messages
    with open(msgs_path, "w", encoding="utf-8") as f:
        json.dump(mock_history, f)

    monkeypatch.setattr("routes.card_routes.run_card_turn", lambda **kw: {"ok": True, "narration": "新第三則", "gm_index": 2})
    compaction.save_recap("test_hanako", "main", {"recap_text": "retained past", "compacted_through_index": 0})
    res_regen = client.post("/api/card/test_hanako/regenerate", json={})
    assert res_regen.status_code == 200
    assert compaction.load_recap("test_hanako", "main")["recap_text"] == "retained past"
    assert res_regen.get_json()["ok"] is True
    # Regeneration inside the covered prefix must invalidate it.
    monkeypatch.setattr("routes.card_routes.run_card_turn", lambda **kw: {"ok": True})
    with open(msgs_path, "w", encoding="utf-8") as f:
        json.dump(mock_history, f)
    compaction.save_recap("test_hanako", "main", {"recap_text": "old ending", "compacted_through_index": 2})
    assert client.post("/api/card/test_hanako/regenerate", json={}).status_code == 200
    assert compaction.load_recap("test_hanako", "main")["recap_text"] == ""

    # Rewind cutting summarized history and reset both clear it.
    with open(msgs_path, "w", encoding="utf-8") as f:
        json.dump(mock_history, f)
    compaction.save_recap("test_hanako", "main", {"recap_text": "future", "compacted_through_index": 1})
    assert client.post("/api/card/test_hanako/rewind", json={"to_index": 0}).status_code == 200
    assert compaction.load_recap("test_hanako", "main")["recap_text"] == ""
    compaction.save_recap("test_hanako", "main", {"recap_text": "opening", "compacted_through_index": 0})
    assert client.post("/api/card/test_hanako/reset").status_code == 200
    assert compaction.load_recap("test_hanako", "main")["recap_text"] == ""



def test_card_fetishes_and_sensitive_spots(sample_card):
    """Test validation and prompt assembly for fetishes and sensitive spots."""
    from story_core.card_mode.prompt import build_card_system_prompt

    card = copy.deepcopy(sample_card)
    card["persona"]["fetishes"] = ["渴望被摸後頸", "霸道命令"]
    card["persona"]["sensitive_spots"] = ["後頸（發軟輕喘）", "腰側軟肉"]

    # 1. Validation should succeed
    errors = validate_card(card)
    assert not errors

    # 2. Invalid types should fail
    bad_card = copy.deepcopy(card)
    bad_card["persona"]["fetishes"] = "not a list"
    assert any("fetishes" in e for e in validate_card(bad_card))

    bad_card2 = copy.deepcopy(card)
    bad_card2["persona"]["sensitive_spots"] = [123]
    assert any("sensitive_spots" in e for e in validate_card(bad_card2))

    # 3. Prompt assembly contains fetishes and sensitive_spots
    prompt = build_card_system_prompt(
        card=card,
        state={"inventory": {}},
        npc={"relationship": {"tier": "陌生", "note": ""}, "memory": []},
        clock_dt=datetime(2026, 5, 16, 2, 8),
    )
    assert "**性癖與偏好情境（Fetishes）**：渴望被摸後頸、霸道命令" in prompt
    assert "**生理敏感點與弱點（Sensitive Spots）**：後頸（發軟輕喘）、腰側軟肉" in prompt
    # Card details remain available without mandatory escalation.
    assert "不是必然觸發激烈反應的開關" in prompt
    assert "身體反應不代表同意" in prompt




def test_suman_appearance_reaches_roleplay_prompt():
    """The real design card supplies its appearance to the generation prompt."""
    from pathlib import Path
    from story_core.card_mode.prompt import build_card_system_prompt

    design = Path(__file__).resolve().parents[1] / "story_design"
    card = load_card("suman", design_dir=str(design))
    prompt = build_card_system_prompt(
        card, {"inventory": {}}, {"relationship": card["start"]["relationship"]},
        datetime(2026, 5, 16, 23, 45),
    )
    assert card["persona"]["appearance"] in prompt
    assert all(detail in prompt for detail in card["persona"]["body_details"])
    assert "27 歲" in prompt
    for stale in ("身形曼妙修長", "鎖骨精緻深陷", "纖細的腰肢", "白皙纖細的脖頸"):
        assert stale not in prompt + card["opening"]


@pytest.mark.parametrize("regenerate", [False, True])
def test_card_turn_keeps_latest_reply_in_provider_payload(tmp_path, sample_card, monkeypatch, regenerate):
    from story_core.card_mode.pipeline import run_card_turn
    from story_core.gemini_bridge import _build_contents
    from story_core.story_io import _upsert_branch_message

    stories_dir = str(tmp_path / "stories")
    design_dir = str(tmp_path / "design")
    registry = tmp_path / "stories.json"
    registry.write_text('{"stories": {}}')
    monkeypatch.setattr("story_core.story_io.STORIES_REGISTRY_PATH", str(registry))
    monkeypatch.setattr("story_core.story_io.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("story_core.story_io.STORIES_DIR", stories_dir)
    monkeypatch.setattr("story_core.card_mode.card.STORY_DESIGN_DIR", design_dir)
    init_card_story(sample_card, stories_dir=stories_dir, design_dir=design_dir)
    previous = "她接過水杯，坐在椅子上喝了一口。"
    current = "水溫可以嗎？"
    for m in [
        {"role": "user", "content": "遞給你一杯水。", "index": 1},
        {"role": "gm", "content": previous, "index": 2},
    ]:
        _upsert_branch_message(sample_card["id"], "main", m)
    if regenerate:
        _upsert_branch_message(sample_card["id"], "main", {"role": "user", "content": current, "index": 3})

    def inspect_request(user, system, recent):
        assert "約 3000 字" in system
        contents = _build_contents(recent, user)
        assert contents[-2] == {"role": "model", "parts": [{"text": previous}]}
        assert contents[-1] == {"role": "user", "parts": [{"text": current}]}
        assert sum(m["parts"][0]["text"].count(current) for m in contents) == 1
        return "她捧著杯子點頭：「剛剛好。」"

    recap_call = {}
    monkeypatch.setattr("story_core.compaction.should_compact", lambda *a: True)
    monkeypatch.setattr("story_core.compaction.compact_async", lambda *a, **k: recap_call.update(k))
    result = run_card_turn(
        sample_card["id"], user_message=current, llm_fn=inspect_request,
        recorder_fn=lambda *args: None, async_recorder=False,
        skip_user_message=regenerate, response_length="long",
    )
    assert result["ok"]

    from story_core.card_mode.recap import COMPACT_PROMPT, META_COMPACT_PROMPT
    assert recap_call == dict(prompt_template=COMPACT_PROMPT, meta_prompt_template=META_COMPACT_PROMPT,
                              max_message_chars=None, batch_chars=60000, recap_version=2)


@pytest.mark.parametrize('choice,expected', [('short','200-500'),('medium','500-1000'),('long','3000')])
def test_response_length_controls_prompt(sample_card, choice, expected):
    from story_core.card_mode.prompt import build_card_system_prompt
    prompt = build_card_system_prompt(sample_card, {}, {}, datetime(2026,5,16), response_length=choice)
    assert f'約 {expected} 字' in prompt
    assert '不是最低要求' not in prompt
    assert '不代寫玩家行動' in prompt


@pytest.mark.parametrize('endpoint', ['send/stream','regenerate'])
@pytest.mark.parametrize('bad', ['extra-long', [], 1000])
def test_invalid_length_rejected_before_card_mutation(endpoint, bad, monkeypatch):
    from app import app
    def unexpected(*a, **k):
        pytest.fail('Invalid request must not access or mutate dialogue')
    monkeypatch.setattr('routes.card_routes._load_branch_messages', unexpected)
    monkeypatch.setattr('routes.card_routes.run_card_turn', unexpected)
    with app.test_client() as client:
        response = client.post(f'/api/card/test/{endpoint}', json={'message':'你好','response_length':bad})
    assert response.status_code == 400


@pytest.mark.parametrize('length', ['short','medium','long'])
def test_sensory_contract_and_length_coexist(sample_card, length):
    from story_core.card_mode.prompt import build_card_system_prompt, RESPONSE_LENGTHS
    prompt = build_card_system_prompt(sample_card, {}, {}, datetime(2026,5,16), response_length=length)
    assert '感官細節要有來源與後續' in prompt
    assert '鞋履與裸足的生活細節' in prompt
    assert '鞋襪遮住的細節不能直接看見' in prompt
    assert '不每輪插入' in prompt
    assert '不假定玩家的感受' in prompt
    assert '沉默、哭泣、僵住、未反抗或身體反應不代表同意' in prompt
    assert f'約 {RESPONSE_LENGTHS[length]} 字' in prompt


@pytest.mark.parametrize('profile', ['', '艾迪，30 歲，在新加坡擔任工程師。'])
def test_free_text_player_profile(sample_card, profile):
    from story_core.card_mode.prompt import build_card_system_prompt
    sample_card['player']['profile'] = profile
    prompt = build_card_system_prompt(sample_card, {}, {}, datetime(2026,9,11))
    assert ('**玩家檔案（自由文字）**' in prompt) == bool(profile)
    if profile:
        assert profile in prompt
        assert '未提供的玩家資料保持未知' in prompt


def test_aila_card_and_opening():
    from pathlib import Path
    design = Path(__file__).resolve().parents[1] / 'story_design'
    card = load_card('aila', design_dir=str(design))
    assert validate_card(card) == []
    assert '艾迪，30 歲' in card['player']['profile']
    assert '26 歲' in card['persona']['age_note']
    assert 180 <= len(''.join(card['opening'].split())) <= 220


def test_clock_explicit_week_jump_and_stale_date(tmp_path, monkeypatch):
    from story_core.card_mode.clock import save_clock
    monkeypatch.setattr("story_core.card_mode.clock._clock_path", lambda s, b: str(tmp_path / "clock.json"))
    save_clock("s", "main", {"start_iso": "2026-09-14T12:10:00", "elapsed_minutes": 0})
    assert sync_from_narration("s", "main", "📅 2026/9/21 ⏰ 18:40 📌 圖書館 🌆 傍晚")
    assert now("s") == datetime(2026, 9, 21, 18, 40)
    assert load_clock("s")["last_location"] == "圖書館"
    assert load_clock("s")["last_weather"] == "傍晚"
    assert sync_from_narration("s", "main", "📅 2026/9/20 ⏰ 18:40 📌 校園 🌤️ 晴\n明天再說。")
    assert now("s") == datetime(2026, 9, 21, 18, 40)
    assert load_clock("s")["last_location"] == "圖書館"


@pytest.mark.parametrize("header", [
    "📅 2026/5/16 ⏰ 02:25 📌 圖書館 🌤️ 陰",
    "2026/5/16（週六）02:25 ｜ 圖書館 ｜ 陰",
])
def test_recorder_cannot_double_advance_header_time(tmp_path, sample_card, monkeypatch, header):
    from story_core.card_mode.pipeline import run_card_turn
    monkeypatch.setattr("story_core.story_io.STORIES_REGISTRY_PATH", str(tmp_path / "stories.json"))
    monkeypatch.setattr("story_core.story_io.DATA_DIR", str(tmp_path))
    monkeypatch.setattr("story_core.story_io.STORIES_DIR", str(tmp_path / "stories"))
    monkeypatch.setattr("story_core.card_mode.card.STORY_DESIGN_DIR", str(tmp_path / "design"))
    init_card_story(sample_card, stories_dir=str(tmp_path / "stories"), design_dir=str(tmp_path / "design"))
    prompts = []
    def recorder(**kwargs):
        prompts.append(kwargs["system_prompt"])
        kwargs["on_tool"]("advance_time", {"minutes": 17})
        kwargs["on_tool"]("advance_time", {"minutes": 17})
    monkeypatch.setattr("story_core.gemini_bridge.call_gemini_gm_tools", recorder)
    def generate(user, prompt, recent):
        prompts.append(prompt)
        return header + "\n她把書放回書架。"
    for _ in range(2):
        result = run_card_turn("test_hanako", user_message="繼續看書", llm_fn=generate, async_recorder=False)
        assert result["ok"]
        assert load_clock("test_hanako")["elapsed_minutes"] == 17
    assert "圖書館" in prompts[2]
    assert "02:25" in prompts[1]
    messages = json.loads((tmp_path / "stories/test_hanako/branches/main/messages.json").read_text())
    assert messages[-1]["clock_snapshot"]["last_location"] == "圖書館"
    assert messages[-1]["clock_snapshot"]["elapsed_minutes"] == 17


def test_initial_background_is_starting_context_not_current_relationship(sample_card):
    from story_core.card_mode.prompt import build_card_system_prompt
    sample_card['setting']['initial_background'] = '開場時與王凱交往。'
    prompt = build_card_system_prompt(sample_card, {}, {'memory':['已與王凱分手。']}, datetime(2026,9,21))
    assert '開場時與王凱交往。' in prompt
    assert '已與王凱分手。' in prompt
    assert '以當前劇情與記憶為準，不重置成開場狀態' in prompt
    assert '不代表艾迪已知悉他人的私事' in prompt


def test_disabled_relationship_note_not_in_narration(sample_card):
    from story_core.card_mode.prompt import build_card_system_prompt, relationship_display
    sample_card['relationship_note_enabled'] = False
    rel = {'tier':'相識', 'note':'EXCLUDED_RELATION_NOTE'}
    prompt = build_card_system_prompt(sample_card, {'relationship_note':'EXCLUDED_RELATION_NOTE'},
                                     {'relationship':rel}, datetime(2026,9,21))
    assert 'EXCLUDED_RELATION_NOTE' not in prompt
    assert relationship_display(sample_card, rel) == '相識'
    assert rel['note'] == 'EXCLUDED_RELATION_NOTE'


def test_disabled_relationship_omits_entire_grounding(sample_card):
    from story_core.card_mode.prompt import build_card_system_prompt
    sample_card['relationship_enabled'] = False
    prompt = build_card_system_prompt(sample_card, {},
             {'relationship':{'tier':'摯友','note':'UNUSED_NOTE'},'memory':['昨天一起吃飯']}, datetime(2026,9,21))
    assert '## 與你的關係' not in prompt
    assert '摯友' not in prompt and 'UNUSED_NOTE' not in prompt
    assert '昨天一起吃飯' in prompt


def test_unlimited_length_omits_card_default_and_word_target(sample_card):
    from story_core.card_mode.prompt import build_card_system_prompt
    from datetime import datetime
    sample_card.setdefault('output', {})['length_hint'] = '123-456'
    prompt = build_card_system_prompt(sample_card, {}, {}, datetime(2026, 5, 16), response_length='unlimited')
    assert '123-456' not in prompt
    assert '本回合建議敘事長度' not in prompt
    assert '所選字數範圍' not in prompt
