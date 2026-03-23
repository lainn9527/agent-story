"""Bridge to Codex CLI in a read-only, branch-scoped workspace."""

from __future__ import annotations

import json
import logging
import os
import select
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from story_core.branch_tree import get_full_timeline
from story_core.compaction import load_recap
from story_core.gm_plan import _load_gm_plan
from story_core.gm_cheats import get_fate_mode
from story_core.lore_helpers import _load_branch_lore
from story_core.tag_extraction import _sanitize_recent_messages
from story_core.story_io import (
    _dungeon_progress_path,
    _load_json,
    _load_tree,
    _story_dir,
    _story_character_state_path,
    _story_character_schema_path,
    _story_design_dir,
    _story_npcs_path,
)

log = logging.getLogger("rpg")

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_TIMEOUT = 300
DEFAULT_CODEX_MODEL = "gpt-5.4"
STREAM_KEEPALIVE_S = 8.0
_ENV = dict(os.environ)


def get_last_usage() -> dict | None:
    """Codex CLI token usage is not surfaced through this bridge."""
    return None


def _write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _recent_messages_text(recent_messages: list[dict]) -> str:
    if not recent_messages:
        return "（無最近訊息）\n"
    chunks: list[str] = []
    for msg in recent_messages:
        role = "玩家" if msg.get("role") == "user" else "GM"
        idx = msg.get("index")
        if idx is None:
            chunks.append(f"【{role}】\n{msg.get('content', '')}")
        else:
            chunks.append(f"【{role} #{idx}】\n{msg.get('content', '')}")
    return "\n\n".join(chunks) + "\n"


