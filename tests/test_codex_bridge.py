import json
import shutil
from pathlib import Path

from story_core import codex_bridge


def test_build_gm_workspace_sanitizes_full_timeline(monkeypatch):
    monkeypatch.setattr(
        codex_bridge,
        "_load_tree",
        lambda _story_id: {"branches": {"branch_x": {"id": "branch_x", "name": "測試分支"}}},
    )
    monkeypatch.setattr(
        codex_bridge,
        "get_full_timeline",
        lambda _story_id, _branch_id: [
            {
                "role": "gm",
                "index": 1,
                "content": "前文敘事\n\n**你打算：**\n1. 繼續前進\n2. 先撤退",
            },
            {"role": "user", "index": 2, "content": "還記得嗎"},
        ],
    )
    monkeypatch.setattr(codex_bridge, "load_recap", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(codex_bridge, "_load_gm_plan", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(codex_bridge, "_load_branch_lore", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(codex_bridge, "get_fate_mode", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(codex_bridge, "_story_dir", lambda _story_id: "/tmp/story")
    monkeypatch.setattr(codex_bridge, "_story_design_dir", lambda _story_id: "/tmp/design")
    monkeypatch.setattr(
        codex_bridge,
        "_story_character_state_path",
        lambda _story_id, _branch_id: "/tmp/character_state.json",
    )
    monkeypatch.setattr(
        codex_bridge,
        "_story_npcs_path",
        lambda _story_id, _branch_id: "/tmp/npcs.json",
    )
    monkeypatch.setattr(
        codex_bridge,
        "_dungeon_progress_path",
        lambda _story_id, _branch_id: "/tmp/dungeon_progress.json",
    )
    monkeypatch.setattr(
        codex_bridge,
        "_story_character_schema_path",
        lambda _story_id: "/tmp/character_schema.json",
    )

    def _fake_load_json(path, default=None):
        if path.endswith("character_state.json"):
            return {"current_phase": "主神空間"}
        if path.endswith("npcs.json"):
            return []
        if path.endswith("dungeon_progress.json"):
            return {}
        if path.endswith("character_schema.json"):
            return {"fields": []}
        if path.endswith("world_lore.json"):
            return []
        return default if default is not None else []

    monkeypatch.setattr(codex_bridge, "_load_json", _fake_load_json)

    workspace = codex_bridge._build_gm_workspace(
        story_id="story_x",
        branch_id="branch_x",
        user_message="玩家新行動",
        system_prompt="system prompt",
        recent_messages=[{"role": "user", "content": "recent"}],
    )
    try:
        payload = json.loads((Path(workspace) / "FULL_TIMELINE.json").read_text(encoding="utf-8"))
        assert payload[0]["content"] == "前文敘事"
        assert "你打算" not in payload[0]["content"]
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_call_codex_gm_stream_emits_keepalive_then_response(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_build_gm_workspace", lambda **_kwargs: "/tmp/codex-test")
    monkeypatch.setattr(codex_bridge.shutil, "rmtree", lambda *_args, **_kwargs: None)

    class _FakeStdout:
        def __init__(self, lines):
            self._lines = list(lines)

        def fileno(self):
            return 1

        def readline(self):
            if self._lines:
                return self._lines.pop(0)
            return ""

        @property
        def has_pending(self):
            return bool(self._lines)

    class _FakeStderr:
        def read(self):
            return ""

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeStdout(
                [
                    '{"type":"thread.started","thread_id":"t1"}\n',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"最終回覆"}}\n',
                    '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":34}}\n',
                ]
            )
            self.stderr = _FakeStderr()
            self.returncode = None

        def poll(self):
            if self.stdout.has_pending:
                return None
            return 0

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    fake_proc = _FakeProc()
    monkeypatch.setattr(codex_bridge.subprocess, "Popen", lambda *args, **kwargs: fake_proc)

    select_results = iter(
        [
            ([], [], []),
            ([fake_proc.stdout], [], []),
            ([fake_proc.stdout], [], []),
            ([fake_proc.stdout], [], []),
            ([], [], []),
        ]
    )
    monkeypatch.setattr(codex_bridge.select, "select", lambda *args, **kwargs: next(select_results))

    events = list(
        codex_bridge.call_codex_gm_stream(
            "玩家新行動",
            "system prompt",
            [{"role": "user", "content": "recent"}],
            story_id="story_x",
            branch_id="branch_x",
            model="gpt-5.4",
        )
    )

    assert events[0] == ("text", "")
    assert ("text", "最終回覆") in events
    assert events[-1] == (
        "done",
        {
            "response": "最終回覆",
            "session_id": None,
            "usage": {"input_tokens": 12, "output_tokens": 34},
        },
    )
