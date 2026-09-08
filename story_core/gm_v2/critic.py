"""GM v2 async critic (evaluation pillar C).

The deterministic gate (pillar B) drops structural errors for free; what it cannot
judge — *semantics* — it FLAGS. This critic reviews those flags OFF the critical
path (player already saw the story) with an INDEPENDENT, adversarial framing
(small bounded context, no narrative stake), then applies corrective deltas.

It handles two flag families:
  - state power/systems changes ("power_field" / "systems_change"): is this a
    permanent upgrade per the text, or a transient buff wrongly persisted? If
    transient → emit a corrective delta to remove it.
  - lore ("lore_needs_review"): is this general reusable setting or a one-off
    plot beat? If general → persist; else drop. (Lore is NOT written synchronously,
    so "drop" = simply not persisting.)

The LLM call is injectable (`llm_fn`) so the critic is unit-testable without a
provider.
"""

import json
import logging
import re
import threading

log = logging.getLogger("rpg")


def _default_llm(prompt: str) -> str:
    from story_core.llm_bridge import call_oneshot

    return call_oneshot(prompt)


def _app():
    import app as app_module

    return app_module


def build_critic_prompt(narration: str, flagged: list, delta: dict) -> str:
    state_flags = [f for f in flagged if f.get("key") in ("base_power_level", "gene_lock", "systems")]
    lore_items = delta.get("lore") or [] if any(f.get("reason") == "lore_needs_review" for f in flagged) else []
    return (
        "你是 RPG 狀態審核員。GM 剛產生了一段敘事與一份狀態變更，其中部分變更被標記需要語義審查。\n"
        "你的任務是判斷這些變更是否該保留，請以「找碴」的角度嚴格檢查。\n\n"
        f"## GM 敘事\n{narration[:1500]}\n\n"
        f"## 被標記的狀態變更\n{json.dumps(state_flags, ensure_ascii=False)}\n"
        f"對應的 state_delta：{json.dumps(delta.get('state_delta', {}), ensure_ascii=False)}\n\n"
        f"## 被標記的 lore\n{json.dumps(lore_items, ensure_ascii=False)}\n\n"
        "## 判斷原則\n"
        "- 能力/體系升級：只有敘事明確表示是『正式、穩定、永久』的升級才保留。一次性爆發、過載、暫時、邊緣觸及、外物催化 → 應撤銷。\n"
        "- lore：只有是『未來其他場景會參照的通用設定』才保留；一次性劇情 → 丟棄。\n\n"
        "## 輸出（嚴格 JSON）\n"
        '{"state_revert": {"systems": {"技能名": null}}, "lore_keep": [0], "reason": "一句話"}\n'
        "- state_revert：要撤銷的欄位 patch（map 用 null 移除、欄位用既有正確值覆蓋）。沒有就給 {}。\n"
        "- lore_keep：要保留的 lore 條目 index 陣列（其餘丟棄）。沒有 lore 就給 []。\n"
        "只輸出 JSON。"
    )


def _parse_json(text: object) -> dict:
    if not text:
        return {}
    s = str(text).strip()
    if s.startswith("```"):
        s = "\n".join(line for line in s.split("\n") if not line.startswith("```"))
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return {}
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def review_flagged(story_id, branch_id, narration, flagged, delta, *, llm_fn=None) -> dict:
    """Review flagged items and apply corrections. Returns the verdict + actions taken."""
    if not flagged:
        return {"verdicts": {}, "applied": []}
    llm_fn = llm_fn or _default_llm
    verdict = _parse_json(llm_fn(build_critic_prompt(narration, flagged, delta)))
    applied = []

    revert = verdict.get("state_revert")
    if isinstance(revert, dict) and revert:
        try:
            from story_core.gm_v2.apply import apply_state_delta
            apply_state_delta(story_id, branch_id, revert)
            applied.append({"action": "state_revert", "patch": revert})
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("gm_v2 critic: state_revert failed (%s)", exc)

    lore_items = delta.get("lore") or []
    keep = verdict.get("lore_keep")
    if lore_items and isinstance(keep, list):
        try:
            app = _app()
            for idx in keep:
                if isinstance(idx, int) and 0 <= idx < len(lore_items):
                    entry = dict(lore_items[idx])
                    entry["edited_by"] = "auto"
                    app._save_branch_lore_entry(story_id, branch_id, entry)
                    applied.append({"action": "lore_keep", "topic": entry.get("topic")})
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("gm_v2 critic: lore persist failed (%s)", exc)

    log.info("gm_v2 critic: %d flagged, applied %s", len(flagged), [a["action"] for a in applied])
    return {"verdicts": verdict, "applied": applied}


def review_flagged_async(story_id, branch_id, narration, flagged, delta, *, llm_fn=None):
    """Fire-and-forget critic on a daemon thread (off the player's critical path)."""
    if not flagged:
        return
    worker = threading.Thread(
        target=review_flagged,
        args=(story_id, branch_id, narration, flagged, delta),
        kwargs={"llm_fn": llm_fn},
        daemon=True,
    )
    worker.start()
    return worker


__all__ = ["build_critic_prompt", "review_flagged", "review_flagged_async"]
