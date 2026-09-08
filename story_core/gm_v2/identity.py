"""GM v2 Tier-3 NPC identity adjudication — an async LLM judge.

Identity ("are these two names the same character?") is a JUDGMENT, so it must
NOT be decided by deterministic name rules — those false-merge distinct entities
(e.g. 李青 / 李青山), destroying separate characters and their memory. Instead a
cheap+fast model votes (k-vote ensemble) with a CONSERVATIVE default (merge only
on a confident, unanimous "same"). Accuracy is bought with test-time compute, not
a bigger single call (eval: flash-lite 12/12, 0 false-merge).

Cost is bounded by FREQUENCY, not model price: a deterministic pre-filter only
fires the judge on surface-overlapping candidate pairs (recall filter — it does
NOT decide identity), and a per-branch verdict cache means each ambiguous pair is
judged at most once. So the judge fires only a handful of times per long session.
"""

import json
import logging
import os
import re
import threading
import unicodedata

log = logging.getLogger("rpg")

DEFAULT_JUDGE_MODEL = "gemini-3.1-flash-lite"

_PROMPT = (
    "你是 NPC 同一性裁決員。判斷下面兩個 NPC 是否指『同一個角色』。\n"
    "重要原則：共用姓氏、部分字相同、或一個是另一個的子字串，都**不足以**判為同一人。\n"
    "只有你有把握是同一角色（例如：同一人的全名 vs 簡稱、名字 + 括號註解、稱號變體）才判 same。\n"
    "不確定一律判 different——寧可分開，絕不可錯併。\n\n"
    "NPC A：{a}\n"
    "NPC B：{b}\n\n"
    "只輸出嚴格 JSON：{{\"same\": true或false, \"confidence\": 0到1的小數, \"reason\": \"一句話\"}}"
)


# ---------------------------------------------------------------------------
# LLM judge (k-vote ensemble, conservative)
# ---------------------------------------------------------------------------

def _fmt(name: str, ctx: str = "") -> str:
    return f"{name}（{ctx}）" if ctx else f"{name}"


def _call(prompt: str, model: str) -> str:
    from story_core.gemini_bridge import call_gemini_oneshot
    from story_core.llm_bridge import _get_config

    g = _get_config().get("gemini", {})
    return call_gemini_oneshot(prompt, gemini_cfg=g, model=model)


def _parse(text: object) -> dict | None:
    if not text:
        return None
    s = str(text).strip()
    if s.startswith("```"):
        s = "\n".join(line for line in s.split("\n") if not line.startswith("```"))
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(d, dict) or "same" not in d:
        return None
    try:
        conf = float(d.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return {"same": bool(d.get("same")), "confidence": conf, "reason": str(d.get("reason", ""))[:80]}


def judge_once(name_a, ctx_a, name_b, ctx_b, model, prompt=_PROMPT) -> dict | None:
    return _parse(_call(prompt.format(a=_fmt(name_a, ctx_a), b=_fmt(name_b, ctx_b)), model))


def judge_ensemble(name_a, ctx_a, name_b, ctx_b, model, k=3, min_conf=0.6, prompt=_PROMPT) -> dict:
    """k-vote ensemble. Conservative: merge only on a UNANIMOUS, confident 'same'.

    Retries dropped (unparseable) votes up to 2k attempts so a flaky-format model
    cannot silently under-sample the quorum (the gemma n<k robustness flaw).
    `prompt` selects the entity kind (NPC default, or _ITEM_PROMPT for inventory).
    """
    votes = []
    for _ in range(2 * k):
        if len(votes) >= k:
            break
        v = judge_once(name_a, ctx_a, name_b, ctx_b, model, prompt=prompt)
        if v:
            votes.append(v)
    if not votes:
        return {"decision": False, "same_votes": 0, "n": 0, "confidence": 0.0, "unanimous": False, "reason": "no valid votes"}
    same_votes = sum(1 for v in votes if v["same"])
    conf = sum(v["confidence"] for v in votes) / len(votes)
    unanimous = same_votes == 0 or same_votes == len(votes)
    decision = same_votes == len(votes) and len(votes) >= 2 and conf >= min_conf
    return {
        "decision": decision, "same_votes": same_votes, "n": len(votes),
        "confidence": round(conf, 2), "unanimous": unanimous,
        "reason": next((v["reason"] for v in votes), ""),
    }


def judge_cascade(name_a, ctx_a, name_b, ctx_b, cheap_model, strong_model, k=3, min_conf=0.6) -> dict:
    """Cheap ensemble first; escalate to the strong model when split/low-confidence."""
    cheap = judge_ensemble(name_a, ctx_a, name_b, ctx_b, cheap_model, k, min_conf)
    escalate = (not cheap["unanimous"]) or cheap["n"] == 0 or cheap["confidence"] < min_conf
    if not escalate:
        return {**cheap, "model": cheap_model, "escalated": False}
    strong = judge_ensemble(name_a, ctx_a, name_b, ctx_b, strong_model, k, min_conf)
    return {**strong, "model": strong_model, "escalated": True, "cheap_decision": cheap["decision"]}


# ---------------------------------------------------------------------------
# Deterministic pre-filter — RECALL only (does NOT decide identity).
# Casts a wide net for candidate pairs worth asking the judge about.
# ---------------------------------------------------------------------------

def _norm(name: str) -> str:
    """Strip to the bare CJK core (drop spaces, punctuation, parenthetical, ASCII)."""
    s = unicodedata.normalize("NFKC", str(name or "")).strip()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)       # drop parenthetical note
    s = re.sub(r"[A-Za-z0-9_.\- ]+", "", s)       # drop ASCII tokens / spaces
    return re.sub(r"\s+", "", s)