def _chunk_text(text: str, max_chars: int = 120):
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_chars:
            yield remaining
            return
        cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = remaining.rfind("。", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        chunk = remaining[:cut].strip()
        if chunk:
            yield chunk
        remaining = remaining[cut:].lstrip()


def _build_generic_workspace(
    task_text: str,
    system_prompt: str | None = None,
) -> str:
    workspace = tempfile.mkdtemp(prefix="codex-oneshot-")
    root = Path(workspace)
    if system_prompt:
        _write_text(root / "SYSTEM_PROMPT.txt", system_prompt)
    _write_text(root / "TASK.md", task_text)
    return workspace


def _build_gm_workspace(
    *,
    story_id: str | None,
    branch_id: str | None,
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
) -> str:
    workspace = tempfile.mkdtemp(prefix="codex-gm-")
    root = Path(workspace)

    _write_text(root / "SYSTEM_PROMPT.txt", system_prompt)
    _write_text(root / "PLAYER_MESSAGE.txt", user_message)
    _write_json(root / "RECENT_MESSAGES.json", recent_messages)
    _write_text(root / "RECENT_MESSAGES.txt", _recent_messages_text(recent_messages))

    available_files = [
        "SYSTEM_PROMPT.txt",
        "PLAYER_MESSAGE.txt",
        "RECENT_MESSAGES.json",
        "RECENT_MESSAGES.txt",
    ]

    if story_id and branch_id:
        design_root = Path(_story_design_dir(story_id))
        tree = _load_tree(story_id)
        branch_meta = tree.get("branches", {}).get(branch_id, {})
        strip_fate = not get_fate_mode(_story_dir(story_id), branch_id)
        timeline = _sanitize_recent_messages(
            get_full_timeline(story_id, branch_id),
            strip_fate=strip_fate,
        )
        recap = load_recap(story_id, branch_id)
        gm_plan = _load_gm_plan(story_id, branch_id)
        branch_lore = _load_branch_lore(story_id, branch_id)
        world_lore = _load_json(str(design_root / "world_lore.json"), [])
        character_state = _load_json(_story_character_state_path(story_id, branch_id), {})
        npcs = _load_json(_story_npcs_path(story_id, branch_id), [])
        dungeon_progress = _load_json(_dungeon_progress_path(story_id, branch_id), {})
        schema = _load_json(_story_character_schema_path(story_id), {})

        _write_json(root / "BRANCH_META.json", branch_meta)
        _write_json(root / "FULL_TIMELINE.json", timeline)
        _write_json(root / "CHARACTER_STATE.json", character_state)
        _write_json(root / "NPCS.json", npcs)
        _write_json(root / "CONVERSATION_RECAP.json", recap)
        _write_json(root / "GM_PLAN.json", gm_plan)
        _write_json(root / "BRANCH_LORE.json", branch_lore)
        _write_json(root / "WORLD_LORE.json", world_lore)
        _write_json(root / "DUNGEON_PROGRESS.json", dungeon_progress)
        _write_json(root / "CHARACTER_SCHEMA.json", schema)

        available_files.extend(
            [
                "BRANCH_META.json",
                "FULL_TIMELINE.json",
                "CHARACTER_STATE.json",
                "NPCS.json",
                "CONVERSATION_RECAP.json",
                "GM_PLAN.json",
                "BRANCH_LORE.json",
                "WORLD_LORE.json",
                "DUNGEON_PROGRESS.json",
                "CHARACTER_SCHEMA.json",
            ]
        )

    task = f"""\
你是文字 RPG 的 GM。你目前在一個只讀工作區中工作。

你必須先閱讀下列檔案：
- `SYSTEM_PROMPT.txt`
- `PLAYER_MESSAGE.txt`
- `RECENT_MESSAGES.json`

如果你需要更長的 continuity、舊副本回憶、NPC 歷史、分支 lore 或副本狀態，才去閱讀其他 JSON 檔。
工作區可用檔案：
{os.linesep.join(f"- `{name}`" for name in available_files)}

規則：
1. 完全遵守 `SYSTEM_PROMPT.txt` 的規則與輸出契約。
2. 視工作區內容為本回合唯一可讀的遊戲上下文；不要假設工作區外的 branch/runtime 資料。
3. 只輸出最終 GM 回覆文字本身。
4. 不要輸出解釋、前言、分析、Markdown code fence，或提到你讀了哪些檔案。
5. 不要修改任何檔案。
"""
    _write_text(root / "TASK.md", task)
    return workspace


def _run_codex_task(*, workspace: str, model: str) -> str:
    output_path = os.path.join(workspace, "codex_last_message.txt")
    cmd = _build_codex_cmd(model=model, output_path=output_path)

    log.info("    codex_bridge: calling CLI cwd=%s model=%s", workspace, model or DEFAULT_CODEX_MODEL)
    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CODEX_TIMEOUT,
            cwd=workspace,
            env=_ENV,
        )
    except subprocess.TimeoutExpired:
        log.info("    codex_bridge: TIMEOUT after %ss", CODEX_TIMEOUT)
        return "【系統錯誤】Codex 回應逾時，請稍後再試。"
    except FileNotFoundError:
        log.info("    codex_bridge: codex CLI not found")
        return "【系統錯誤】找不到 codex CLI。請確認已安裝 Codex。"
    except Exception as exc:
        log.info("    codex_bridge: EXCEPTION %s", exc)
        return f"【系統錯誤】{exc}"

    elapsed = time.time() - t0
    response_text = ""
    if os.path.exists(output_path):
        try:
            response_text = Path(output_path).read_text(encoding="utf-8").strip()
        except Exception:
            response_text = ""

    if result.returncode == 0 and response_text:
        log.info("    codex_bridge: OK in %.1fs response_len=%d", elapsed, len(response_text))
        return response_text

    stderr_text = (result.stderr or "").strip()
    stdout_text = (result.stdout or "").strip()
    detail = stderr_text or stdout_text or f"Codex process exited with code {result.returncode}"
    log.info("    codex_bridge: FAILED in %.1fs rc=%d detail=%s", elapsed, result.returncode, detail[:160])
    return f"【系統錯誤】Codex 回應失敗：{detail}"


def _build_codex_cmd(*, model: str, output_path: str | None = None, json_stream: bool = False) -> list[str]:
    cmd = [
        CODEX_BIN,
        "-a",
        "never",
        "exec",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--ephemeral",
        "-m",
        model or DEFAULT_CODEX_MODEL,
    ]
    if json_stream:
        cmd.append("--json")
    elif output_path:
        cmd.extend(["-o", output_path])
    cmd.append("Read TASK.md and follow it exactly.")
    return cmd


