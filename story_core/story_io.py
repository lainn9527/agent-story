"""Story runtime file I/O helpers and shared filesystem state."""

import json
import logging
import os
import threading
import time

log = logging.getLogger("rpg")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
STORIES_DIR = os.path.join(DATA_DIR, "stories")
STORY_DESIGN_DIR = os.path.join(BASE_DIR, "story_design")
STORIES_REGISTRY_PATH = os.path.join(DATA_DIR, "stories.json")
_LLM_CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")

# Track in-flight async tag extraction jobs keyed by (story, branch, msg_index).
_PENDING_EXTRACT_LOCK = threading.Lock()
_PENDING_EXTRACT: set[tuple[str, str, int]] = set()

# Thread-safe lock registry for branch messages.json read-modify-write operations.
_BRANCH_MESSAGES_LOCKS: dict[str, threading.Lock] = {}
_BRANCH_MESSAGES_LOCKS_META = threading.Lock()
_SYNCED_IMAGE_READY: set[tuple[str, str]] = set()
_SYNCED_IMAGE_READY_LOCK = threading.Lock()

DEFAULT_IMAGE_MODEL = "imagen-4.0-ultra-generate-001"


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_json(path, default=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else []


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _story_dir(story_id: str) -> str:
    return os.path.join(STORIES_DIR, story_id)


def _story_design_dir(story_id: str) -> str:
    return os.path.join(STORY_DESIGN_DIR, story_id)


def _story_tree_path(story_id: str) -> str:
    return os.path.join(_story_dir(story_id), "timeline_tree.json")


def _story_parsed_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "parsed_conversation.json")


def _branch_dir(story_id: str, branch_id: str) -> str:
    d = os.path.join(_story_dir(story_id), "branches", branch_id)
    os.makedirs(d, exist_ok=True)
    return d


def _debug_units_dir(story_id: str) -> str:
    d = os.path.join(_story_dir(story_id), "debug_units")
    os.makedirs(d, exist_ok=True)
    return d


def _debug_unit_dir(story_id: str, debug_unit_id: str) -> str:
    d = os.path.join(_debug_units_dir(story_id), debug_unit_id)
    os.makedirs(d, exist_ok=True)
    return d


def _debug_chat_path(story_id: str, debug_unit_id: str) -> str:
    return os.path.join(_debug_unit_dir(story_id, debug_unit_id), "chat.json")


def _last_apply_backup_path(story_id: str, debug_unit_id: str) -> str:
    return os.path.join(_debug_unit_dir(story_id, debug_unit_id), "last_apply_backup.json")


def _debug_directive_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "debug_directive.json")


def _dungeon_progress_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "dungeon_progress.json")


def _story_messages_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "messages.json")


def _story_character_state_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "character_state.json")


def _story_npcs_path(story_id: str, branch_id: str = "main") -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "npcs.json")


def _story_system_prompt_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "system_prompt.txt")


def _story_character_schema_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "character_schema.json")


def _story_default_character_state_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "default_character_state.json")


def _branch_config_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "config.json")


def _nsfw_preferences_path(story_id: str) -> str:
    return os.path.join(_story_design_dir(story_id), "nsfw_preferences.json")


def _mark_extract_pending(story_id: str, branch_id: str, msg_index: int):
    with _PENDING_EXTRACT_LOCK:
        _PENDING_EXTRACT.add((story_id, branch_id, msg_index))


def _mark_extract_done(story_id: str, branch_id: str, msg_index: int):
    with _PENDING_EXTRACT_LOCK:
        _PENDING_EXTRACT.discard((story_id, branch_id, msg_index))


def _has_pending_extract(story_id: str, branch_id: str, target_index: int, include_prior: bool) -> bool:
    with _PENDING_EXTRACT_LOCK:
        if include_prior:
            return any(
                sid == story_id and bid == branch_id and idx <= target_index
                for sid, bid, idx in _PENDING_EXTRACT
            )
        return (story_id, branch_id, target_index) in _PENDING_EXTRACT


def _wait_extract_done(story_id: str, branch_id: str, msg_index: int, timeout_s: float = 8.0):
    """Best-effort wait for async extraction at/before a target index to finish."""
    if msg_index is None or msg_index < 0:
        return
    deadline = time.time() + max(0.0, timeout_s)
    while time.time() < deadline:
        pending = _has_pending_extract(story_id, branch_id, msg_index, include_prior=True)
        if not pending:
            return
        time.sleep(0.05)
    log.warning(
        "extract_wait timeout: story=%s branch=%s max_msg=%s",
        story_id, branch_id, msg_index,
    )


def _get_branch_messages_lock(story_id: str, branch_id: str) -> threading.Lock:
    """Get/create a per-branch lock for messages.json RMW operations."""
    key = f"{story_id}:{branch_id}"
    with _BRANCH_MESSAGES_LOCKS_META:
        if key not in _BRANCH_MESSAGES_LOCKS:
            _BRANCH_MESSAGES_LOCKS[key] = threading.Lock()
        return _BRANCH_MESSAGES_LOCKS[key]


def _load_branch_messages(story_id: str, branch_id: str) -> list[dict]:
    """Thread-safe load of branch messages delta."""
    path = _story_messages_path(story_id, branch_id)
    lock = _get_branch_messages_lock(story_id, branch_id)
    with lock:
        data = _load_json(path, [])
    return data if isinstance(data, list) else []


