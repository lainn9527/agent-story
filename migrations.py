"""Legacy data migrations and startup initialization helpers."""

import logging
import os
import shutil
from datetime import datetime, timezone

import story_io
from character_state import _load_character_schema
from dungeon_system import ensure_dungeon_templates
from event_db import delete_events_for_branch
from lore_db import rebuild_index as rebuild_lore_index
from parser import parse_conversation, save_parsed
from prompts import SYSTEM_PROMPT_TEMPLATE
from story_io import (
    _branch_dir,
    _ensure_data_dir,
    _load_json,
    _load_tree,
    _save_branch_messages,
    _save_json,
    _save_stories_registry,
    _save_tree,
    _story_character_schema_path,
    _story_default_character_state_path,
    _story_design_dir,
    _story_dir,
    _story_messages_path,
    _story_parsed_path,
    _story_system_prompt_path,
    _story_tree_path,
)

log = logging.getLogger("rpg")


class _DynamicPath(os.PathLike):
    def __init__(self, resolver):
        self._resolver = resolver

    def __fspath__(self) -> str:
        return self._resolver()

    def __str__(self) -> str:
        return self._resolver()

    def __repr__(self) -> str:
        return repr(self._resolver())


CONVERSATION_PATH = _DynamicPath(lambda: os.path.join(story_io.BASE_DIR, "Grok_conversation.md"))
LEGACY_PARSED_PATH = _DynamicPath(lambda: os.path.join(story_io.DATA_DIR, "parsed_conversation.json"))
LEGACY_TREE_PATH = _DynamicPath(lambda: os.path.join(story_io.DATA_DIR, "timeline_tree.json"))
LEGACY_CHARACTER_STATE_PATH = _DynamicPath(lambda: os.path.join(story_io.DATA_DIR, "character_state.json"))
LEGACY_NEW_MESSAGES_PATH = _DynamicPath(lambda: os.path.join(story_io.DATA_DIR, "new_messages.json"))


def _migrate_to_timeline_tree(story_id: str):
    """One-time migration: create timeline_tree.json for a story from existing data."""
    tree_path = _story_tree_path(story_id)
    if os.path.exists(tree_path):
        return

    now = datetime.now(timezone.utc).isoformat()

    session_id_path = os.path.join(story_io.DATA_DIR, "session_id.txt")
    session_id = None
    if os.path.exists(session_id_path):
        with open(session_id_path, "r", encoding="utf-8") as f:
            sid = f.read().strip()
            if sid:
                session_id = sid

    tree = {
        "active_branch_id": "main",
        "branches": {
            "main": {
                "id": "main",
                "name": "主時間線",
                "parent_branch_id": None,
                "branch_point_index": None,
                "created_at": now,
                "session_id": session_id,
                "character_state_file": "character_state_main.json",
            }
        },
    }
    _save_tree(story_id, tree)

    main_msgs_path = _story_messages_path(story_id, "main")
    legacy_new_msgs = os.path.join(_story_dir(story_id), "new_messages.json")
    if os.path.exists(legacy_new_msgs) and not os.path.exists(main_msgs_path):
        shutil.move(legacy_new_msgs, main_msgs_path)

    if not os.path.exists(main_msgs_path):
        _save_branch_messages(story_id, "main", [])