def call_codex_gm(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
    *,
    story_id: str | None = None,
    branch_id: str | None = None,
    model: str = DEFAULT_CODEX_MODEL,
) -> tuple[str, str | None]:
    """Run Codex GM in a read-only branch-scoped workspace."""
    del session_id
    workspace = _build_gm_workspace(
        story_id=story_id,
        branch_id=branch_id,
        user_message=user_message,
        system_prompt=system_prompt,
        recent_messages=recent_messages,
    )
    try:
        return _run_codex_task(workspace=workspace, model=model), None
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def call_codex_gm_stream(
    user_message: str,
    system_prompt: str,
    recent_messages: list[dict],
    session_id: str | None = None,
    *,
    story_id: str | None = None,
    branch_id: str | None = None,
    model: str = DEFAULT_CODEX_MODEL,
    tools: list[dict] | None = None,
):
    """Streaming wrapper for Codex GM using `codex exec --json`.

    Codex CLI does not currently provide token-by-token deltas for plain text
    output, so we forward any completed agent text as soon as it appears and
    emit empty keepalive chunks while the subprocess is still running.
    """
    del tools, session_id
    workspace = _build_gm_workspace(
        story_id=story_id,
        branch_id=branch_id,
        user_message=user_message,
        system_prompt=system_prompt,
        recent_messages=recent_messages,
    )
    try:
        cmd = _build_codex_cmd(model=model, json_stream=True)
        log.info("    codex_bridge_stream: calling CLI cwd=%s model=%s", workspace, model or DEFAULT_CODEX_MODEL)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=workspace,
            env=_ENV,
        )
    except FileNotFoundError:
        shutil.rmtree(workspace, ignore_errors=True)
        log.info("    codex_bridge_stream: codex CLI not found")
        yield ("error", "找不到 codex CLI。請確認已安裝 Codex。")
        return
    except Exception as exc:
        shutil.rmtree(workspace, ignore_errors=True)
        log.info("    codex_bridge_stream: Popen EXCEPTION %s", exc)
        yield ("error", str(exc))
        return

    response_text = ""
    usage = None
    last_emit_at = time.time()

    try:
        stdout = proc.stdout
        assert stdout is not None
        while True:
            ready, _, _ = select.select([stdout], [], [], STREAM_KEEPALIVE_S)
            if not ready:
                if proc.poll() is not None:
                    break
                last_emit_at = time.time()
                yield ("text", "")
                continue

            raw_line = stdout.readline()
            if not raw_line:
                if proc.poll() is not None:
                    break
                continue

            line = raw_line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = data.get("type")
            if event_type in {"thread.started", "turn.started"}:
                if time.time() - last_emit_at >= 0.1:
                    last_emit_at = time.time()
                    yield ("text", "")
                continue

            if event_type == "item.completed":
                item = data.get("item", {}) or {}
                if item.get("type") == "agent_message":
                    text = str(item.get("text", "")).strip()
                    if text and text != response_text:
                        delta = text[len(response_text):] if text.startswith(response_text) else text
                        response_text = text
                        if delta:
                            last_emit_at = time.time()
                            yield ("text", delta)
                continue

            if event_type == "turn.completed":
                usage = data.get("usage")
                continue

            if event_type == "error":
                message = ""
                err = data.get("error")
                if isinstance(err, dict):
                    message = str(err.get("message", "")).strip()
                if not message:
                    message = str(data.get("message", "")).strip() or "Codex 執行失敗"
                yield ("error", message)
                return

        proc.wait(timeout=10)
        if proc.returncode != 0 and not response_text:
            stderr_text = proc.stderr.read().strip() if proc.stderr else ""
            error_msg = stderr_text or f"Codex process exited with code {proc.returncode}"
            yield ("error", error_msg)
            return

        if not response_text:
            yield ("error", "Codex 回傳空白回應")
            return

        yield ("done", {"response": response_text, "session_id": None, "usage": usage})
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        yield ("error", "Codex 回應逾時，請稍後再試。")
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        if response_text:
            yield ("done", {"response": response_text, "session_id": None, "usage": usage})
        else:
            yield ("error", str(exc))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def call_codex_oneshot(
    prompt: str,
    *,
    system_prompt: str | None = None,
    model: str = DEFAULT_CODEX_MODEL,
) -> str:
    """Run a one-shot Codex call in a blank read-only workspace."""
    task_parts = []
    if system_prompt:
        task_parts.append(
            "你必須遵守 `SYSTEM_PROMPT.txt`。\n"
            "先閱讀該檔案，再完成使用者要求。"
        )
    task_parts.append(
        "只輸出最終答案本身，不要輸出分析、前言、或 Markdown code fence。\n\n"
        "## 使用者要求\n"
        f"{prompt}"
    )
    workspace = _build_generic_workspace("\n\n".join(task_parts), system_prompt=system_prompt)
    try:
        return _run_codex_task(workspace=workspace, model=model)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
