from __future__ import annotations

from datetime import datetime, timezone
from flask import Blueprint, Response, jsonify, request, stream_with_context
import logging
import os
import shutil
import time
import uuid


log = logging.getLogger("rpg")
branch_bp = Blueprint("branch", __name__)


def _app():
    import app as app_module

    return app_module


@branch_bp.route("/api/branches")
def api_branches():
    app_module = _app()
    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    visible = {
        branch_id: branch
        for branch_id, branch in tree.get("branches", {}).items()
        if not branch.get("deleted") and not branch.get("merged") and not branch.get("pruned")
    }
    return jsonify(
        {
            "active_branch_id": tree.get("active_branch_id", "main"),
            "last_played_branch_id": tree.get("last_played_branch_id"),
            "branches": visible,
        }
    )


@branch_bp.route("/api/branches", methods=["POST"])
def api_branches_create():
    app_module = _app()
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")
    if not name:
        return jsonify({"ok": False, "error": "branch name required"}), 400
    if branch_point_index is None:
        return jsonify({"ok": False, "error": "branch_point_index required"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id
    parent_branch_id = app_module._resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    app_module._wait_extract_done(story_id, parent_branch_id, branch_point_index)
    forked_state = app_module._find_state_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._backfill_forked_state(forked_state, story_id, source_branch_id)
    app_module._save_json(app_module._story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = app_module._find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._save_json(app_module._story_npcs_path(story_id, branch_id), forked_npcs)
    app_module.rebuild_state_db_from_json(story_id, branch_id, state=forked_state, npcs=forked_npcs)
    app_module._save_branch_config(story_id, branch_id, app_module._load_branch_config(story_id, source_branch_id))
    app_module.copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = app_module._find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    app_module.set_world_day(story_id, branch_id, forked_world_day)
    app_module.copy_cheats(app_module._story_dir(story_id), source_branch_id, branch_id)
    app_module._copy_branch_lore_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module.copy_events_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module._copy_gm_plan(story_id, source_branch_id, branch_id, branch_point_index=branch_point_index)
    app_module.copy_dungeon_progress(story_id, parent_branch_id, branch_id)
    app_module._save_branch_messages(story_id, branch_id, [])

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    app_module._clear_loaded_save_preview(tree)
    app_module._save_tree(story_id, tree)
    return jsonify({"ok": True, "branch": branches[branch_id]})


@branch_bp.route("/api/branches/blank", methods=["POST"])
def api_branches_blank():
    app_module = _app()
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "branch name required"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    blank_state = app_module._blank_character_state(story_id)
    app_module._save_json(app_module._story_character_state_path(story_id, branch_id), blank_state)
    blank_npcs = []
    app_module._save_json(app_module._story_npcs_path(story_id, branch_id), blank_npcs)
    app_module.rebuild_state_db_from_json(story_id, branch_id, state=blank_state, npcs=blank_npcs)
    app_module._save_branch_messages(story_id, branch_id, [])

    from dungeon_system import _save_dungeon_progress

    _save_dungeon_progress(
        story_id,
        branch_id,
        {"history": [], "current_dungeon": None, "total_dungeons_completed": 0},
    )
    app_module._save_branch_config(story_id, branch_id, app_module._load_branch_config(story_id, "main"))

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": "main",
        "branch_point_index": -1,
        "created_at": now,
        "session_id": None,
        "blank": True,
    }
    tree["active_branch_id"] = branch_id
    app_module._clear_loaded_save_preview(tree)
    app_module._save_tree(story_id, tree)
    return jsonify({"ok": True, "branch": branches[branch_id]})


@branch_bp.route("/api/branches/switch", methods=["POST"])
def api_branches_switch():
    app_module = _app()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "main")

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404
    branch = branches[branch_id]
    if branch.get("deleted") or branch.get("merged") or branch.get("pruned"):
        return jsonify({"ok": False, "error": "cannot switch to inactive branch"}), 400

    mainline_leaf = tree.get("promoted_mainline_leaf_id")
    if mainline_leaf in branches:
        chain = []
        current = mainline_leaf
        seen = set()
        while current is not None and current not in seen and current in branches:
            seen.add(current)
            chain.append(current)
            current = branches[current].get("parent_branch_id")
        chain_set = set(chain)
        if branch_id in chain_set:
            current = branch_id
            visited = set()
            while current not in visited:
                visited.add(current)
                children = [
                    bid
                    for bid, child in branches.items()
                    if child.get("parent_branch_id") == current
                    and not child.get("deleted")
                    and not child.get("merged")
                    and not child.get("pruned")
                    and bid in chain_set
                ]
                if len(children) != 1:
                    break
                current = children[0]
            branch_id = current

    tree["active_branch_id"] = branch_id
    app_module._clear_loaded_save_preview(tree)
    app_module._save_tree(story_id, tree)
    return jsonify({"ok": True, "active_branch_id": branch_id})


@branch_bp.route("/api/branches/<branch_id>", methods=["PATCH"])
def api_branches_rename(branch_id: str):
    app_module = _app()
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    if branch_id not in tree.get("branches", {}):
        return jsonify({"ok": False, "error": "branch not found"}), 404

    tree["branches"][branch_id]["name"] = name
    app_module._save_tree(story_id, tree)
    return jsonify({"ok": True, "branch": tree["branches"][branch_id]})


@branch_bp.route("/api/branches/<branch_id>/config", methods=["GET"])
def api_branch_config_get(branch_id: str):
    app_module = _app()
    story_id = app_module._active_story_id()
    config = app_module._load_branch_config(story_id, branch_id)
    return jsonify({"ok": True, "config": config, "defaults": app_module._branch_config_defaults()})


@branch_bp.route("/api/branches/<branch_id>/config", methods=["POST"])
def api_branch_config_set(branch_id: str):
    app_module = _app()
    story_id = app_module._active_story_id()
    config = app_module._load_branch_config(story_id, branch_id)
    body = request.get_json(force=True)
    config.update(body)
    app_module._save_branch_config(story_id, branch_id, config)
    return jsonify({"ok": True, "config": config, "defaults": app_module._branch_config_defaults()})


@branch_bp.route("/api/branches/promote", methods=["POST"])
def api_branches_promote():
    app_module = _app()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "").strip()
    if not branch_id or branch_id == "main":
        return jsonify({"ok": False, "error": "invalid branch_id"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404
    if branches[branch_id].get("deleted"):
        return jsonify({"ok": False, "error": "cannot promote a deleted branch"}), 400
    if branches[branch_id].get("merged"):
        return jsonify({"ok": False, "error": "cannot promote a merged branch"}), 400
    if branches[branch_id].get("pruned"):
        return jsonify({"ok": False, "error": "cannot promote a pruned branch"}), 400

    ancestor_chain_reverse = []
    current = branch_id
    visited = set()
    stopped_at_blank_root = False
    while current is not None and current not in visited:
        branch = branches.get(current)
        if not branch:
            break
        visited.add(current)
        ancestor_chain_reverse.append(current)
        if branch.get("blank"):
            stopped_at_blank_root = True
            break
        current = branch.get("parent_branch_id")
    ancestor_chain = list(reversed(ancestor_chain_reverse))
    keep_ids = set(ancestor_chain)

    for index in range(1, len(ancestor_chain)):
        parent_id = ancestor_chain[index - 1]
        child_id = ancestor_chain[index]
        child_branch_point = branches.get(child_id, {}).get("branch_point_index")
        if child_branch_point is None:
            continue
        parent_delta = app_module._load_branch_messages(story_id, parent_id)
        trimmed_delta = [message for message in parent_delta if message.get("index", 0) <= child_branch_point]
        if len(trimmed_delta) != len(parent_delta):
            app_module._save_branch_messages(story_id, parent_id, trimmed_delta)

    children_map = {}
    for bid, branch in branches.items():
        parent_id = branch.get("parent_branch_id")
        if parent_id is None:
            continue
        children_map.setdefault(parent_id, []).append(bid)

    branches_to_remove = set()

    def _collect_subtree(root_id: str):
        stack = [root_id]
        seen = set()
        while stack:
            bid = stack.pop()
            if bid in seen or bid in keep_ids:
                continue
            seen.add(bid)
            branches_to_remove.add(bid)
            stack.extend(children_map.get(bid, []))

    promote_root_id = ancestor_chain[0] if ancestor_chain else branch_id
    stack = [promote_root_id]
    walked = set()
    while stack:
        current = stack.pop()
        if current in walked:
            continue
        walked.add(current)
        for child_id in children_map.get(current, []):
            if child_id in keep_ids:
                stack.append(child_id)
            else:
                _collect_subtree(child_id)

    now = datetime.now(timezone.utc).isoformat()
    for bid in sorted(branches_to_remove):
        branches[bid]["deleted"] = True
        branches[bid]["deleted_at"] = now

    parent_id = branches[branch_id].get("parent_branch_id")
    if isinstance(parent_id, str) and parent_id in branches:
        app_module._copy_gm_plan(story_id, branch_id, parent_id, branch_point_index=None)
        app_module._copy_debug_directive(story_id, branch_id, parent_id)

    tree["active_branch_id"] = branch_id
    tree["promoted_mainline_leaf_id"] = branch_id
    app_module._save_tree(story_id, tree)
    return jsonify(
        {
            "ok": True,
            "active_branch_id": branch_id,
            "deleted_branch_ids": sorted(branches_to_remove),
            "stopped_at_blank_root": stopped_at_blank_root,
        }
    )


@branch_bp.route("/api/branches/merge", methods=["POST"])
def api_branches_merge():
    app_module = _app()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "").strip()
    if not branch_id or branch_id == "main":
        return jsonify({"ok": False, "error": "invalid branch_id"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    child = branches[branch_id]
    if child.get("deleted"):
        return jsonify({"ok": False, "error": "cannot merge a deleted branch"}), 400
    if child.get("merged"):
        return jsonify({"ok": False, "error": "branch already merged"}), 400
    if child.get("pruned"):
        return jsonify({"ok": False, "error": "cannot merge a pruned branch"}), 400

    parent_id = child.get("parent_branch_id")
    if parent_id is None:
        return jsonify({"ok": False, "error": "branch has no parent"}), 400
    if parent_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    branch_point = child.get("branch_point_index", -1)
    parent_messages = app_module._load_branch_messages(story_id, parent_id)
    kept = [message for message in parent_messages if message.get("index", 0) <= branch_point]
    child_messages = app_module._load_branch_messages(story_id, branch_id)
    for message in child_messages:
        message.pop("owner_branch_id", None)
        message.pop("inherited", None)
    kept.extend(child_messages)
    app_module._save_branch_messages(story_id, parent_id, kept)

    src_char = app_module._story_character_state_path(story_id, branch_id)
    dst_char = app_module._story_character_state_path(story_id, parent_id)
    if os.path.exists(src_char):
        shutil.copy2(src_char, dst_char)

    src_npcs = app_module._story_npcs_path(story_id, branch_id)
    dst_npcs = app_module._story_npcs_path(story_id, parent_id)
    if os.path.exists(src_npcs):
        shutil.copy2(src_npcs, dst_npcs)

    app_module.rebuild_state_db_from_json(story_id, parent_id)
    app_module.copy_recap_to_branch(story_id, branch_id, parent_id, -1)
    app_module.copy_world_day(story_id, branch_id, parent_id)
    app_module.copy_cheats(app_module._story_dir(story_id), branch_id, parent_id)
    app_module._merge_branch_lore_into(story_id, branch_id, parent_id)
    app_module.merge_events_into(story_id, branch_id, parent_id)
    app_module._copy_gm_plan(story_id, branch_id, parent_id, branch_point_index=None)
    app_module.copy_dungeon_progress(story_id, branch_id, parent_id)

    for bid, branch in branches.items():
        if branch.get("parent_branch_id") == branch_id:
            branch["parent_branch_id"] = parent_id

    now = datetime.now(timezone.utc).isoformat()
    child["merged"] = True
    child["merged_at"] = now
    if tree.get("active_branch_id") == branch_id:
        tree["active_branch_id"] = parent_id
    app_module._save_tree(story_id, tree)
    return jsonify({"ok": True, "parent_branch_id": parent_id})


@branch_bp.route("/api/branches/<branch_id>", methods=["DELETE"])
def api_branches_delete(branch_id: str):
    app_module = _app()
    if branch_id == "main":
        return jsonify({"ok": False, "error": "cannot delete main branch"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    branch = branches[branch_id]
    deleted_parent = branch.get("parent_branch_id", "main") or "main"
    deleted_branch_point = branch.get("branch_point_index")
    all_children = [child for child in branches.values() if child.get("parent_branch_id") == branch_id]
    active_children = [
        child for child in all_children
        if not child.get("deleted") and not child.get("merged") and not child.get("pruned")
    ]

    if active_children:
        deleted_delta = app_module._load_branch_messages(story_id, branch_id)
        for child in active_children:
            child_id = child["id"]
            child_branch_point = child.get("branch_point_index")
            if child_branch_point is not None and child_branch_point >= 0 and deleted_branch_point is not None:
                if child_branch_point >= deleted_branch_point:
                    inherited = [
                        message for message in deleted_delta
                        if message.get("index", 0) <= child_branch_point
                    ]
                    if inherited:
                        child_delta = app_module._load_branch_messages(story_id, child_id)
                        app_module._save_branch_messages(story_id, child_id, inherited + child_delta)
                        child["branch_point_index"] = deleted_branch_point

    for child in all_children:
        child["parent_branch_id"] = deleted_parent

    app_module.delete_events_for_branch(story_id, branch_id)

    if branch.get("was_main"):
        now = datetime.now(timezone.utc).isoformat()
        branch["deleted"] = True
        branch["deleted_at"] = now
    else:
        branch_dir = app_module._branch_dir(story_id, branch_id)
        if os.path.isdir(branch_dir):
            shutil.rmtree(branch_dir)
        del branches[branch_id]

    if tree.get("active_branch_id") == branch_id:
        tree["active_branch_id"] = deleted_parent
    app_module._save_tree(story_id, tree)
    return jsonify({"ok": True, "switch_to": deleted_parent})


@branch_bp.route("/api/branches/<branch_id>/protect", methods=["POST"])
def api_branches_protect(branch_id: str):
    app_module = _app()
    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    if branch_id not in branches:
        return jsonify({"ok": False, "error": "branch not found"}), 404

    branch = branches[branch_id]
    if branch.get("deleted") or branch.get("pruned") or branch.get("merged"):
        return jsonify({"ok": False, "error": "cannot protect inactive branch"}), 400

    if branch.get("protected"):
        branch.pop("protected", None)
        protected = False
    else:
        branch["protected"] = True
        protected = True

    app_module._save_tree(story_id, tree)
    return jsonify({"ok": True, "protected": protected})


@branch_bp.route("/api/branches/edit", methods=["POST"])
def api_branches_edit():
    """Edit a user message: create a branch, save edited message, call Claude."""
    app_module = _app()
    t_start = time.time()
    body = request.get_json(force=True)
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")
    edited_message = body.get("edited_message", "").strip()

    if branch_point_index is None:
        return jsonify({"ok": False, "error": "branch_point_index required"}), 400
    if not edited_message:
        return jsonify({"ok": False, "error": "edited_message required"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id

    edit_target_index = branch_point_index + 1
    timeline = app_module.get_full_timeline(story_id, source_branch_id)
    original_msg = app_module._find_timeline_message(timeline, edit_target_index, role="user")
    if not original_msg:
        return jsonify({"ok": False, "error": "invalid_edit_target"}), 400
    if original_msg.get("content", "").strip() == edited_message:
        return jsonify({"ok": False, "error": "no_change"}), 400

    parent_branch_id = app_module._resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    log.info("/api/branches/edit START  msg=%s", edited_message[:30])

    name = edited_message[:15].strip()
    if len(edited_message) > 15:
        name += "…"

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    app_module._wait_extract_done(story_id, parent_branch_id, branch_point_index)
    forked_state = app_module._find_state_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._backfill_forked_state(forked_state, story_id, source_branch_id)
    app_module._save_json(app_module._story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = app_module._find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._save_json(app_module._story_npcs_path(story_id, branch_id), forked_npcs)
    app_module.rebuild_state_db_from_json(story_id, branch_id, state=forked_state, npcs=forked_npcs)
    app_module._save_branch_config(
        story_id, branch_id, app_module._load_branch_config(story_id, source_branch_id)
    )
    app_module.copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = app_module._find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    app_module.set_world_day(story_id, branch_id, forked_world_day)
    app_module.copy_cheats(app_module._story_dir(story_id), source_branch_id, branch_id)
    app_module._copy_branch_lore_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module.copy_events_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module._copy_gm_plan(
        story_id, source_branch_id, branch_id, branch_point_index=branch_point_index
    )
    app_module.copy_dungeon_progress(story_id, parent_branch_id, branch_id)
    app_module._copy_debug_directive(story_id, source_branch_id, branch_id)

    user_msg_index = branch_point_index + 1
    gm_msg_index = branch_point_index + 2

    user_msg = {"role": "user", "content": edited_message, "index": user_msg_index}
    app_module._save_branch_messages(story_id, branch_id, [user_msg])

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    app_module._clear_loaded_save_preview(tree)
    app_module._save_tree(story_id, tree)

    t0 = time.time()
    full_timeline = app_module.get_full_timeline(story_id, branch_id)
    state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id)
    state_text = app_module._build_core_state_text(story_id, state)
    recap_text = app_module.get_recap_text(story_id, branch_id)
    system_prompt = app_module._build_story_system_prompt(
        story_id,
        state_text,
        branch_id=branch_id,
        narrative_recap=recap_text,
        npcs=npcs,
        state_dict=state,
    )
    recent = app_module._sanitize_recent_messages(
        full_timeline[-app_module.RECENT_MESSAGE_COUNT :],
        strip_fate=not app_module.get_fate_mode(app_module._story_dir(story_id), branch_id),
    )
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
    augmented_edit, dice_result = app_module._build_augmented_message(
        story_id,
        branch_id,
        edited_message,
        state,
        npcs=npcs,
        recent_messages=recent,
        turn_count=turn_count,
        current_index=user_msg_index,
    )
    if dice_result:
        user_msg["dice"] = dice_result
        app_module._upsert_branch_message(story_id, branch_id, user_msg)
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    app_module._trace_llm(
        stage="gm_request",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/branches/edit",
        payload={
            "user_text": edited_message,
            "augmented_text": augmented_edit,
            "system_prompt": system_prompt,
            "recent": recent,
            "dice_result": dice_result,
        },
        tags={"mode": "sync"},
    )
    t0 = time.time()
    try:
        gm_response, _ = app_module.call_claude_gm(
            augmented_edit, system_prompt, recent, session_id=None
        )
    except Exception as exc:
        log.info("/api/branches/edit EXCEPTION %s", exc)
        app_module._cleanup_branch(story_id, branch_id)
        return jsonify({"ok": False, "error": str(exc)}), 500
    edit_elapsed = time.time() - t0
    log.info("  claude_call: %.1fs", edit_elapsed)
    app_module._log_llm_usage(story_id, "gm", edit_elapsed, branch_id=branch_id)
    app_module._trace_llm(
        stage="gm_response_raw",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/branches/edit",
        payload={"response": gm_response, "usage": app_module.get_last_usage()},
        tags={"mode": "sync"},
    )

    gm_response, image_info, snapshots = app_module._process_gm_response(
        gm_response, story_id, branch_id, gm_msg_index
    )

    gm_msg = {"role": "gm", "content": gm_response, "index": gm_msg_index}
    if image_info:
        gm_msg["image"] = image_info
    gm_msg.update(snapshots)
    app_module._upsert_branch_message(story_id, branch_id, gm_msg)

    recap = app_module.load_recap(story_id, branch_id)
    if app_module.should_compact(recap, len(full_timeline) + 1):
        timeline_with_reply = list(full_timeline) + [gm_msg]
        app_module.compact_async(story_id, branch_id, timeline_with_reply)

    log.info("/api/branches/edit DONE   total=%.1fs", time.time() - t_start)
    return jsonify(
        {
            "ok": True,
            "branch": tree["branches"][branch_id],
            "user_msg": user_msg,
            "gm_msg": gm_msg,
        }
    )


@branch_bp.route("/api/branches/edit/stream", methods=["POST"])
def api_branches_edit_stream():
    """Streaming version of /api/branches/edit — returns SSE events."""
    app_module = _app()
    body = request.get_json(force=True)
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")
    edited_message = body.get("edited_message", "").strip()

    if branch_point_index is None:
        return Response(
            app_module._sse_event({"type": "error", "message": "branch_point_index required"}),
            mimetype="text/event-stream",
        )
    if not edited_message:
        return Response(
            app_module._sse_event({"type": "error", "message": "edited_message required"}),
            mimetype="text/event-stream",
        )

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id

    edit_target_index = branch_point_index + 1
    timeline = app_module.get_full_timeline(story_id, source_branch_id)
    original_msg = app_module._find_timeline_message(timeline, edit_target_index, role="user")
    if not original_msg:
        return Response(
            app_module._sse_event({"type": "error", "message": "invalid_edit_target"}),
            mimetype="text/event-stream",
        )
    if original_msg.get("content", "").strip() == edited_message:
        return Response(
            app_module._sse_event({"type": "error", "message": "no_change"}),
            mimetype="text/event-stream",
        )

    parent_branch_id = app_module._resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return Response(
            app_module._sse_event({"type": "error", "message": "parent branch not found"}),
            mimetype="text/event-stream",
        )

    log.info("/api/branches/edit/stream START  msg=%s", edited_message[:30])

    name = edited_message[:15].strip()
    if len(edited_message) > 15:
        name += "…"

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    app_module._wait_extract_done(story_id, parent_branch_id, branch_point_index)
    forked_state = app_module._find_state_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._backfill_forked_state(forked_state, story_id, source_branch_id)
    app_module._save_json(app_module._story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = app_module._find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._save_json(app_module._story_npcs_path(story_id, branch_id), forked_npcs)
    app_module.rebuild_state_db_from_json(story_id, branch_id, state=forked_state, npcs=forked_npcs)
    app_module._save_branch_config(
        story_id, branch_id, app_module._load_branch_config(story_id, source_branch_id)
    )
    app_module.copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = app_module._find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    app_module.set_world_day(story_id, branch_id, forked_world_day)
    app_module.copy_cheats(app_module._story_dir(story_id), source_branch_id, branch_id)
    app_module._copy_branch_lore_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module.copy_events_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module._copy_gm_plan(
        story_id, source_branch_id, branch_id, branch_point_index=branch_point_index
    )
    app_module.copy_dungeon_progress(story_id, parent_branch_id, branch_id)
    app_module._copy_debug_directive(story_id, source_branch_id, branch_id)

    user_msg_index = branch_point_index + 1
    gm_msg_index = branch_point_index + 2

    user_msg = {"role": "user", "content": edited_message, "index": user_msg_index}
    app_module._save_branch_messages(story_id, branch_id, [user_msg])

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    tree["last_played_branch_id"] = branch_id
    app_module._clear_loaded_save_preview(tree)
    app_module._save_tree(story_id, tree)

    full_timeline = app_module.get_full_timeline(story_id, branch_id)
    state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id)
    state_text = app_module._build_core_state_text(story_id, state)
    recap_text = app_module.get_recap_text(story_id, branch_id)
    system_prompt = app_module._build_story_system_prompt(
        story_id,
        state_text,
        branch_id=branch_id,
        narrative_recap=recap_text,
        npcs=npcs,
        state_dict=state,
    )
    recent = app_module._sanitize_recent_messages(
        full_timeline[-app_module.RECENT_MESSAGE_COUNT :],
        strip_fate=not app_module.get_fate_mode(app_module._story_dir(story_id), branch_id),
    )
    turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
    augmented_edit, dice_result = app_module._build_augmented_message(
        story_id,
        branch_id,
        edited_message,
        state,
        npcs=npcs,
        recent_messages=recent,
        turn_count=turn_count,
        current_index=user_msg_index,
    )
    if dice_result:
        user_msg["dice"] = dice_result
        app_module._upsert_branch_message(story_id, branch_id, user_msg)

    app_module._trace_llm(
        stage="gm_request",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/branches/edit/stream",
        payload={
            "user_text": edited_message,
            "augmented_text": augmented_edit,
            "system_prompt": system_prompt,
            "recent": recent,
            "dice_result": dice_result,
        },
        tags={"mode": "stream"},
    )

    def generate():
        t_start = time.time()
        if dice_result:
            yield app_module._sse_event({"type": "dice", "dice": dice_result})
        try:
            for event_type, payload in app_module.call_claude_gm_stream(
                augmented_edit, system_prompt, recent, session_id=None
            ):
                if event_type == "text":
                    yield app_module._sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    app_module._cleanup_branch(story_id, branch_id)
                    yield app_module._sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    gm_response = payload["response"]
                    app_module._log_llm_usage(
                        story_id,
                        "gm_stream",
                        time.time() - t_start,
                        branch_id=branch_id,
                        usage=payload.get("usage"),
                    )
                    app_module._trace_llm(
                        stage="gm_response_raw",
                        story_id=story_id,
                        branch_id=branch_id,
                        message_index=gm_msg_index,
                        source="/api/branches/edit/stream",
                        payload={"response": gm_response, "usage": payload.get("usage")},
                        tags={"mode": "stream"},
                    )

                    gm_response, image_info, snapshots = app_module._process_gm_response(
                        gm_response, story_id, branch_id, gm_msg_index
                    )

                    gm_msg = {"role": "gm", "content": gm_response, "index": gm_msg_index}
                    if image_info:
                        gm_msg["image"] = image_info
                    gm_msg.update(snapshots)
                    app_module._upsert_branch_message(story_id, branch_id, gm_msg)

                    recap = app_module.load_recap(story_id, branch_id)
                    if app_module.should_compact(recap, len(full_timeline) + 1):
                        timeline_with_reply = list(full_timeline) + [gm_msg]
                        app_module.compact_async(story_id, branch_id, timeline_with_reply)

                    log.info("/api/branches/edit/stream DONE total=%.1fs", time.time() - t_start)
                    yield app_module._sse_event(
                        {
                            "type": "done",
                            "branch": tree["branches"][branch_id],
                            "user_msg": user_msg,
                            "gm_msg": gm_msg,
                        }
                    )
        except Exception as exc:
            import traceback

            log.info(
                "/api/branches/edit/stream EXCEPTION %s\n%s",
                exc,
                traceback.format_exc(),
            )
            app_module._cleanup_branch(story_id, branch_id)
            yield app_module._sse_event({"type": "error", "message": str(exc)})
        finally:
            delta_now = app_module._load_branch_messages(story_id, branch_id)
            has_gm = any(message.get("role") == "gm" for message in delta_now)
            if not has_gm:
                log.info("/api/branches/edit/stream cleanup orphan branch %s", branch_id)
                app_module._cleanup_branch(story_id, branch_id)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@branch_bp.route("/api/branches/regenerate", methods=["POST"])
def api_branches_regenerate():
    """Regenerate a GM message: create a branch, call Claude, save new response."""
    app_module = _app()
    t_start = time.time()
    body = request.get_json(force=True)
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")

    if branch_point_index is None:
        return jsonify({"ok": False, "error": "branch_point_index required"}), 400

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id
    source_timeline = app_module.get_full_timeline(story_id, source_branch_id)
    user_msg = app_module._find_timeline_message(source_timeline, branch_point_index, role="user")
    gm_msg = app_module._find_timeline_message(
        source_timeline, branch_point_index + 1, role=("gm", "assistant")
    )
    if not user_msg or not gm_msg:
        return jsonify({"ok": False, "error": "invalid_regenerate_target"}), 400
    parent_branch_id = app_module._resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return jsonify({"ok": False, "error": "parent branch not found"}), 404

    user_msg_content = user_msg.get("content", "")

    log.info("/api/branches/regenerate START  idx=%s", branch_point_index)

    name = "Re: " + user_msg_content[:12].strip()
    if len(user_msg_content) > 12:
        name += "…"

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    app_module._wait_extract_done(story_id, parent_branch_id, branch_point_index)
    forked_state = app_module._find_state_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._backfill_forked_state(forked_state, story_id, source_branch_id)
    app_module._save_json(app_module._story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = app_module._find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._save_json(app_module._story_npcs_path(story_id, branch_id), forked_npcs)
    app_module.rebuild_state_db_from_json(story_id, branch_id, state=forked_state, npcs=forked_npcs)
    app_module._save_branch_config(
        story_id, branch_id, app_module._load_branch_config(story_id, source_branch_id)
    )
    app_module.copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = app_module._find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    app_module.set_world_day(story_id, branch_id, forked_world_day)
    app_module.copy_cheats(app_module._story_dir(story_id), source_branch_id, branch_id)
    app_module._copy_branch_lore_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module.copy_events_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module._copy_gm_plan(
        story_id, source_branch_id, branch_id, branch_point_index=branch_point_index
    )
    app_module.copy_dungeon_progress(story_id, parent_branch_id, branch_id)
    app_module._copy_debug_directive(story_id, source_branch_id, branch_id)

    app_module._save_branch_messages(story_id, branch_id, [])

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    app_module._clear_loaded_save_preview(tree)
    app_module._save_tree(story_id, tree)

    t0 = time.time()
    full_timeline = app_module.get_full_timeline(story_id, branch_id)
    state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id)
    state_text = app_module._build_core_state_text(story_id, state)
    recap_text = app_module.get_recap_text(story_id, branch_id)
    system_prompt = app_module._build_story_system_prompt(
        story_id,
        state_text,
        branch_id=branch_id,
        narrative_recap=recap_text,
        npcs=npcs,
        state_dict=state,
    )
    recent = app_module._sanitize_recent_messages(
        full_timeline[-app_module.RECENT_MESSAGE_COUNT :],
        strip_fate=not app_module.get_fate_mode(app_module._story_dir(story_id), branch_id),
    )
    log.info("  build_prompt: %.0fms", (time.time() - t0) * 1000)

    t0 = time.time()
    turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
    augmented_regen, dice_result = app_module._build_augmented_message(
        story_id,
        branch_id,
        user_msg_content,
        state,
        npcs=npcs,
        recent_messages=recent,
        turn_count=turn_count,
        current_index=branch_point_index,
    )
    log.info("  context_search: %.0fms", (time.time() - t0) * 1000)

    gm_msg_index = branch_point_index + 1
    app_module._trace_llm(
        stage="gm_request",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/branches/regenerate",
        payload={
            "user_text": user_msg_content,
            "augmented_text": augmented_regen,
            "system_prompt": system_prompt,
            "recent": recent,
            "dice_result": dice_result,
        },
        tags={"mode": "sync"},
    )

    t0 = time.time()
    try:
        gm_response, _ = app_module.call_claude_gm(
            augmented_regen, system_prompt, recent, session_id=None
        )
    except Exception as exc:
        log.info("/api/branches/regenerate EXCEPTION %s", exc)
        app_module._cleanup_branch(story_id, branch_id)
        return jsonify({"ok": False, "error": str(exc)}), 500
    regen_elapsed = time.time() - t0
    log.info("  claude_call: %.1fs", regen_elapsed)
    app_module._log_llm_usage(story_id, "gm", regen_elapsed, branch_id=branch_id)
    app_module._trace_llm(
        stage="gm_response_raw",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/branches/regenerate",
        payload={"response": gm_response, "usage": app_module.get_last_usage()},
        tags={"mode": "sync"},
    )

    gm_response, image_info, snapshots = app_module._process_gm_response(
        gm_response, story_id, branch_id, gm_msg_index
    )

    gm_msg = {"role": "gm", "content": gm_response, "index": gm_msg_index}
    if image_info:
        gm_msg["image"] = image_info
    if dice_result:
        gm_msg["dice"] = dice_result
    gm_msg.update(snapshots)
    app_module._save_branch_messages(story_id, branch_id, [gm_msg])

    recap = app_module.load_recap(story_id, branch_id)
    if app_module.should_compact(recap, len(full_timeline) + 1):
        timeline_with_reply = list(full_timeline) + [gm_msg]
        app_module.compact_async(story_id, branch_id, timeline_with_reply)

    log.info("/api/branches/regenerate DONE   total=%.1fs", time.time() - t_start)
    return jsonify({"ok": True, "branch": tree["branches"][branch_id], "gm_msg": gm_msg})


@branch_bp.route("/api/branches/regenerate/stream", methods=["POST"])
def api_branches_regenerate_stream():
    """Streaming version of /api/branches/regenerate — returns SSE events."""
    app_module = _app()
    body = request.get_json(force=True)
    parent_branch_id = body.get("parent_branch_id", "main")
    branch_point_index = body.get("branch_point_index")

    if branch_point_index is None:
        return Response(
            app_module._sse_event({"type": "error", "message": "branch_point_index required"}),
            mimetype="text/event-stream",
        )

    story_id = app_module._active_story_id()
    tree = app_module._load_tree(story_id)
    branches = tree.get("branches", {})
    source_branch_id = parent_branch_id
    source_timeline = app_module.get_full_timeline(story_id, source_branch_id)
    user_msg = app_module._find_timeline_message(source_timeline, branch_point_index, role="user")
    gm_msg = app_module._find_timeline_message(
        source_timeline, branch_point_index + 1, role=("gm", "assistant")
    )
    if not user_msg or not gm_msg:
        return Response(
            app_module._sse_event({"type": "error", "message": "invalid_regenerate_target"}),
            mimetype="text/event-stream",
        )
    parent_branch_id = app_module._resolve_sibling_parent(branches, parent_branch_id, branch_point_index)
    if parent_branch_id not in branches:
        return Response(
            app_module._sse_event({"type": "error", "message": "parent branch not found"}),
            mimetype="text/event-stream",
        )

    user_msg_content = user_msg.get("content", "")

    log.info("/api/branches/regenerate/stream START  idx=%s", branch_point_index)

    name = "Re: " + user_msg_content[:12].strip()
    if len(user_msg_content) > 12:
        name += "…"

    branch_id = f"branch_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()

    app_module._wait_extract_done(story_id, parent_branch_id, branch_point_index)
    forked_state = app_module._find_state_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._backfill_forked_state(forked_state, story_id, source_branch_id)
    app_module._save_json(app_module._story_character_state_path(story_id, branch_id), forked_state)
    forked_npcs = app_module._find_npcs_at_index(story_id, parent_branch_id, branch_point_index)
    app_module._save_json(app_module._story_npcs_path(story_id, branch_id), forked_npcs)
    app_module.rebuild_state_db_from_json(story_id, branch_id, state=forked_state, npcs=forked_npcs)
    app_module._save_branch_config(
        story_id, branch_id, app_module._load_branch_config(story_id, source_branch_id)
    )
    app_module.copy_recap_to_branch(story_id, parent_branch_id, branch_id, branch_point_index)
    forked_world_day = app_module._find_world_day_at_index(story_id, parent_branch_id, branch_point_index)
    app_module.set_world_day(story_id, branch_id, forked_world_day)
    app_module.copy_cheats(app_module._story_dir(story_id), source_branch_id, branch_id)
    app_module._copy_branch_lore_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module.copy_events_for_fork(story_id, source_branch_id, branch_id, branch_point_index)
    app_module._copy_gm_plan(
        story_id, source_branch_id, branch_id, branch_point_index=branch_point_index
    )
    app_module.copy_dungeon_progress(story_id, parent_branch_id, branch_id)
    app_module._save_branch_messages(story_id, branch_id, [])

    branches[branch_id] = {
        "id": branch_id,
        "name": name,
        "parent_branch_id": parent_branch_id,
        "branch_point_index": branch_point_index,
        "created_at": now,
        "session_id": None,
        "character_state_file": f"character_state_{branch_id}.json",
    }
    tree["active_branch_id"] = branch_id
    tree["last_played_branch_id"] = branch_id
    app_module._clear_loaded_save_preview(tree)
    app_module._save_tree(story_id, tree)

    full_timeline = app_module.get_full_timeline(story_id, branch_id)
    state = app_module._load_character_state(story_id, branch_id)
    npcs = app_module._load_npcs(story_id, branch_id)
    state_text = app_module._build_core_state_text(story_id, state)
    recap_text = app_module.get_recap_text(story_id, branch_id)
    system_prompt = app_module._build_story_system_prompt(
        story_id,
        state_text,
        branch_id=branch_id,
        narrative_recap=recap_text,
        npcs=npcs,
        state_dict=state,
    )
    recent = app_module._sanitize_recent_messages(
        full_timeline[-app_module.RECENT_MESSAGE_COUNT :],
        strip_fate=not app_module.get_fate_mode(app_module._story_dir(story_id), branch_id),
    )
    turn_count = sum(1 for message in full_timeline if message.get("role") == "user")
    augmented_regen, dice_result = app_module._build_augmented_message(
        story_id,
        branch_id,
        user_msg_content,
        state,
        npcs=npcs,
        recent_messages=recent,
        turn_count=turn_count,
        current_index=branch_point_index,
    )

    gm_msg_index = branch_point_index + 1
    app_module._trace_llm(
        stage="gm_request",
        story_id=story_id,
        branch_id=branch_id,
        message_index=gm_msg_index,
        source="/api/branches/regenerate/stream",
        payload={
            "user_text": user_msg_content,
            "augmented_text": augmented_regen,
            "system_prompt": system_prompt,
            "recent": recent,
            "dice_result": dice_result,
        },
        tags={"mode": "stream"},
    )

    def generate():
        t_start = time.time()
        if dice_result:
            yield app_module._sse_event({"type": "dice", "dice": dice_result})
        try:
            for event_type, payload in app_module.call_claude_gm_stream(
                augmented_regen, system_prompt, recent, session_id=None
            ):
                if event_type == "text":
                    yield app_module._sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    app_module._cleanup_branch(story_id, branch_id)
                    yield app_module._sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    gm_response = payload["response"]
                    app_module._log_llm_usage(
                        story_id,
                        "gm_stream",
                        time.time() - t_start,
                        branch_id=branch_id,
                        usage=payload.get("usage"),
                    )
                    app_module._trace_llm(
                        stage="gm_response_raw",
                        story_id=story_id,
                        branch_id=branch_id,
                        message_index=gm_msg_index,
                        source="/api/branches/regenerate/stream",
                        payload={"response": gm_response, "usage": payload.get("usage")},
                        tags={"mode": "stream"},
                    )

                    gm_response, image_info, snapshots = app_module._process_gm_response(
                        gm_response, story_id, branch_id, gm_msg_index
                    )

                    gm_msg = {"role": "gm", "content": gm_response, "index": gm_msg_index}
                    if image_info:
                        gm_msg["image"] = image_info
                    if dice_result:
                        gm_msg["dice"] = dice_result
                    gm_msg.update(snapshots)
                    app_module._save_branch_messages(story_id, branch_id, [gm_msg])

                    recap = app_module.load_recap(story_id, branch_id)
                    if app_module.should_compact(recap, len(full_timeline) + 1):
                        timeline_with_reply = list(full_timeline) + [gm_msg]
                        app_module.compact_async(story_id, branch_id, timeline_with_reply)

                    log.info("/api/branches/regenerate/stream DONE total=%.1fs", time.time() - t_start)
                    yield app_module._sse_event(
                        {
                            "type": "done",
                            "branch": tree["branches"][branch_id],
                            "gm_msg": gm_msg,
                        }
                    )
        except Exception as exc:
            import traceback

            log.info(
                "/api/branches/regenerate/stream EXCEPTION %s\n%s",
                exc,
                traceback.format_exc(),
            )
            app_module._cleanup_branch(story_id, branch_id)
            yield app_module._sse_event({"type": "error", "message": str(exc)})
        finally:
            messages = app_module._load_branch_messages(story_id, branch_id)
            if not messages:
                log.info("/api/branches/regenerate/stream cleanup orphan branch %s", branch_id)
                app_module._cleanup_branch(story_id, branch_id)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")
