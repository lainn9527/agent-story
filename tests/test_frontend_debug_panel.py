"""Regression tests for Debug Panel frontend parsing helpers."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


APP_JS_PATH = Path(__file__).resolve().parents[1] / "static" / "app.js"


def _run_debug_panel_expr(expr: str):
    if shutil.which("node") is None:
        pytest.skip("node is required for frontend parser regression tests")

    script = f"""
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync({json.dumps(str(APP_JS_PATH))}, "utf8");
const start = src.indexOf("const _DEBUG_ACTION_TYPES = new Set([");
const end = src.indexOf("function _renderDebugChatMessages");
if (start < 0 || end < 0 || end <= start) {{
  throw new Error("debug panel parser snippet markers not found");
}}

const snippet = src.slice(start, end);
const ctx = {{ console }};
vm.createContext(ctx);
vm.runInContext(snippet, ctx);

const result = {expr};
process.stdout.write(JSON.stringify(result));
"""

    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=APP_JS_PATH.parents[1],
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def test_parse_debug_content_normalizes_nested_state_patch_alias():
    text = (
        "先做檢查。\n"
        "<!--DEBUG_ACTION "
        '{"state_patch":{"inventory":{"阿豪":"深度休眠修復中"},"current_status":"閉關整理中"}}'
        " DEBUG_ACTION-->\n"
        "<!--DEBUG_DIRECTIVE "
        '{"instruction":"下一回合沿用新的技能描述"}'
        " DEBUG_DIRECTIVE-->"
    )

    parsed = _run_debug_panel_expr(f"ctx._parseDebugContent({json.dumps(text)})")

    assert parsed["cleanText"] == "先做檢查。"
    assert parsed["proposals"] == [
        {"type": "state_patch", "update": {"inventory": {"阿豪": "深度休眠修復中"}}},
        {"type": "state_patch", "update": {"current_status": "閉關整理中"}},
    ]
    assert parsed["directives"] == [{"instruction": "下一回合沿用新的技能描述"}]


def test_split_debug_proposals_normalizes_patch_alias():
    proposals = [
        {"action": "state_patch", "patch": {"reward_points_delta": 7}},
    ]

    parsed = _run_debug_panel_expr(f"ctx._splitDebugProposals({json.dumps(proposals, ensure_ascii=False)})")

    assert parsed == [
        {"type": "state_patch", "update": {"reward_points_delta": 7}},
    ]
