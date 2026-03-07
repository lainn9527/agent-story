from __future__ import annotations

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context
import json
import logging
import re
import time


log = logging.getLogger("rpg")
lore_bp = Blueprint("lore", __name__)

_LORE_PROPOSE_RE = re.compile(r"<!--LORE_PROPOSE\s*(.*?)\s*LORE_PROPOSE-->", re.DOTALL)


def _app():
    import app as app_module

    return app_module


@lore_bp.route("/api/lore/search")
def api_lore_search():
    """Search world lore. Query params: q, tags, limit."""
    from story_core.lore_db import get_all_entries, search_by_tags, search_lore

    app_module = _app()
    story_id = app_module._active_story_id()
    query = request.args.get("q", "").strip()
    tags = request.args.get("tags", "").strip()
    limit = int(request.args.get("limit", "10"))

    if query:
        results = search_lore(story_id, query, limit=limit)
    elif tags:
        tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
        results = search_by_tags(story_id, tag_list, limit=limit)
    else:
        results = get_all_entries(story_id)

    return jsonify({"ok": True, "results": results, "count": len(results)})


@lore_bp.route("/api/lore/rebuild", methods=["POST"])
def api_lore_rebuild():
    app_module = _app()
    story_id = app_module._active_story_id()
    app_module.rebuild_lore_index(story_id)
    return jsonify({"ok": True, "message": "lore index rebuilt"})


@lore_bp.route("/api/lore/duplicates")
def api_lore_duplicates():
    app_module = _app()
    story_id = request.args.get("story_id") or app_module._active_story_id()
    try:
        threshold = float(request.args.get("threshold", "0.90"))
    except (ValueError, TypeError):
        threshold = 0.90
    threshold = max(0.5, min(1.0, threshold))
    pairs = app_module.find_duplicates(story_id, threshold=threshold)
    return jsonify({"ok": True, "pairs": pairs, "count": len(pairs), "threshold": threshold})


@lore_bp.route("/api/lore/embedding-stats")
def api_lore_embedding_stats():
    app_module = _app()
    story_id = request.args.get("story_id") or app_module._active_story_id()
    stats = app_module.get_embedding_stats(story_id)
    return jsonify({"ok": True, **stats})


@lore_bp.route("/lore")
def lore_page():
    return render_template("lore.html")


@lore_bp.route("/api/lore/all")
def api_lore_all():
    app_module = _app()
    story_id = app_module._active_story_id()
    branch_id = request.args.get("branch_id")
    if not branch_id:
        tree = app_module._load_tree(story_id)
        branch_id = tree.get("active_branch_id", "main")

    base = app_module._load_lore(story_id)
    for entry in base:
        entry["layer"] = "base"
    branch = app_module._load_branch_lore(story_id, branch_id)
    for entry in branch:
        entry["layer"] = "branch"
    all_entries = base + branch
    categories = list(dict.fromkeys(entry.get("category", "其他") for entry in all_entries))
    return jsonify({"ok": True, "entries": all_entries, "categories": categories, "branch_id": branch_id})


@lore_bp.route("/api/lore/entry", methods=["POST"])
def api_lore_entry_create():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    category = body.get("category", "其他").strip()
    content = body.get("content", "").strip()
    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400

    subcategory = body.get("subcategory", "").strip()
    lore = app_module._load_lore(story_id)
    for entry in lore:
        if entry.get("topic") == topic and entry.get("subcategory", "") == subcategory:
            return jsonify({"ok": False, "error": f"topic '{topic}' already exists in this subcategory"}), 409

    entry = {"category": category, "topic": topic, "content": content, "edited_by": "user"}
    if subcategory:
        entry["subcategory"] = subcategory
    app_module._save_lore_entry(story_id, entry)
    return jsonify({"ok": True, "entry": entry})


