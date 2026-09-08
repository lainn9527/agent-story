#!/usr/bin/env python3
"""Scaffold a new story by copying story_design/_shared/_template/ and registering it.

Usage:
    python scripts/new_story.py <slug> [--name "Display name"] [--from <existing_story_id>] [--switch]

Examples:
    python scripts/new_story.py xianxia --name "仙俠世界"
    python scripts/new_story.py xianxia --from story_original
    python scripts/new_story.py xianxia --switch        # also set as active

The script does pure filesystem work plus a registry update — it does NOT call the
running server. Restart or hit /api/stories from the UI afterwards if the server
needs to pick the new story up.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from story_core.story_io import (  # noqa: E402
    DATA_DIR,
    STORIES_REGISTRY_PATH,
    STORY_DESIGN_DIR,
    STORY_TEMPLATE_DIR,
    _is_valid_story_id,
    _load_json,
    _save_json,
    _story_design_dir,
    _story_dir,
)


def _err(msg: str) -> "NoReturn":
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _scaffold_design(slug: str, source_dir: str | None, explicit_source: bool) -> None:
    target = _story_design_dir(slug)
    target_has_content = os.path.exists(target) and os.listdir(target)
    if target_has_content and not explicit_source:
        # Wizard or manual edits already populated the dir — preserve, skip copy.
        print(f"  design dir already populated, skipping copy: {os.path.relpath(target)}")
        return
    if target_has_content and explicit_source:
        _err(f"design dir already exists and is non-empty: {target} (refuse to overwrite with --from)")
    if not source_dir or not os.path.isdir(source_dir):
        _err(f"source design dir does not exist: {source_dir}")
    shutil.copytree(source_dir, target, dirs_exist_ok=True)
    print(f"  copied design: {os.path.relpath(source_dir)} → {os.path.relpath(target)}")


def _scaffold_runtime(slug: str) -> None:
    story_dir = _story_dir(slug)
    os.makedirs(story_dir, exist_ok=True)

    branches_dir = os.path.join(story_dir, "branches", "main")
    os.makedirs(branches_dir, exist_ok=True)

    tree_path = os.path.join(story_dir, "timeline_tree.json")
    if not os.path.exists(tree_path):
        now = datetime.now(timezone.utc).isoformat()
        _save_json(tree_path, {
            "active_branch_id": "main",
            "branches": {
                "main": {
                    "id": "main",
                    "name": "主時間線",
                    "parent_branch_id": None,
                    "branch_point_index": None,
                    "created_at": now,
                    "session_id": None,
                    "character_state_file": "character_state_main.json",
                }
            },
        })

    msgs_path = os.path.join(branches_dir, "messages.json")
    if not os.path.exists(msgs_path):
        _save_json(msgs_path, [])

    state_path = os.path.join(branches_dir, "character_state.json")
    if not os.path.exists(state_path):
        default_path = os.path.join(_story_design_dir(slug), "default_character_state.json")
        default_state = _load_json(default_path, {"name": "—"})
        _save_json(state_path, default_state)

    print(f"  initialized runtime: {os.path.relpath(story_dir)}")


def _register(slug: str, name: str, description: str, switch: bool) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    registry = _load_json(STORIES_REGISTRY_PATH, {}) or {}
    stories = registry.setdefault("stories", {})
    if slug in stories:
        _err(f"story already in registry: {slug}")
    now = datetime.now(timezone.utc).isoformat()
    stories[slug] = {
        "id": slug,
        "name": name,
        "description": description,
        "created_at": now,
    }
    if switch or not registry.get("active_story_id"):
        registry["active_story_id"] = slug
    _save_json(STORIES_REGISTRY_PATH, registry)
    print(f"  registered: {slug}  active={registry['active_story_id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("slug", help="story id slug, e.g. xianxia (lowercase, [a-z0-9_], must start with a letter)")
    parser.add_argument("--name", help="display name (defaults to slug)")
    parser.add_argument("--description", default="", help="optional description")
    parser.add_argument("--from", dest="source", default=None,
                        help="existing story id to copy design from (default: _shared/_template)")
    parser.add_argument("--switch", action="store_true", help="set new story as active")
    args = parser.parse_args()

    slug = args.slug.strip()
    if not _is_valid_story_id(slug):
        _err("invalid slug — use lowercase [a-z0-9_], must start with a letter, max 40 chars, no leading underscore")

    explicit_source = args.source is not None
    if explicit_source:
        source_dir = _story_design_dir(args.source)
    else:
        source_dir = STORY_TEMPLATE_DIR

    target_design = _story_design_dir(slug)
    target_populated = os.path.exists(target_design) and os.listdir(target_design)
    if not target_populated and not os.path.isdir(source_dir):
        _err(f"template not found: {source_dir} — create it first or use --from <existing_story_id>")

    name = (args.name or slug).strip()

    print(f"Creating story: {slug}")
    _scaffold_design(slug, source_dir, explicit_source)
    _scaffold_runtime(slug)
    _register(slug, name, args.description, args.switch)
    print(f"\ndone. Edit story_design/{slug}/ then restart server (or hit /api/stories/switch).")


if __name__ == "__main__":
    main()
