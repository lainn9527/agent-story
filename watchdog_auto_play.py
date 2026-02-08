#!/usr/bin/env python3
"""Watchdog for auto-play agents. Run via cron every 10 minutes.

Strategy:
  1. Check if agents are running
  2. If down, try simple restart (clear flags + relaunch)
  3. If simple restart keeps failing (3+ consecutive watchdog restarts),
     escalate to Claude CLI for diagnosis and fix
  4. Log everything to data/watchdog.log
"""

import json
import os
import subprocess
import sys
from datetime import datetime

STORY_DIR = os.path.dirname(os.path.abspath(__file__))
BRANCHES_DIR = os.path.join(STORY_DIR, "data/stories/story_original/branches")
LOG_FILE = os.path.join(STORY_DIR, "data/watchdog.log")
CONFIG_FILE = os.path.join(STORY_DIR, "data/watchdog_config.json")
RESTART_TRACKER = os.path.join(STORY_DIR, "data/watchdog_restarts.json")
ESCALATE_THRESHOLD = 3  # escalate to Claude CLI after this many consecutive restarts

DEFAULT_AGENTS = [
    {"branch_id": "auto_18f8d831", "max_turns": 200, "provider": "claude_cli"},
    {"branch_id": "auto_a46a7941", "max_turns": 200, "provider": "claude_cli"},
]


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_agents():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)["agents"]
        except Exception as e:
            log(f"  Warning: failed to load config: {e}, using defaults")
    return DEFAULT_AGENTS