@lore_bp.route("/api/lore/entry", methods=["PUT"])
def api_lore_entry_update():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400

    requested_subcategory = body.get("subcategory", "").strip()
    lock = app_module.get_lore_lock(story_id)
    with lock:
        lore = app_module._load_lore(story_id)
        for index, entry in enumerate(lore):
            if entry.get("topic") != topic or entry.get("subcategory", "") != requested_subcategory:
                continue
            new_topic = body.get("new_topic", topic).strip()
            new_category = body.get("category", entry.get("category", "其他")).strip()
            new_content = body.get("content", entry.get("content", "")).strip()
            new_subcategory = body["subcategory"].strip() if "subcategory" in body else entry.get("subcategory", "")
            if new_topic != topic or new_subcategory != entry.get("subcategory", ""):
                collision = any(
                    other is not entry
                    and other.get("topic") == new_topic
                    and other.get("subcategory", "") == new_subcategory
                    for other in lore
                )
                if collision:
                    return jsonify({"ok": False, "error": f"topic '{new_topic}' already exists in this subcategory"}), 409
            if new_topic != topic or new_subcategory != requested_subcategory:
                app_module.delete_lore_entry(story_id, topic, requested_subcategory)
            updated = {
                "category": new_category,
                "topic": new_topic,
                "content": new_content,
                "edited_by": "user",
            }
            if "subcategory" in body:
                if new_subcategory:
                    updated["subcategory"] = new_subcategory
            elif entry.get("subcategory"):
                updated["subcategory"] = entry["subcategory"]
            if "source" in entry:
                updated["source"] = entry["source"]
            lore[index] = updated
            app_module._save_json(app_module._story_lore_path(story_id), lore)
            app_module.upsert_lore_entry(story_id, lore[index])
            return jsonify({"ok": True, "entry": lore[index]})
    return jsonify({"ok": False, "error": "entry not found"}), 404


@lore_bp.route("/api/lore/entry", methods=["DELETE"])
def api_lore_entry_delete():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    subcategory = body.get("subcategory", "").strip()
    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400

    lock = app_module.get_lore_lock(story_id)
    with lock:
        lore = app_module._load_lore(story_id)
        new_lore = [
            entry for entry in lore
            if not (entry.get("topic") == topic and entry.get("subcategory", "") == subcategory)
        ]
        if len(new_lore) == len(lore):
            return jsonify({"ok": False, "error": "entry not found"}), 404
        app_module._save_json(app_module._story_lore_path(story_id), new_lore)
        app_module.delete_lore_entry(story_id, topic, subcategory)
    return jsonify({"ok": True})


@lore_bp.route("/api/lore/branch/entry", methods=["DELETE"])
def api_branch_lore_entry_delete():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    topic = body.get("topic", "").strip()
    subcategory = body.get("subcategory", "").strip()
    branch_id = body.get("branch_id", "")
    if not topic or not branch_id:
        return jsonify({"ok": False, "error": "topic and branch_id required"}), 400

    lore = app_module._load_branch_lore(story_id, branch_id)
    new_lore = [
        entry for entry in lore
        if not (entry.get("topic") == topic and entry.get("subcategory", "") == subcategory)
    ]
    if len(new_lore) == len(lore):
        return jsonify({"ok": False, "error": "entry not found"}), 404
    app_module._save_branch_lore(story_id, branch_id, new_lore)
    return jsonify({"ok": True})


