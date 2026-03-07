from __future__ import annotations

from story_core.story_io import (
    _load_branch_messages,
    _load_json,
    _load_tree,
    _story_parsed_path,
)


def get_full_timeline(story_id: str, branch_id: str) -> list[dict]:
    """Reconstruct full message timeline for a branch within a story."""
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    parsed_path = _story_parsed_path(story_id)

    if branch_id not in branches:
        base = _load_json(parsed_path, [])
        for message in base:
            message["owner_branch_id"] = "main"
        return base

    chain = []
    current = branch_id
    visited = set()
    while current is not None and current not in visited:
        branch = branches.get(current)
        if not branch:
            break
        visited.add(current)
        chain.append(branch)
        current = branch.get("parent_branch_id")
    chain.reverse()

    base = _load_json(parsed_path, [])
    for message in base:
        message["owner_branch_id"] = chain[0]["id"]

    timeline = list(base)
    for branch in chain:
        branch_point_index = branch.get("branch_point_index")
        if branch_point_index is not None:
            timeline = [msg for msg in timeline if msg.get("index", 0) <= branch_point_index]

        delta = _load_branch_messages(story_id, branch["id"])
        for message in delta:
            message["owner_branch_id"] = branch["id"]
        timeline.extend(delta)

    return timeline


def _next_timeline_index(
    story_id: str,
    branch_id: str,
    timeline: list[dict] | None = None,
) -> int:
    """Return the next message index for appending in a branch timeline."""
    if timeline is None:
        timeline = get_full_timeline(story_id, branch_id)
    max_index = _max_message_index(timeline)
    if max_index is None:
        return 0
    return max_index + 1


def _coerce_message_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _max_message_index(messages: object) -> int | None:
    if not isinstance(messages, list):
        return None
    max_index: int | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        index = _coerce_message_index(message.get("index"))
        if index is None:
            continue
        if max_index is None or index > max_index:
            max_index = index
    return max_index


def _next_branch_message_index_fast(story_id: str, branch_id: str) -> int:
    """Cheap next-index lookup for append-only debug audit messages."""
    delta_max = _max_message_index(_load_branch_messages(story_id, branch_id))
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    branch = branches.get(branch_id, {})

    if branch_id == "main" or branch.get("parent_branch_id") is None:
        inherited_max = _max_message_index(_load_json(_story_parsed_path(story_id), []))
    else:
        branch_point_index = _coerce_message_index(branch.get("branch_point_index"))
        if branch_point_index is not None:
            inherited_max = branch_point_index
        else:
            inherited_max = _max_message_index(get_full_timeline(story_id, branch_id))

    max_index = inherited_max
    if delta_max is not None and (max_index is None or delta_max > max_index):
        max_index = delta_max
    if max_index is None:
        return 0
    return max_index + 1


def _find_timeline_message(
    timeline: list[dict],
    index: int,
    role: str | tuple[str, ...] | None = None,
) -> dict | None:
    """Return the first timeline message matching an index and optional role."""
    for message in timeline:
        if message.get("index") != index:
            continue
        if role:
            roles = (role,) if isinstance(role, str) else role
            if message.get("role") not in roles:
                continue
        return message
    return None


def _resolve_sibling_parent(branches: dict, parent_branch_id: str, branch_point_index: int) -> str:
    """Walk up ancestor chain for sibling detection."""
    current = parent_branch_id
    visited = set()
    while current in branches and current != "main" and current not in visited:
        visited.add(current)
        branch = branches[current]
        parent_bp = branch.get("branch_point_index")
        if parent_bp is not None and branch_point_index <= parent_bp:
            current = branch.get("parent_branch_id", "main") or "main"
        else:
            break
    return current


def _get_fork_points(story_id: str, branch_id: str) -> dict:
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})
    fork_points = {}

    ancestor_ids = set()
    current = branch_id
    while current is not None and current not in ancestor_ids:
        ancestor_ids.add(current)
        branch = branches.get(current)
        if not branch:
            break
        current = branch.get("parent_branch_id")

    for bid, branch in branches.items():
        if bid == branch_id or branch.get("deleted") or branch.get("blank") or branch.get("merged") or branch.get("pruned"):
            continue
        parent = branch.get("parent_branch_id")
        branch_point_index = branch.get("branch_point_index")
        if parent in ancestor_ids and branch_point_index is not None:
            fork_points.setdefault(branch_point_index, []).append(
                {
                    "branch_id": bid,
                    "branch_name": branch.get("name", bid),
                }
            )

    return fork_points


def _get_sibling_groups(story_id: str, branch_id: str) -> dict:
    tree = _load_tree(story_id)
    branches = tree.get("branches", {})

    if branch_id not in branches:
        return {}

    ancestor_ids = []
    current = branch_id
    visited = set()
    while current is not None and current not in visited:
        visited.add(current)
        ancestor_ids.append(current)
        branch = branches.get(current)
        if not branch:
            break
        current = branch.get("parent_branch_id")
    ancestor_ids.reverse()
    ancestor_set = set(ancestor_ids)

    sibling_groups = {}
    fork_map = {}
    for bid, branch in branches.items():
        if branch.get("deleted") or branch.get("blank") or branch.get("merged") or branch.get("pruned"):
            continue
        parent_id = branch.get("parent_branch_id")
        branch_point_index = branch.get("branch_point_index")
        if parent_id is not None and branch_point_index is not None and parent_id in ancestor_set:
            fork_map.setdefault((parent_id, branch_point_index), []).append(branch)

    parsed_path = _story_parsed_path(story_id)

    for (parent_id, branch_point_index), children in fork_map.items():
        children.sort(key=lambda branch: branch.get("created_at", ""))

        parent_delta = _load_branch_messages(story_id, parent_id)
        parent_has_continuation = any(message.get("index", 0) > branch_point_index for message in parent_delta)
        if parent_id == "main" and not parent_has_continuation:
            parsed = _load_json(parsed_path, [])
            parent_has_continuation = any(message.get("index", 0) > branch_point_index for message in parsed)

        variants = []
        if parent_has_continuation:
            variants.append(
                {
                    "branch_id": parent_id,
                    "label": branches[parent_id].get("name", parent_id),
                    "is_current": parent_id in ancestor_set and not any(
                        child["id"] in ancestor_set for child in children
                    ),
                }
            )

        for child in children:
            child_messages = _load_branch_messages(story_id, child["id"])
            if not child_messages:
                continue
            variants.append(
                {
                    "branch_id": child["id"],
                    "label": child.get("name", child["id"]),
                    "is_current": child["id"] in ancestor_set,
                }
            )

        if len(variants) >= 2:
            current_variant = 0
            for variant_index, variant in enumerate(variants):
                if variant["is_current"]:
                    current_variant = variant_index + 1
                    break

            divergent_index = branch_point_index + 1
            sibling_groups[str(divergent_index)] = {
                "current_variant": current_variant,
                "total": len(variants),
                "variants": variants,
            }

    return sibling_groups


__all__ = [
    "get_full_timeline",
    "_next_timeline_index",
    "_coerce_message_index",
    "_max_message_index",
    "_next_branch_message_index_fast",
    "_find_timeline_message",
    "_resolve_sibling_parent",
    "_get_fork_points",
    "_get_sibling_groups",
]