def _migrate_to_stories():
    """One-time migration: move all legacy flat data/ files into data/stories/story_original/."""
    if os.path.exists(story_io.STORIES_REGISTRY_PATH):
        return

    _ensure_data_dir()
    story_id = "story_original"
    story_dir = _story_dir(story_id)
    os.makedirs(story_dir, exist_ok=True)

    moves = {
        "timeline_tree.json": "timeline_tree.json",
        "parsed_conversation.json": "parsed_conversation.json",
        "new_messages.json": "new_messages.json",
    }
    for src_name, dst_name in moves.items():
        src = os.path.join(story_io.DATA_DIR, src_name)
        dst = os.path.join(story_dir, dst_name)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    for fname in os.listdir(story_io.DATA_DIR):
        if fname.startswith("messages_") and fname.endswith(".json"):
            src = os.path.join(story_io.DATA_DIR, fname)
            dst = os.path.join(story_dir, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
        elif fname.startswith("character_state_") and fname.endswith(".json"):
            src = os.path.join(story_io.DATA_DIR, fname)
            dst = os.path.join(story_dir, fname)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)

    legacy_char = os.path.join(story_io.DATA_DIR, "character_state.json")
    if os.path.exists(legacy_char):
        dst = os.path.join(story_dir, "character_state.json")
        if not os.path.exists(dst):
            shutil.copy2(legacy_char, dst)

    os.makedirs(_story_design_dir(story_id), exist_ok=True)

    prompt_path = _story_system_prompt_path(story_id)
    if not os.path.exists(prompt_path):
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(SYSTEM_PROMPT_TEMPLATE)

    from app_helpers import DEFAULT_CHARACTER_SCHEMA, DEFAULT_CHARACTER_STATE

    schema_path = _story_character_schema_path(story_id)
    if not os.path.exists(schema_path):
        _save_json(schema_path, DEFAULT_CHARACTER_SCHEMA)

    default_state_path = _story_default_character_state_path(story_id)
    if not os.path.exists(default_state_path):
        _save_json(default_state_path, DEFAULT_CHARACTER_STATE)

    parsed_path = _story_parsed_path(story_id)
    if not os.path.exists(parsed_path):
        if os.path.exists(CONVERSATION_PATH):
            save_parsed(
                parse_conversation(os.fspath(CONVERSATION_PATH)),
                output=os.fspath(LEGACY_PARSED_PATH),
            )
            if os.path.exists(LEGACY_PARSED_PATH):
                shutil.copy2(LEGACY_PARSED_PATH, parsed_path)
        else:
            _save_json(parsed_path, [])

    now = datetime.now(timezone.utc).isoformat()
    registry = {
        "active_story_id": story_id,
        "stories": {
            story_id: {
                "id": story_id,
                "name": "主神空間 — 無限輪迴",
                "description": "諸天無限流·主神空間 RPG",
                "created_at": now,
            }
        },
    }
    _save_stories_registry(registry)
    _migrate_to_timeline_tree(story_id)


def _migrate_branch_files(story_id: str):
    """One-time migration: move flat per-branch files into branches/<branch_id>/ subdirs."""
    story_dir = _story_dir(story_id)
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    if not branches:
        return

    migrated = False
    for branch_id in branches:
        branch_dir = os.path.join(story_dir, "branches", branch_id)
        os.makedirs(branch_dir, exist_ok=True)

        moves = [
            (f"messages_{branch_id}.json", "messages.json"),
            (f"character_state_{branch_id}.json", "character_state.json"),
            (f"npcs_{branch_id}.json", "npcs.json"),
            (f"npc_activities_{branch_id}.json", "npc_activities.json"),
        ]
        for old_name, new_name in moves:
            src = os.path.join(story_dir, old_name)
            dst = os.path.join(branch_dir, new_name)
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.move(src, dst)
                migrated = True

    legacy_npcs = os.path.join(story_dir, "npcs.json")
    main_npcs = os.path.join(story_dir, "branches", "main", "npcs.json")
    if os.path.exists(legacy_npcs) and not os.path.exists(main_npcs):
        os.makedirs(os.path.dirname(main_npcs), exist_ok=True)
        shutil.move(legacy_npcs, main_npcs)
        migrated = True

    if migrated:
        log.info("Migrated branch files to branches/ dirs for story %s", story_id)


def _migrate_schema_abilities(story_id: str):
    """One-time migration: add 'abilities' list field to character schema and default state."""
    schema = _load_character_schema(story_id)
    lists = schema.get("lists", [])
    has_abilities = any(l.get("key") == "abilities" for l in lists)
    if not has_abilities:
        lists.append({
            "key": "abilities",
            "label": "功法與技能",
            "state_add_key": "abilities_add",
            "state_remove_key": "abilities_remove",
        })
        schema["lists"] = lists
        schema_path = _story_character_schema_path(story_id)
        _save_json(schema_path, schema)
        log.info("Migrated character_schema.json: added 'abilities' list for story %s", story_id)

    default_path = _story_default_character_state_path(story_id)
    if os.path.exists(default_path):
        default_state = _load_json(default_path, {})
        if "abilities" not in default_state:
            default_state["abilities"] = []
            _save_json(default_path, default_state)
            log.info("Migrated default_character_state.json: added 'abilities' for story %s", story_id)