@lore_bp.route("/api/lore/promote/review", methods=["POST"])
def api_lore_promote_review():
    from story_core.llm_bridge import call_oneshot

    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "")
    if not branch_id:
        return jsonify({"ok": False, "error": "branch_id required"}), 400

    branch_lore = app_module._load_branch_lore(story_id, branch_id)
    if not branch_lore:
        return jsonify({"ok": True, "proposals": []})

    base_toc = app_module.get_lore_toc(story_id)
    entries_text = ""
    for index, entry in enumerate(branch_lore):
        entries_text += f"\n### 條目 {index + 1}\n"
        entries_text += f"分類: {entry.get('category', '')}\n"
        entries_text += f"主題: {entry.get('topic', '')}\n"
        entries_text += f"內容: {entry.get('content', '')}\n"

    prompt = (
        "你是一個 RPG 世界設定審核員。以下是從某個分支冒險中自動提取的設定條目。\n"
        "請判斷每個條目是否適合提升為永久世界設定（base lore），還是只是特定角色的經驗。\n\n"
        f"## 已有的永久世界設定\n{base_toc}\n\n"
        f"## 待審核的分支設定\n{entries_text}\n\n"
        "## 審核規則\n"
        "對每個條目選擇一個動作：\n"
        "- **promote**: 純粹的世界觀設定（體系規則、副本背景、場景描述、NPC通用資料等），可以直接提升\n"
        "- **rewrite**: 混合內容（包含世界設定但也含有特定角色名稱/經歷），需要改寫後提升。提供 rewritten_content，移除角色特定內容，只保留通用世界設定\n"
        "- **reject**: 純粹的角色經驗（角色完成了X、角色獲得了Y、角色的狀態等），不適合提升\n\n"
        "## 輸出格式\n"
        "JSON 陣列，每個元素：\n"
        '[{"index": 0, "action": "promote|rewrite|reject", "reason": "簡短理由", "rewritten_content": "改寫後內容（僅 rewrite 時提供）"}]\n'
        "只輸出 JSON。"
    )

    app_module._trace_llm(
        stage="lore_promote_review_request",
        story_id=story_id,
        branch_id=branch_id,
        source="/api/lore/promote/review",
        payload={"prompt": prompt, "entry_count": len(branch_lore)},
        tags={"mode": "oneshot"},
    )
    started = time.time()
    result = call_oneshot(prompt)
    app_module._log_llm_usage(story_id, "oneshot", time.time() - started)
    app_module._trace_llm(
        stage="lore_promote_review_response_raw",
        story_id=story_id,
        branch_id=branch_id,
        source="/api/lore/promote/review",
        payload={"response": result, "usage": app_module.get_last_usage()},
        tags={"mode": "oneshot"},
    )

    if not result:
        return jsonify({"ok": False, "error": "LLM call failed"}), 500

    result = result.strip()
    if result.startswith("```"):
        lines = result.split("\n")
        result = "\n".join(line for line in lines if not line.startswith("```"))

    try:
        proposals = json.loads(result)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", result, re.DOTALL)
        if not match:
            return jsonify({"ok": False, "error": "failed to parse LLM response"}), 500
        try:
            proposals = json.loads(match.group())
        except json.JSONDecodeError:
            return jsonify({"ok": False, "error": "failed to parse LLM response"}), 500

    enriched = []
    for proposal in proposals:
        index = proposal.get("index", -1)
        if 0 <= index < len(branch_lore):
            entry = branch_lore[index]
            enriched.append(
                {
                    "index": index,
                    "action": proposal.get("action", "reject"),
                    "reason": proposal.get("reason", ""),
                    "topic": entry.get("topic", ""),
                    "category": entry.get("category", ""),
                    "content": entry.get("content", ""),
                    "rewritten_content": proposal.get("rewritten_content", ""),
                }
            )
    return jsonify({"ok": True, "proposals": enriched})