def _save_branch_messages(story_id: str, branch_id: str, messages: list[dict]):
    """Thread-safe overwrite of branch messages delta."""
    path = _story_messages_path(story_id, branch_id)
    lock = _get_branch_messages_lock(story_id, branch_id)
    with lock:
        _save_json(path, messages)


def _mark_image_ready_in_branch_messages(story_id: str, branch_id: str, filename: str) -> bool:
    """Persist ready=true for any message in the branch that references filename."""
    path = _story_messages_path(story_id, branch_id)
    lock = _get_branch_messages_lock(story_id, branch_id)
    with lock:
        msgs = _load_json(path, [])
        if not isinstance(msgs, list):
            return False
        changed = False
        for msg in msgs:
            image = msg.get("image")
            if not isinstance(image, dict):
                continue
            if image.get("filename") != filename or image.get("ready") is True:
                continue
            image["ready"] = True
            changed = True
        if changed:
            _save_json(path, msgs)
        return changed


def _sync_message_image_ready(story_id: str, filename: str) -> bool:
    """Best-effort sync of stale message.image.ready flags after file creation."""
    cache_key = (story_id, filename)
    with _SYNCED_IMAGE_READY_LOCK:
        if cache_key in _SYNCED_IMAGE_READY:
            return False
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    changed = False
    for branch_id in branches:
        if _mark_image_ready_in_branch_messages(story_id, branch_id, filename):
            changed = True
    with _SYNCED_IMAGE_READY_LOCK:
        _SYNCED_IMAGE_READY.add(cache_key)
    return changed


def _upsert_branch_message(story_id: str, branch_id: str, message: dict):
    """Thread-safe upsert by message index (avoids stale list overwrite races)."""
    path = _story_messages_path(story_id, branch_id)
    lock = _get_branch_messages_lock(story_id, branch_id)
    with lock:
        msgs = _load_json(path, [])
        if not isinstance(msgs, list):
            msgs = []
        idx = message.get("index")
        replaced = False
        for i, existing in enumerate(msgs):
            if existing.get("index") == idx:
                msgs[i] = message
                replaced = True
                break
        if not replaced:
            msgs.append(message)
        msgs.sort(key=lambda m: m.get("index", 0))
        _save_json(path, msgs)


def _load_stories_registry() -> dict:
    return _load_json(STORIES_REGISTRY_PATH, {})


def _save_stories_registry(registry: dict):
    _save_json(STORIES_REGISTRY_PATH, registry)


def _active_story_id() -> str:
    reg = _load_stories_registry()
    return reg.get("active_story_id", "story_original")


def _load_tree(story_id: str) -> dict:
    return _load_json(_story_tree_path(story_id), {})


def _save_tree(story_id: str, tree: dict):
    _save_json(_story_tree_path(story_id), tree)


def _load_branch_config(story_id: str, branch_id: str) -> dict:
    return _load_json(_branch_config_path(story_id, branch_id), {})


def _save_branch_config(story_id: str, branch_id: str, config: dict):
    _save_json(_branch_config_path(story_id, branch_id), config)


def _branch_config_defaults() -> dict:
    return {
        "image_gen_enabled": True,
        "image_model": DEFAULT_IMAGE_MODEL,
    }


def _is_image_gen_enabled(branch_config: dict) -> bool:
    """Branch config gate for scene image generation. Default: enabled."""
    val = branch_config.get("image_gen_enabled", True)
    if isinstance(val, str):
        return val.strip().lower() not in {"0", "false", "off", "no"}
    return bool(val)


def _get_image_model(branch_config: dict) -> str:
    model = branch_config.get("image_model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_IMAGE_MODEL


__all__ = [
    "BASE_DIR",
    "DATA_DIR",
    "STORIES_DIR",
    "STORY_DESIGN_DIR",
    "STORIES_REGISTRY_PATH",
    "_LLM_CONFIG_PATH",
    "_PENDING_EXTRACT_LOCK",
    "_PENDING_EXTRACT",
    "_BRANCH_MESSAGES_LOCKS",
    "_SYNCED_IMAGE_READY",
    "_SYNCED_IMAGE_READY_LOCK",
    "DEFAULT_IMAGE_MODEL",
    "_ensure_data_dir",
    "_load_json",
    "_save_json",
    "_story_dir",
    "_story_design_dir",
    "_story_tree_path",
    "_story_parsed_path",
    "_branch_dir",
    "_debug_units_dir",
    "_debug_unit_dir",
    "_debug_chat_path",
    "_last_apply_backup_path",
    "_debug_directive_path",
    "_dungeon_progress_path",
    "_story_messages_path",
    "_story_character_state_path",
    "_story_npcs_path",
    "_story_system_prompt_path",
    "_story_character_schema_path",
    "_story_default_character_state_path",
    "_branch_config_path",
    "_nsfw_preferences_path",
    "_mark_extract_pending",
    "_mark_extract_done",
    "_has_pending_extract",
    "_wait_extract_done",
    "_get_branch_messages_lock",
    "_load_branch_messages",
    "_save_branch_messages",
    "_upsert_branch_message",
    "_mark_image_ready_in_branch_messages",
    "_sync_message_image_ready",
    "_load_stories_registry",
    "_save_stories_registry",
    "_active_story_id",
    "_load_tree",
    "_save_tree",
    "_load_branch_config",
    "_save_branch_config",
    "_branch_config_defaults",
    "_is_image_gen_enabled",
    "_get_image_model",
]
