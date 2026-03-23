from story_core import llm_bridge


def test_call_claude_gm_dispatches_codex_agent(monkeypatch):
    monkeypatch.setattr(
        llm_bridge,
        "_get_config",
        lambda: {"provider": "codex_agent", "codex_agent": {"model": "gpt-5.4"}},
    )
    monkeypatch.setattr(llm_bridge, "_provider_override", None)

    captured = {}

    def _fake_codex_gm(user_message, system_prompt, recent_messages, session_id=None, **kwargs):
        captured["user_message"] = user_message
        captured["system_prompt"] = system_prompt
        captured["recent_messages"] = recent_messages
        captured["session_id"] = session_id
        captured.update(kwargs)
        return ("GM reply", None)

    monkeypatch.setattr("story_core.codex_bridge.call_codex_gm", _fake_codex_gm)

    result = llm_bridge.call_claude_gm(
        "玩家行動",
        "system prompt",
        [{"role": "user", "content": "前文"}],
        session_id=None,
        story_id="story_x",
        branch_id="branch_y",
    )

    assert result == ("GM reply", None)
    assert captured["story_id"] == "story_x"
    assert captured["branch_id"] == "branch_y"
    assert captured["model"] == "gpt-5.4"


def test_call_claude_gm_stream_dispatches_codex_agent(monkeypatch):
    monkeypatch.setattr(
        llm_bridge,
        "_get_config",
        lambda: {"provider": "codex_agent", "codex_agent": {"model": "gpt-5.4"}},
    )
    monkeypatch.setattr(llm_bridge, "_provider_override", None)

    captured = {}

    def _fake_codex_stream(user_message, system_prompt, recent_messages, session_id=None, **kwargs):
        captured["user_message"] = user_message
        captured["system_prompt"] = system_prompt
        captured["recent_messages"] = recent_messages
        captured["session_id"] = session_id
        captured.update(kwargs)
        yield ("text", "片段")
        yield ("done", {"response": "完整回應", "session_id": None, "usage": None})

    monkeypatch.setattr("story_core.codex_bridge.call_codex_gm_stream", _fake_codex_stream)

    events = list(
        llm_bridge.call_claude_gm_stream(
            "玩家行動",
            "system prompt",
            [{"role": "user", "content": "前文"}],
            session_id=None,
            story_id="story_x",
            branch_id="branch_y",
        )
    )

    assert events[0] == ("text", "片段")
    assert events[-1][0] == "done"
    assert captured["story_id"] == "story_x"
    assert captured["branch_id"] == "branch_y"
    assert captured["model"] == "gpt-5.4"


def test_call_oneshot_dispatches_codex_agent(monkeypatch):
    monkeypatch.setattr(
        llm_bridge,
        "_get_config",
        lambda: {"provider": "codex_agent", "codex_agent": {"model": "gpt-5.4"}},
    )
    monkeypatch.setattr(llm_bridge, "_provider_override", None)

    captured = {}

    def _fake_codex_oneshot(prompt, *, system_prompt=None, model=""):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt
        captured["model"] = model
        return "{\"ok\": true}"

    monkeypatch.setattr("story_core.codex_bridge.call_codex_oneshot", _fake_codex_oneshot)

    result = llm_bridge.call_oneshot("請輸出 JSON", system_prompt="你是抽取器")

    assert result == "{\"ok\": true}"
    assert captured["prompt"] == "請輸出 JSON"
    assert captured["system_prompt"] == "你是抽取器"
    assert captured["model"] == "gpt-5.4"