def load_restart_tracker():
    if os.path.exists(RESTART_TRACKER):
        try:
            with open(RESTART_TRACKER, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_restart_tracker(tracker):
    with open(RESTART_TRACKER, "w", encoding="utf-8") as f:
        json.dump(tracker, f, indent=2)


def is_running(branch_id):
    result = subprocess.run(
        ["pgrep", "-f", f"auto_play.*{branch_id}"],
        capture_output=True,
    )
    return result.returncode == 0


def get_state(branch_id):
    path = os.path.join(BRANCHES_DIR, branch_id, "auto_play_state.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(branch_id, state):
    path = os.path.join(BRANCHES_DIR, branch_id, "auto_play_state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_recent_log(branch_id, lines=50):
    log_path = os.path.join(STORY_DIR, f"data/auto_play_{branch_id}.log")
    if not os.path.exists(log_path):
        return "(no log file)"
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except Exception:
        return "(failed to read log)"


def simple_restart(branch_id, max_turns, provider):
    log_path = os.path.join(STORY_DIR, f"data/auto_play_{branch_id}.log")
    cmd = [
        sys.executable,
        os.path.join(STORY_DIR, "auto_play.py"),
        "--provider", provider,
        "--resume", "--branch-id", branch_id,
        "--max-turns", str(max_turns),
        "--max-errors", "10",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=open(log_path, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log(f"  Simple restart {branch_id} with {provider} (PID {proc.pid})")


def escalate_to_claude(branch_id, max_turns, provider):
    """Call Claude CLI to diagnose and generate a fix script, then execute it."""
    log(f"  ESCALATING {branch_id} to Claude CLI for diagnosis...")

    state = get_state(branch_id)
    recent_log = get_recent_log(branch_id)

    prompt = f"""你是一個自動化系統管理員。一個 auto-play RPG agent 反覆重啟失敗，需要你診斷並修復。

## 環境
- 工作目錄: {STORY_DIR}
- Python: {sys.executable}
- Branch: {branch_id}
- State 檔案: {BRANCHES_DIR}/{branch_id}/auto_play_state.json

## 目前 State
{json.dumps(state, indent=2, ensure_ascii=False) if state else "無法讀取"}

## 最近 Log（最後 50 行）
{recent_log}

## 任務
1. 分析上面的 state 和 log，找出反覆失敗的原因
2. 輸出一段可以直接執行的 bash 腳本來修復問題並重啟 agent
3. 重啟指令: {sys.executable} {STORY_DIR}/auto_play.py --provider {provider} --resume --branch-id {branch_id} --max-turns {max_turns} --max-errors 10

只輸出純 bash 腳本，不要 markdown code fence，不要解釋。腳本結尾用 nohup + & 背景執行重啟指令。"""

    try:
        result = subprocess.run(
            ["/Users/eddylai/.local/bin/claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0 or not result.stdout.strip():
            log(f"  Claude CLI failed (rc={result.returncode}): {result.stderr[:200]}")
            log(f"  Falling back to simple restart")
            simple_restart(branch_id, max_turns, provider)
            return

        fix_script = result.stdout.strip()
        # Remove markdown fences if Claude added them anyway
        if fix_script.startswith("```"):
            lines = fix_script.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            fix_script = "\n".join(lines)

        log(f"  Claude CLI fix script:\n{fix_script}")

        # Save fix script for audit
        fix_path = os.path.join(STORY_DIR, f"data/watchdog_fix_{branch_id}.sh")
        with open(fix_path, "w") as f:
            f.write(fix_script)

        # Execute the fix
        proc = subprocess.Popen(
            ["bash", fix_path],
            stdout=open(os.path.join(STORY_DIR, f"data/watchdog_fix_{branch_id}.log"), "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=STORY_DIR,
        )
        log(f"  Fix script running (PID {proc.pid})")

    except subprocess.TimeoutExpired:
        log(f"  Claude CLI timed out, falling back to simple restart")
        simple_restart(branch_id, max_turns, provider)
    except FileNotFoundError:
        log(f"  Claude CLI not found, falling back to simple restart")
        simple_restart(branch_id, max_turns, provider)
    except Exception as e:
        log(f"  Claude CLI error: {e}, falling back to simple restart")
        simple_restart(branch_id, max_turns, provider)


def check_and_restart(agent, tracker):
    bid = agent["branch_id"]
    provider = agent.get("provider", "claude_cli")
    max_turns = agent.get("max_turns", 200)

    if is_running(bid):
        state = get_state(bid)
        turn = state["turn"] if state else "?"
        log(f"  {bid}: running (turn {turn})")
        # Reset restart counter on success
        tracker[bid] = 0
        return True

    # Not running — check why
    state = get_state(bid)
    if state is None:
        log(f"  {bid}: no state file found, skipping")
        return False

    turn = state.get("turn", 0)

    # Finished all turns? Extend and keep going
    if turn >= max_turns:
        new_max = max_turns + 200
        log(f"  {bid}: reached {turn}/{max_turns} turns, extending to {new_max} and restarting")
        agent["max_turns"] = new_max
        simple_restart(bid, new_max, provider)
        return False

    # Track consecutive watchdog restarts
    restart_count = tracker.get(bid, 0) + 1
    tracker[bid] = restart_count

    # Clear blocking flags
    dirty = False
    if state.get("death_detected"):
        log(f"  {bid}: clearing death_detected (turn {turn})")
        state["death_detected"] = False
        dirty = True

    errors = state.get("consecutive_errors", 0)
    if errors > 0:
        log(f"  {bid}: clearing {errors} consecutive_errors (turn {turn})")
        state["consecutive_errors"] = 0
        dirty = True

    if dirty:
        save_state(bid, state)

    # Decide: simple restart or escalate
    if restart_count >= ESCALATE_THRESHOLD:
        log(f"  {bid}: DOWN at turn {turn}, {restart_count} consecutive watchdog restarts -> ESCALATE")
        escalate_to_claude(bid, max_turns, provider)
        # Reset counter after escalation to give it a fresh chance
        tracker[bid] = 0
    else:
        log(f"  {bid}: DOWN at turn {turn}, attempt {restart_count}/{ESCALATE_THRESHOLD}")
        simple_restart(bid, max_turns, provider)

    return False


def main():
    log("=== Watchdog check ===")

    # Remove stop file if it exists (allow restart)
    stop_file = os.path.join(STORY_DIR, "STOP_AUTO_PLAY")
    if os.path.exists(stop_file):
        log(f"  STOP file detected — skipping (touch STOP_AUTO_PLAY to pause watchdog)")
        return

    tracker = load_restart_tracker()
    agents = load_agents()
    all_ok = True
    for agent in agents:
        if not check_and_restart(agent, tracker):
            all_ok = False

    save_restart_tracker(tracker)

    if all_ok:
        log("  All agents OK")


if __name__ == "__main__":
    main()