def _migrate_design_files(story_id: str):
    """One-time migration: copy design files from data/stories/<id>/ to story_design/<id>/.

    Copies files only if they exist in the old location but not in the new one.
    The old copies in data/ become inert (no longer read by code).
    """
    design_dir = _story_design_dir(story_id)
    old_dir = _story_dir(story_id)

    design_files = [
        "system_prompt.txt",
        "character_schema.json",
        "default_character_state.json",
        "world_lore.json",
        "parsed_conversation.json",
        "nsfw_preferences.json",
    ]

    migrated = False
    for fname in design_files:
        old_path = os.path.join(old_dir, fname)
        new_path = os.path.join(design_dir, fname)
        if os.path.exists(old_path) and not os.path.exists(new_path):
            os.makedirs(design_dir, exist_ok=True)
            shutil.copy2(old_path, new_path)
            migrated = True

    if migrated:
        log.info("Migrated design files to story_design/ for story %s", story_id)
        lore_path = os.path.join(design_dir, "world_lore.json")
        if os.path.exists(lore_path):
            try:
                rebuild_lore_index(story_id)
            except Exception:
                log.warning("Failed to rebuild lore index after migration for %s", story_id, exc_info=True)


def _init_lore_indexes():
    """Rebuild lore search indexes for all stories on startup."""
    if not os.path.exists(story_io.STORY_DESIGN_DIR):
        return
    for story_dir_name in os.listdir(story_io.STORY_DESIGN_DIR):
        lore_path = os.path.join(story_io.STORY_DESIGN_DIR, story_dir_name, "world_lore.json")
        if os.path.exists(lore_path):
            rebuild_lore_index(story_dir_name)


def _cleanup_incomplete_branches():
    """Remove branches orphaned by server crash (no GM response saved)."""
    if not os.path.exists(story_io.STORIES_DIR):
        return
    for story_dir_name in os.listdir(story_io.STORIES_DIR):
        tree_path = os.path.join(story_io.STORIES_DIR, story_dir_name, "timeline_tree.json")
        if not os.path.exists(tree_path):
            continue
        tree = _load_json(tree_path, {})
        branches = tree.get("branches", {})
        modified = False
        to_delete = []

        for bid, branch in branches.items():
            if bid == "main":
                continue
            if branch.get("deleted") or branch.get("blank") or branch.get("merged") or branch.get("pruned"):
                continue
            if bid.startswith("auto_"):
                continue
            msgs_path = os.path.join(story_io.STORIES_DIR, story_dir_name, "branches", bid, "messages.json")
            msgs = _load_json(msgs_path, [])
            has_user = any(m.get("role") == "user" for m in msgs)
            has_gm = any(m.get("role") == "gm" for m in msgs)
            if has_user and not has_gm:
                to_delete.append(bid)

        for bid in to_delete:
            parent = branches[bid].get("parent_branch_id", "main")
            for other_branch in branches.values():
                if other_branch.get("parent_branch_id") == bid:
                    other_branch["parent_branch_id"] = parent
            del branches[bid]
            if tree.get("active_branch_id") == bid:
                tree["active_branch_id"] = parent
            try:
                delete_events_for_branch(story_dir_name, bid)
            except Exception as e:
                log.warning(
                    "Startup cleanup: failed to delete events for branch %s in story %s (%s)",
                    bid, story_dir_name, e,
                )
            bdir = os.path.join(story_io.STORIES_DIR, story_dir_name, "branches", bid)
            if os.path.isdir(bdir):
                shutil.rmtree(bdir)
            log.warning("Startup cleanup: removed incomplete branch %s from story %s (no GM response)", bid, story_dir_name)
            modified = True

        if modified:
            _save_json(tree_path, tree)


def _init_dungeon_templates():
    """Ensure dungeon templates exist for all stories on startup."""
    if not os.path.exists(story_io.STORIES_DIR):
        return
    for story_dir_name in os.listdir(story_io.STORIES_DIR):
        story_path = os.path.join(story_io.STORIES_DIR, story_dir_name)
        if os.path.isdir(story_path):
            ensure_dungeon_templates(story_dir_name)


__all__ = [
    "CONVERSATION_PATH",
    "LEGACY_PARSED_PATH",
    "LEGACY_TREE_PATH",
    "LEGACY_CHARACTER_STATE_PATH",
    "LEGACY_NEW_MESSAGES_PATH",
    "_migrate_to_timeline_tree",
    "_migrate_to_stories",
    "_migrate_branch_files",
    "_migrate_schema_abilities",
    "_migrate_design_files",
    "_init_lore_indexes",
    "_cleanup_incomplete_branches",
    "_init_dungeon_templates",
]
