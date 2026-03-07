"""GM plan helpers extracted from app_helpers."""

import copy
import logging
import os

from event_db import get_active_events
from story_io import _branch_dir, _load_json, _save_json

log = logging.getLogger("rpg")


def _parse_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("invalid %s=%r, using default %s", name, raw, default)
        return default


GM_PLAN_CHAR_LIMIT = max(120, _parse_env_int("GM_PLAN_CHAR_LIMIT", 500))


def _gm_plan_path(story_id: str, branch_id: str) -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "gm_plan.json")


def _load_gm_plan(story_id: str, branch_id: str) -> dict:
    data = _load_json(_gm_plan_path(story_id, branch_id), {})
    return data if isinstance(data, dict) else {}


def _save_gm_plan(story_id: str, branch_id: str, plan: dict):
    path = _gm_plan_path(story_id, branch_id)
    if not isinstance(plan, dict) or not plan:
        if os.path.exists(path):
            os.remove(path)
        return
    _save_json(path, plan)


def _safe_int(value: object, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _relink_plan_payoffs_by_title(payoffs: object, active_event_rows: list[dict],
                                  default_created_index: int) -> list[dict]:
    if not isinstance(payoffs, list):
        return []

    id_to_title: dict[int, str] = {}
    title_to_id: dict[str, int] = {}
    for row in active_event_rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        eid = _safe_int(row.get("id"))
        if not title or eid is None:
            continue
        id_to_title[eid] = title
        if title not in title_to_id:
            title_to_id[title] = eid

    linked: list[dict] = []
    seen_titles: set[str] = set()
    for raw in payoffs:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("event_title", "")).strip()
        if not title or title in seen_titles:
            continue

        raw_event_id = _safe_int(raw.get("event_id"))
        resolved_event_id: int | None = None
        if raw_event_id is not None and id_to_title.get(raw_event_id) == title:
            resolved_event_id = raw_event_id
        else:
            resolved_event_id = title_to_id.get(title)
        if resolved_event_id is None:
            continue

        ttl = _safe_int(raw.get("ttl_turns"), 3) or 3
        ttl = max(1, min(ttl, 6))
        created_at_index = _safe_int(raw.get("created_at_index"), default_created_index)
        if created_at_index is None:
            created_at_index = default_created_index

        linked.append({
            "event_title": title,
            "event_id": resolved_event_id,
            "ttl_turns": ttl,
            "created_at_index": created_at_index,
        })
        seen_titles.add(title)
    return linked


def _normalize_gm_plan_payload(raw_plan: dict, previous_plan: dict, msg_index: int,
                               active_event_rows: list[dict]) -> dict | None:
    """Normalize extracted plan payload.

    Return semantics:
    - dict: valid plan content to persist
    - {}:   valid but empty plan => clear existing gm_plan.json
    - None: invalid payload => ignore, keep existing plan
    """
    if not isinstance(raw_plan, dict):
        return None

    arc = str(raw_plan.get("arc", "")).strip()
    arc = arc[:120]

    next_beats: list[str] = []
    for beat in raw_plan.get("next_beats", []) if isinstance(raw_plan.get("next_beats"), list) else []:
        if not isinstance(beat, str):
            continue
        text = beat.strip()
        if not text or text in next_beats:
            continue
        next_beats.append(text[:80])
        if len(next_beats) >= 3:
            break

    prev_created_at: dict[str, int] = {}
    if isinstance(previous_plan, dict):
        for payoff in previous_plan.get("must_payoff", []) if isinstance(previous_plan.get("must_payoff"), list) else []:
            if not isinstance(payoff, dict):
                continue
            title = str(payoff.get("event_title", "")).strip()
            created_at = _safe_int(payoff.get("created_at_index"))
            if title and created_at is not None:
                prev_created_at[title] = created_at

    raw_payoffs: list[dict] = []
    for payoff in raw_plan.get("must_payoff", []) if isinstance(raw_plan.get("must_payoff"), list) else []:
        if not isinstance(payoff, dict):
            continue
        title = str(payoff.get("event_title", "")).strip()
        if not title:
            continue
        ttl = _safe_int(payoff.get("ttl_turns"), 3) or 3
        ttl = max(1, min(ttl, 6))
        created_at = prev_created_at.get(title)
        if created_at is None:
            created_at = _safe_int(payoff.get("created_at_index"), msg_index)
        if created_at is None:
            created_at = msg_index
        raw_payoffs.append({
            "event_title": title,
            "event_id": payoff.get("event_id"),
            "ttl_turns": ttl,
            "created_at_index": created_at,
        })

    linked_payoffs = _relink_plan_payoffs_by_title(
        raw_payoffs, active_event_rows, default_created_index=msg_index
    )

    if not arc and not next_beats and not linked_payoffs:
        return {}

    return {
        "arc": arc,
        "next_beats": next_beats,
        "must_payoff": linked_payoffs,
        "updated_at_index": msg_index,
    }


