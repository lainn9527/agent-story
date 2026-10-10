"""Unit tests for the OpenRouter Chat Completions bridge."""

import json

import pytest

from story_core import openrouter_bridge as bridge


def test_build_messages_replaces_trailing_user_and_preserves_assistant():
    messages = bridge._build_messages(
        [
            {"role": "user", "content": "older"},
            {"role": "gm", "content": "reply"},
            {"role": "user", "content": "duplicate current"},
        ],
        "current",
        "system",
    )
    assert messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "older"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "current"},
    ]


def test_request_body_disables_reasoning_by_default():
    body = bridge._request_body({}, "vendor/model", [{"role": "user", "content": "hi"}])
    assert body["reasoning"] == {"enabled": False}
    assert body["usage"] == {"include": True}


def test_normalize_usage_includes_cost_cache_and_reasoning():
    usage = bridge._normalize_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "cost": 0.00042,
            "prompt_tokens_details": {"cached_tokens": 6},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
        provider="test-provider",
    )
    assert usage == {
        "prompt_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "cached_tokens": 6,
        "reasoning_tokens": 2,
        "cost_usd": 0.00042,
        "upstream_provider": "test-provider",
    }


def test_tool_loop_executes_and_aggregates_usage(monkeypatch):
    responses = iter([
        {
            "provider": "first",
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13, "cost": 0.001},
            "choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "record_note", "arguments": json.dumps({"text": "hello"})},
            }]}}],
        },
        {
            "provider": "second",
            "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22, "cost": 0.002},
            "choices": [{"message": {"content": "done"}}],
        },
    ])
    bodies = []

    def fake_post(_cfg, body):
        bodies.append(body)
        return next(responses)

    monkeypatch.setattr(bridge, "_post_json", fake_post)
    calls = []
    text, executed, status = bridge.call_openrouter_gm_tools(
        "record it",
        "system",
        [],
        openrouter_cfg={"api_key": "not-used"},
        model="vendor/model",
        on_tool=lambda name, args: calls.append((name, args)) or {"ok": True},
        tool_decls=[{"name": "record_note", "description": "record", "parameters": {"type": "object"}}],
    )

    assert (text, status) == ("done", "ok")
    assert calls == [("record_note", {"text": "hello"})]
    assert executed[0]["result"] == {"ok": True}
    assert bodies[1]["messages"][-1]["role"] == "tool"
    usage = bridge.get_last_usage()
    assert usage["prompt_tokens"] == 30
    assert usage["total_tokens"] == 35
    assert usage["cost_usd"] == pytest.approx(0.003)
    assert usage["upstream_provider"] == "second"


class _StreamResponse:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return iter(self.lines)

    def __exit__(self, *_args):
        return False


def test_stream_yields_text_and_usage(monkeypatch):
    events = [
        {"choices": [{"delta": {"content": "O"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "K"}, "finish_reason": "stop"}]},
        {
            "provider": "stream-provider",
            "choices": [],
            "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9, "cost": 0.0002},
        },
    ]
    lines = [("data: " + json.dumps(event) + "\n").encode() for event in events]
    lines.append(b"data: [DONE]\n")
    monkeypatch.setattr(bridge, "_open_request", lambda *_args: _StreamResponse(lines))

    yielded = list(bridge.call_openrouter_gm_stream(
        "hi", "system", [], {"api_key": "not-used"}, "vendor/model",
    ))
    assert yielded[0:2] == [("text", "O"), ("text", "K")]
    assert yielded[-1][0] == "done"
    assert yielded[-1][1]["response"] == "OK"
    assert yielded[-1][1]["usage"]["cost_usd"] == pytest.approx(0.0002)
    assert bridge.get_last_usage()["upstream_provider"] == "stream-provider"