@lore_bp.route("/api/lore/promote", methods=["POST"])
def api_lore_promote():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    branch_id = body.get("branch_id", "")
    topic = body.get("topic", "").strip()
    subcategory = body.get("subcategory", "").strip()
    content_override = body.get("content", "").strip()
    if not branch_id or not topic:
        return jsonify({"ok": False, "error": "branch_id and topic required"}), 400

    branch_lore = app_module._load_branch_lore(story_id, branch_id)
    entry = next(
        (
            item for item in branch_lore
            if item.get("topic") == topic and item.get("subcategory", "") == subcategory
        ),
        None,
    )
    if not entry:
        return jsonify({"ok": False, "error": "entry not found in branch lore"}), 404

    base_entry = {
        "category": entry.get("category", "其他"),
        "topic": topic,
        "content": content_override or entry.get("content", ""),
        "edited_by": "user",
    }
    if entry.get("subcategory"):
        base_entry["subcategory"] = entry["subcategory"]
    if "source" in entry:
        base_entry["source"] = entry["source"]

    app_module._save_lore_entry(story_id, base_entry)
    new_branch_lore = [
        item for item in branch_lore
        if not (item.get("topic") == topic and item.get("subcategory", "") == subcategory)
    ]
    app_module._save_branch_lore(story_id, branch_id, new_branch_lore)
    return jsonify({"ok": True, "entry": base_entry})


