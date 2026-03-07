"""Structured trace logging for LLM inputs/outputs.

Writes one JSON file per trace event to avoid unbounded single-log growth.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_PRUNE_INTERVAL_SECONDS = 600

_prune_lock = threading.Lock()
_last_prune_by_root: dict[str, float] = {}


def _safe_token(value: str | None, fallback: str) -> str:
    token = (value or "").strip()
    if not token:
        return fallback
    token = _SAFE_TOKEN_RE.sub("_", token).strip("._-")
    return token or fallback


def _atomic_write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _maybe_prune_old_traces(root: str, retention_days: int, now_utc: datetime):
    if retention_days <= 0:
        return
    now_ts = now_utc.timestamp()
    with _prune_lock:
        last = _last_prune_by_root.get(root, 0.0)
        if now_ts - last < _PRUNE_INTERVAL_SECONDS:
            return
        _last_prune_by_root[root] = now_ts

    cutoff_date = (now_utc - timedelta(days=retention_days)).date()
    if not os.path.isdir(root):
        return

    # Layout: <root>/<story_id>/<YYYY-MM-DD>/...
    for story_id in os.listdir(root):
        story_path = os.path.join(root, story_id)
        if not os.path.isdir(story_path):
            continue
        for date_dir in os.listdir(story_path):
            date_path = os.path.join(story_path, date_dir)
            if not os.path.isdir(date_path):
                continue
            try:
                day = datetime.strptime(date_dir, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff_date:
                shutil.rmtree(date_path, ignore_errors=True)


def write_trace(
    *,
    data_dir: str,
    story_id: str,
    stage: str,
    payload: Any,
    branch_id: str = "",
    message_index: int | None = None,
    tags: dict | None = None,
    source: str = "",
    retention_days: int = 14,
    now_utc: datetime | None = None,
) -> str | None:
    """Write a single LLM trace event JSON file and return its path.

    Directory layout:
    data/llm_traces/<story_id>/<YYYY-MM-DD>/<branch_id>/<msg_tag>/<HHMMSS.mmm>_<stage>_<id>.json
    """
    if not data_dir or not story_id or not stage:
        return None

    now = now_utc or datetime.now(timezone.utc)
    root = os.path.join(data_dir, "llm_traces")
    _maybe_prune_old_traces(root, retention_days=retention_days, now_utc=now)

    date_dir = now.strftime("%Y-%m-%d")
    branch_tag = _safe_token(branch_id, "no_branch")
    msg_tag = f"msg_{message_index:06d}" if isinstance(message_index, int) and message_index >= 0 else "msg_na"
    stage_tag = _safe_token(stage, "stage")
    ts = now.strftime("%H%M%S.%f")[:-3]
    suffix = uuid.uuid4().hex[:8]

    out_dir = os.path.join(root, _safe_token(story_id, "story"), date_dir, branch_tag, msg_tag)
    out_path = os.path.join(out_dir, f"{ts}_{stage_tag}_{suffix}.json")

    record = {
        "schema_version": 1,
        "created_at": now.isoformat(),
        "story_id": story_id,
        "branch_id": branch_id or "",
        "message_index": message_index,
        "stage": stage,
        "source": source or "",
        "tags": tags or {},
        "payload": payload,
    }

    try:
        _atomic_write_json(out_path, record)
        return out_path
    except Exception:
        return None