def _copy_gm_plan(story_id: str, from_bid: str, to_bid: str, branch_point_index: int | None = None):
    if from_bid == to_bid:
        return

    plan = _load_gm_plan(story_id, from_bid)
    if not plan:
        if branch_point_index is None:
            _save_gm_plan(story_id, to_bid, {})
        return

    if branch_point_index is not None:
        updated_at = _safe_int(plan.get("updated_at_index"))
        if updated_at is None or updated_at > branch_point_index:
            return

    copied = copy.deepcopy(plan)
    copied["arc"] = str(copied.get("arc", "")).strip()[:120]
    copied["next_beats"] = [
        str(b).strip()[:80]
        for b in copied.get("next_beats", [])
        if isinstance(b, str) and str(b).strip()
    ][:3]
    updated_at_index = _safe_int(copied.get("updated_at_index"), 0) or 0
    active_event_rows = get_active_events(story_id, to_bid, limit=80)
    copied["must_payoff"] = _relink_plan_payoffs_by_title(
        copied.get("must_payoff", []),
        active_event_rows,
        default_created_index=updated_at_index,
    )

    if not copied["arc"] and not copied["next_beats"] and not copied["must_payoff"]:
        _save_gm_plan(story_id, to_bid, {})
        return
    _save_gm_plan(story_id, to_bid, copied)


def _compute_payoff_remaining(payoff: dict, current_index: int) -> int:
    ttl = _safe_int(payoff.get("ttl_turns"), 3) or 3
    ttl = max(1, min(ttl, 6))
    created_at = _safe_int(payoff.get("created_at_index"), current_index)
    if created_at is None:
        created_at = current_index
    return ttl - max(0, current_index - created_at)


def _summarize_gm_plan_for_prompt(plan: dict, current_index: int) -> str:
    if not isinstance(plan, dict):
        return "（無）"

    lines: list[str] = []
    arc = str(plan.get("arc", "")).strip()
    if arc:
        lines.append(f"弧線：{arc}")

    beats = [
        str(b).strip()
        for b in plan.get("next_beats", [])
        if isinstance(b, str) and str(b).strip()
    ][:3]
    if beats:
        lines.append("節點：")
        for i, beat in enumerate(beats, 1):
            lines.append(f"{i}. {beat}")

    payoffs: list[str] = []
    for payoff in plan.get("must_payoff", []) if isinstance(plan.get("must_payoff"), list) else []:
        if not isinstance(payoff, dict):
            continue
        title = str(payoff.get("event_title", "")).strip()
        if not title:
            continue
        remaining = _compute_payoff_remaining(payoff, current_index)
        if remaining <= 0:
            continue
        payoffs.append(f"{title}（剩餘 {remaining} 回合）")
    if payoffs:
        lines.append("待回收：")
        lines.extend(f"- {p}" for p in payoffs[:2])

    return "\n".join(lines) if lines else "（無）"


def _build_gm_plan_injection_block(story_id: str, branch_id: str, current_index: int,
                                   char_limit: int = GM_PLAN_CHAR_LIMIT) -> str:
    plan = _load_gm_plan(story_id, branch_id)
    if not isinstance(plan, dict) or not plan:
        return ""

    arc = str(plan.get("arc", "")).strip()
    beats = [
        str(b).strip()
        for b in plan.get("next_beats", [])
        if isinstance(b, str) and str(b).strip()
    ][:3]

    payoffs: list[tuple[str, int]] = []
    for payoff in plan.get("must_payoff", []) if isinstance(plan.get("must_payoff"), list) else []:
        if not isinstance(payoff, dict):
            continue
        title = str(payoff.get("event_title", "")).strip()
        if not title:
            continue
        remaining = _compute_payoff_remaining(payoff, current_index)
        if remaining <= 0:
            continue
        payoffs.append((title, remaining))
    payoffs = payoffs[:2]

    if not arc and not beats and not payoffs:
        return ""

    header = "[GM 敘事計劃（僅供 GM 內部參考，勿透露給玩家）]"

    def _render(arc_text: str, beat_list: list[str], payoff_list: list[tuple[str, int]]) -> str:
        lines = [header]
        if arc_text:
            lines.append(f"- 當前弧線：{arc_text}")
        if beat_list:
            lines.append("- 接下來節點：")
            for i, beat in enumerate(beat_list, 1):
                lines.append(f"  {i}. {beat}")
        if payoff_list:
            lines.append("- 待回收伏筆：")
            for title, remaining in payoff_list:
                lines.append(f"  - {title}（剩餘 {remaining} 回合）")
        return "\n".join(lines)

    while True:
        block = _render(arc, beats, payoffs)
        if len(block) <= char_limit:
            return block
        if payoffs:
            payoffs = payoffs[:-1]
            continue
        if beats:
            beats = beats[:-1]
            continue
        if len(arc) > 24:
            arc = arc[: max(16, len(arc) - 12)].rstrip() + "…"
            continue
        return block[:char_limit].rstrip()


__all__ = [
    "GM_PLAN_CHAR_LIMIT",
    "_gm_plan_path",
    "_load_gm_plan",
    "_save_gm_plan",
    "_safe_int",
    "_relink_plan_payoffs_by_title",
    "_normalize_gm_plan_payload",
    "_copy_gm_plan",
    "_compute_payoff_remaining",
    "_summarize_gm_plan_for_prompt",
    "_build_gm_plan_injection_block",
]