@lore_bp.route("/api/lore/chat/stream", methods=["POST"])
def api_lore_chat_stream():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    messages = body.get("messages", [])
    if not messages:
        return Response(app_module._sse_event({"type": "error", "message": "no messages"}), mimetype="text/event-stream")

    lore = app_module._load_lore(story_id)
    lore_text_parts = []
    from collections import OrderedDict

    groups = OrderedDict()
    for entry in lore:
        category = entry.get("category", "其他")
        subcategory = entry.get("subcategory", "")
        key = f"{category}/{subcategory}" if subcategory else category
        groups.setdefault(key, []).append(entry)
    for key, entries in groups.items():
        lore_text_parts.append(f"### 【{key}】")
        for entry in entries:
            lore_text_parts.append(f"#### {entry['topic']}")
            lore_text_parts.append(entry.get("content", ""))
            lore_text_parts.append("")

    category_list = ", ".join(dict.fromkeys(entry.get("category", "其他") for entry in lore)) if lore else "其他"
    lore_system = f"""你是世界設定管理助手，協助維護 RPG 世界的設定知識庫。

角色：討論/新增/修改/刪除設定，確保一致性，用繁體中文回覆。
重要：變更會即時同步到遊戲中，影響 GM 的下一次回覆。

現有分類：{category_list}
現有設定（{len(lore)} 條）：
{chr(10).join(lore_text_parts)}

設定格式規範：
- 設定內容可包含 [tag: 標籤1/標籤2] 用於搜尋分類（例：[tag: 體系/戰鬥]）
- 設定內容可包含 [source: 來源] 標記參考資料
- 設定會透過關鍵字搜尋注入 GM 上下文，請使用明確的術語和關鍵字以提升檢索效果
- 新增設定時請使用上方現有分類，避免建立新分類

子分類（subcategory）規範：
- 副本世界觀 的條目：subcategory = 副本名稱（如「海賊王」「生化危機」）。首條總覽條目 topic = 「介紹」；後續條目用具體名稱（如「T病毒」「追蹤者」）
- 體系 的條目：subcategory = 體系名稱（如「霸氣」「基因鎖」）。首條總覽條目 topic = 「介紹」；後續條目用具體名稱
- 場景 的條目：subcategory 建議填對應副本名稱（與副本世界觀對應），topic 為場景名稱
- 其他分類：subcategory 可選，不強制
- 同一 subcategory 下的 topic 必須唯一，但不同 subcategory 間 topic 可重複（例如每個副本都可有「介紹」）
- delete 操作以 (subcategory + topic) 聯合識別，請同時提供 subcategory 以精確刪除

提案格式（當建議變更時使用）：
<!--LORE_PROPOSE {{"action":"add|edit|delete", "category":"...", "subcategory":"...", "topic":"...", "content":"..."}} LORE_PROPOSE-->

規則：
- 先討論再提案，確認用戶意圖後再輸出提案標籤
- content 欄位是完整的設定文字（不是差異）
- delete 操作不需要 content 欄位
- 可在一次回覆中輸出多個提案標籤
- 提案標籤必須放在回覆最末尾"""

    provider = app_module.get_provider()
    tools = None
    if provider == "gemini":
        tools = [{"googleSearch": {}}]
        lore_system += """

網路搜尋：
- 你可以使用 Google Search 搜尋外部資料（動漫/小說/遊戲設定等）
- 當用戶提到外部作品或需要查證資料時，主動搜尋以獲得準確資訊
- 搜尋到的資料可以作為建議設定的依據"""

    prior = [{"role": message.get("role", "user"), "content": message.get("content", "")} for message in messages[:-1]]
    last_user_message = messages[-1].get("content", "")
    app_module._trace_llm(
        stage="lore_chat_request",
        story_id=story_id,
        branch_id="",
        source="/api/lore/chat/stream",
        payload={
            "user_text": last_user_message,
            "system_prompt": lore_system,
            "recent": prior,
            "tools": tools,
        },
        tags={"mode": "stream"},
    )

    def generate():
        started = time.time()
        try:
            for event_type, payload in app_module.call_claude_gm_stream(
                last_user_message,
                lore_system,
                prior,
                session_id=None,
                tools=tools,
            ):
                if event_type == "text":
                    yield app_module._sse_event({"type": "text", "chunk": payload})
                elif event_type == "error":
                    yield app_module._sse_event({"type": "error", "message": payload})
                    return
                elif event_type == "done":
                    app_module._log_llm_usage(
                        story_id,
                        "lore_chat",
                        time.time() - started,
                        usage=payload.get("usage"),
                    )
                    full_response = payload.get("response", "")
                    app_module._trace_llm(
                        stage="lore_chat_response_raw",
                        story_id=story_id,
                        branch_id="",
                        source="/api/lore/chat/stream",
                        payload={"response": full_response, "usage": payload.get("usage")},
                        tags={"mode": "stream"},
                    )
                    proposals = []
                    for match in _LORE_PROPOSE_RE.finditer(full_response):
                        try:
                            proposals.append(json.loads(match.group(1)))
                        except json.JSONDecodeError:
                            pass
                    display_text = _LORE_PROPOSE_RE.sub("", full_response).strip()
                    done_event = {"type": "done", "response": display_text, "proposals": proposals}
                    if payload.get("grounding"):
                        done_event["grounding"] = payload["grounding"]
                    yield app_module._sse_event(done_event)
        except Exception as exc:
            log.info("/api/lore/chat/stream EXCEPTION %s", exc)
            yield app_module._sse_event({"type": "error", "message": str(exc)})

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@lore_bp.route("/api/lore/apply", methods=["POST"])
def api_lore_apply():
    app_module = _app()
    story_id = app_module._active_story_id()
    body = request.get_json(force=True)
    proposals = body.get("proposals", [])
    applied = []
    for proposal in proposals:
        action = proposal.get("action", "").lower()
        topic = proposal.get("topic", "").strip()
        if not topic:
            continue
        if action in {"add", "edit"}:
            entry = {
                "category": proposal.get("category", "其他"),
                "topic": topic,
                "content": proposal.get("content", ""),
                "edited_by": "user",
            }
            if proposal.get("subcategory"):
                entry["subcategory"] = proposal["subcategory"]
            app_module._save_lore_entry(story_id, entry)
            applied.append({"action": action, "topic": topic})
        elif action == "delete":
            subcategory = proposal.get("subcategory", "").strip()
            lore = app_module._load_lore(story_id)
            new_lore = [
                entry for entry in lore
                if not (entry.get("topic") == topic and entry.get("subcategory", "") == subcategory)
            ]
            if len(new_lore) < len(lore):
                app_module._save_json(app_module._story_lore_path(story_id), new_lore)
                app_module.delete_lore_entry(story_id, topic, subcategory)
                applied.append({"action": "delete", "topic": topic})
    return jsonify({"ok": True, "applied": applied})

