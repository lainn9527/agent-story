from __future__ import annotations

from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
import os
import shutil
import uuid


story_bp = Blueprint("story", __name__)


def _app():
    import app as app_module

    return app_module


@story_bp.route("/api/stories")
def api_stories():
    app_module = _app()
    registry = app_module._load_stories_registry()
    return jsonify(
        {
            "active_story_id": registry.get("active_story_id", "story_original"),
            "stories": registry.get("stories", {}),
        }
    )


@story_bp.route("/api/stories", methods=["POST"])
def api_stories_create():
    app_module = _app()
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "story name required"}), 400

    description = body.get("description", "").strip()
    system_prompt_text = body.get("system_prompt", "").strip()
    character_schema = body.get("character_schema")
    default_state = body.get("default_character_state")

    story_id = f"story_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    story_dir = app_module._story_dir(story_id)
    os.makedirs(story_dir, exist_ok=True)
    os.makedirs(app_module._story_design_dir(story_id), exist_ok=True)

    if system_prompt_text:
        with open(app_module._story_system_prompt_path(story_id), "w", encoding="utf-8") as handle:
            handle.write(system_prompt_text)

    if character_schema:
        app_module._save_json(app_module._story_character_schema_path(story_id), character_schema)
    else:
        app_module._save_json(
            app_module._story_character_schema_path(story_id),
            {
                "fields": [{"key": "name", "label": "姓名", "type": "text"}],
                "lists": [],
                "direct_overwrite_keys": [],
            },
        )

    if default_state:
        app_module._save_json(app_module._story_default_character_state_path(story_id), default_state)
    else:
        app_module._save_json(app_module._story_default_character_state_path(story_id), {"name": "—"})

    app_module._save_json(app_module._story_parsed_path(story_id), [])

    tree = {
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
    }
    app_module._save_tree(story_id, tree)
    app_module._save_branch_messages(story_id, "main", [])

    initial_state = default_state if default_state else {"name": "—"}
    app_module._save_json(app_module._story_character_state_path(story_id, "main"), initial_state)

    registry = app_module._load_stories_registry()
    registry["stories"][story_id] = {
        "id": story_id,
        "name": name,
        "description": description,
        "created_at": now,
    }
    app_module._save_stories_registry(registry)

    return jsonify({"ok": True, "story": registry["stories"][story_id]})


@story_bp.route("/api/stories/switch", methods=["POST"])
def api_stories_switch():
    app_module = _app()
    body = request.get_json(force=True)
    story_id = body.get("story_id", "").strip()
    if not story_id:
        return jsonify({"ok": False, "error": "story_id required"}), 400

    registry = app_module._load_stories_registry()
    if story_id not in registry.get("stories", {}):
        return jsonify({"ok": False, "error": "story not found"}), 404

    registry["active_story_id"] = story_id
    app_module._save_stories_registry(registry)

    tree = app_module._load_tree(story_id)
    active_branch = tree.get("active_branch_id", "main")
    original = app_module._load_json(app_module._story_parsed_path(story_id), [])
    story_meta = registry["stories"][story_id]
    character_schema = app_module._load_character_schema(story_id)

    return jsonify(
        {
            "ok": True,
            "active_story_id": story_id,
            "story_name": story_meta.get("name", story_id),
            "active_branch_id": active_branch,
            "original_count": len(original),
            "character_schema": character_schema,
        }
    )


@story_bp.route("/api/stories/<story_id>", methods=["PATCH"])
def api_stories_update(story_id: str):
    app_module = _app()
    body = request.get_json(force=True)
    registry = app_module._load_stories_registry()
    stories = registry.get("stories", {})

    if story_id not in stories:
        return jsonify({"ok": False, "error": "story not found"}), 404

    if "name" in body and body["name"].strip():
        stories[story_id]["name"] = body["name"].strip()
    if "description" in body:
        stories[story_id]["description"] = body["description"].strip()

    app_module._save_stories_registry(registry)
    return jsonify({"ok": True, "story": stories[story_id]})


@story_bp.route("/api/stories/<story_id>", methods=["DELETE"])
def api_stories_delete(story_id: str):
    app_module = _app()
    registry = app_module._load_stories_registry()
    stories = registry.get("stories", {})

    if story_id not in stories:
        return jsonify({"ok": False, "error": "story not found"}), 404
    if len(stories) <= 1:
        return jsonify({"ok": False, "error": "cannot delete the last story"}), 400

    story_dir = app_module._story_dir(story_id)
    if os.path.exists(story_dir):
        shutil.rmtree(story_dir)
    design_dir = app_module._story_design_dir(story_id)
    if os.path.exists(design_dir):
        shutil.rmtree(design_dir)

    del stories[story_id]

    if registry.get("active_story_id") == story_id:
        registry["active_story_id"] = next(iter(stories))

    app_module._save_stories_registry(registry)
    return jsonify({"ok": True, "active_story_id": registry["active_story_id"]})


@story_bp.route("/api/stories/<story_id>/schema")
def api_stories_schema(story_id: str):
    app_module = _app()
    registry = app_module._load_stories_registry()
    if story_id not in registry.get("stories", {}):
        return jsonify({"ok": False, "error": "story not found"}), 404
    return jsonify(app_module._load_character_schema(story_id))