def _surface_overlap(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True                                # same CJK core (likely a variant)
    if na in nb or nb in na:
        return True                                # substring (李青 / 李青山 — judge decides)
    return len(na) >= 2 and len(nb) >= 2 and na[:2] == nb[:2]   # shared leading 2 chars


def find_candidate_pairs(npcs) -> list[tuple[str, str]]:
    """Distinct-named NPC pairs with surface overlap (worth asking the judge)."""
    names: list[str] = []
    for n in npcs or []:
        if isinstance(n, dict):
            nm = str(n.get("name", "")).strip()
            if nm and nm not in names:
                names.append(nm)
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if _surface_overlap(names[i], names[j]):
                pairs.append((names[i], names[j]))
    return pairs


# ---------------------------------------------------------------------------
# Verdict cache (per branch) — never re-judge the same pair.
# ---------------------------------------------------------------------------

def _cache_path(story_id, branch_id) -> str:
    from story_core.story_io import _branch_dir

    return os.path.join(_branch_dir(story_id, branch_id), "npc_identity_cache.json")


def _load_cache(story_id, branch_id) -> dict:
    from story_core.story_io import _load_json

    c = _load_json(_cache_path(story_id, branch_id), {})
    return c if isinstance(c, dict) else {}


def _save_cache(story_id, branch_id, cache) -> None:
    from story_core.story_io import _save_json

    _save_json(_cache_path(story_id, branch_id), cache)


def _cache_key(a, b, kind="npc") -> str:
    return kind + "::" + "||".join(sorted([a, b]))


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------

def _npc_ctx(npc: dict) -> str:
    parts = []
    if npc.get("role"):
        parts.append(str(npc["role"]))
    if npc.get("current_status"):
        parts.append(str(npc["current_status"])[:40])
    return "；".join(parts)


def _pick_canonical(a: dict, b: dict) -> tuple[dict, dict]:
    """Keep the record with more durable memory as canonical."""
    return (a, b) if len(a.get("memory", []) or []) >= len(b.get("memory", []) or []) else (b, a)


def _merge_into(canon: dict, dup: dict) -> None:
    mem = list(canon.get("memory", []) or [])
    for m in dup.get("memory", []) or []:
        if m not in mem:
            mem.append(m)
    if mem:
        canon["memory"] = mem
    # preserve the dup's cold archive too — never destroy ground truth
    arch = list(canon.get("memory_archive", []) or [])
    for m in dup.get("memory_archive", []) or []:
        if m not in arch:
            arch.append(m)
    if arch:
        canon["memory_archive"] = arch
    for key, val in dup.items():
        if key in ("name", "id", "memory", "memory_archive", "aliases"):
            continue
        if not canon.get(key) and val:
            canon[key] = val
    # record the absorbed name so a wrong merge is traceable / recoverable
    aliases = list(canon.get("aliases", []) or [])
    dup_name = str(dup.get("name", "")).strip()
    if dup_name and dup_name != str(canon.get("name", "")).strip() and dup_name not in aliases:
        aliases.append(dup_name)
    if aliases:
        canon["aliases"] = aliases


# ---------------------------------------------------------------------------
# Resolver — pre-filter -> (cache | judge) -> conservative merge. Async-safe.
# ---------------------------------------------------------------------------

def resolve_identities(story_id, branch_id, model=DEFAULT_JUDGE_MODEL, k=3) -> dict:
    from story_core.story_io import _load_json, _save_json, _story_npcs_path

    path = _story_npcs_path(story_id, branch_id)
    npcs = _load_json(path, [])
    if not isinstance(npcs, list) or len(npcs) < 2:
        return {"merged": [], "judged": 0}

    cache = _load_cache(story_id, branch_id)
    by_name = {str(n["name"]).strip(): n for n in npcs if isinstance(n, dict) and str(n.get("name", "")).strip()}
    merged, judged, cache_dirty = [], 0, False

    for a, b in find_candidate_pairs(npcs):
        if a not in by_name or b not in by_name:
            continue  # one already merged away this pass
        key = _cache_key(a, b, "npc")
        if key in cache:
            verdict = cache[key]
        else:
            res = judge_ensemble(a, _npc_ctx(by_name[a]), b, _npc_ctx(by_name[b]), model, k=k)
            if res["n"] == 0:
                continue  # judge unavailable (rate-limited?) — don't cache, retry later
            verdict = "same" if res["decision"] else "different"
            cache[key] = verdict
            cache_dirty = True
            judged += 1
        if verdict == "same":
            canon, dup = _pick_canonical(by_name[a], by_name[b])
            _merge_into(canon, dup)
            npcs = [n for n in npcs if n is not dup]
            by_name.pop(str(dup.get("name", "")).strip(), None)
            by_name[str(canon.get("name", "")).strip()] = canon
            merged.append({"dropped": dup.get("name"), "into": canon.get("name")})

    if merged:
        _save_json(path, npcs)
        try:
            import app

            app.rebuild_state_db_from_json(story_id, branch_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("identity resolve: state.db resync failed (%s)", exc)
    if cache_dirty:
        _save_cache(story_id, branch_id, cache)
    if merged or judged:
        log.info("identity resolve: judged=%d merged=%s", judged, merged)
    return {"merged": merged, "judged": judged}


def resolve_identities_async(story_id, branch_id, model=DEFAULT_JUDGE_MODEL, k=3):
    """Fire-and-forget identity resolution on a daemon thread (off the critical path)."""
    worker = threading.Thread(
        target=resolve_identities, args=(story_id, branch_id),
        kwargs={"model": model, "k": k}, daemon=True,
    )
    worker.start()
    return worker


# ---------------------------------------------------------------------------
# Inventory identity — the SAME Tier-3 machinery applied to inventory item keys.
# "are these two item names the same item?" is a JUDGMENT, so never a string rule.
# ---------------------------------------------------------------------------

_ITEM_PROMPT = (
    "你是道具同一性裁決員。判斷下面兩個道具名稱是否指『同一件道具』。\n"
    "重要原則：名字部分相同、或一個是另一個的子字串，都**不足以**判為同一件。\n"
    "不同階級（如『回血丹』vs『回血丹·高階』）、不同來源（通用 vs 宗門專屬）是**不同道具**。\n"
    "只有你有把握是同一件（同物全名 vs 簡稱、帶括號註解、明顯同物異寫）才判 same。\n"
    "不確定一律判 different——寧可分開，絕不可錯併。\n\n"
    "道具 A：{a}\n"
    "道具 B：{b}\n\n"
    "只輸出嚴格 JSON：{{\"same\": true或false, \"confidence\": 0到1的小數, \"reason\": \"一句話\"}}"
)


def find_candidate_item_pairs(inventory) -> list[tuple[str, str]]:
    keys = [str(k).strip() for k in (inventory or {}).keys() if str(k).strip()]
    pairs = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if _surface_overlap(keys[i], keys[j]):
                pairs.append((keys[i], keys[j]))
    return pairs


def resolve_inventory_identities(story_id, branch_id, model=DEFAULT_JUDGE_MODEL, k=3) -> dict:
    from story_core.story_io import _load_json, _save_json, _story_character_state_path

    path = _story_character_state_path(story_id, branch_id)
    state = _load_json(path, {})
    inv = state.get("inventory")
    if not isinstance(inv, dict) or len(inv) < 2:
        return {"merged": [], "judged": 0}

    cache = _load_cache(story_id, branch_id)
    merged, judged, cache_dirty = [], 0, False
    for a, b in find_candidate_item_pairs(inv):
        if a not in inv or b not in inv:
            continue
        key = _cache_key(a, b, "item")
        if key in cache:
            verdict = cache[key]
        else:
            res = judge_ensemble(a, str(inv.get(a, "")), b, str(inv.get(b, "")), model, k=k, prompt=_ITEM_PROMPT)
            if res["n"] == 0:
                continue
            verdict = "same" if res["decision"] else "different"
            cache[key] = verdict
            cache_dirty = True
            judged += 1
        if verdict == "same":
            canon = a if len(a) >= len(b) else b      # keep the more descriptive name
            drop = b if canon == a else a
            if not inv.get(canon) and inv.get(drop):
                inv[canon] = inv[drop]
            dropped_val = inv.pop(drop, None)
            state.setdefault("inventory_archive", {})[drop] = dropped_val   # never destroy ground truth
            merged.append({"dropped": drop, "into": canon})

    if merged:
        state["inventory"] = inv
        _save_json(path, state)
        try:
            import app

            app.rebuild_state_db_from_json(story_id, branch_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("inventory resolve: state.db resync failed (%s)", exc)
    if cache_dirty:
        _save_cache(story_id, branch_id, cache)
    if merged or judged:
        log.info("inventory resolve: judged=%d merged=%s", judged, merged)
    return {"merged": merged, "judged": judged}


def resolve_inventory_identities_async(story_id, branch_id, model=DEFAULT_JUDGE_MODEL, k=3):
    worker = threading.Thread(
        target=resolve_inventory_identities, args=(story_id, branch_id),
        kwargs={"model": model, "k": k}, daemon=True,
    )
    worker.start()
    return worker


__all__ = [
    "judge_once", "judge_ensemble", "judge_cascade",
    "find_candidate_pairs", "resolve_identities", "resolve_identities_async",
    "find_candidate_item_pairs", "resolve_inventory_identities", "resolve_inventory_identities_async",
]
