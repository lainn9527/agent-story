"""Read snapshot-owned memories without discarding older relevant facts."""
import math
import re
import random
from collections import Counter


def _terms(text):
    words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.lower())
    return {term for word in words for term in
            ([word] if not re.search(r"[\u3400-\u9fff]", word)
             else [word[i:i + 2] for i in range(len(word) - 1)])}


def memories_before_context(recent, npc_name):
    """Read the context boundary's branch-owned snapshot; None preserves legacy selection."""
    if not recent:
        return None
    first = recent[0]
    key = "pre_npcs_snapshot" if first.get("role") == "gm" else "npcs_snapshot"
    snapshot = first.get(key)
    if not isinstance(snapshot, list):
        return None
    matches = [npc for npc in snapshot if isinstance(npc, dict) and npc.get("name") == npc_name]
    if len(matches) != 1 or not isinstance(matches[0].get("memory"), list):
        return None
    return matches[0]["memory"]


def memory_kwargs(card):
    """Card-level select_memories quotas: memory_counts = [recent, relevant, random]."""
    counts = (card or {}).get("memory_counts")
    if isinstance(counts, (list, tuple)) and len(counts) == 3:
        return dict(zip(("recent_count", "relevant_count", "random_count"), counts))
    return {}


def select_memories(memories, query="", recent_count=36, relevant_count=36,
                    random_count=36, *, recent_memory_pool=None):
    """Recent, relevant and randomly recalled facts, ordered by recording order.

    Current agreements live in recap; retrieved memories are historical evidence,
    not an assertion that an old arrangement is still in force.
    """
    # Keep the latest occurrence of exact duplicates without changing the archive.
    candidates = [m for m in memories if isinstance(m, str) and m.strip()]
    facts = list(reversed(dict.fromkeys(reversed(candidates))))
    if len(facts) <= recent_count and recent_memory_pool is None:
        return facts
    terms = [_terms(m) for m in facts]
    frequencies = Counter(t for row in terms for t in row)
    query_terms = _terms(query)
    recent_start = max(0, len(facts) - recent_count)
    scores = [(sum(math.log(1 + len(facts) / frequencies[t])
                   for t in row & query_terms), i)
              for i, row in enumerate(terms[:recent_start])]
    selected = {i for score, i in sorted(scores, reverse=True)[:relevant_count] if score > 0}
    # Keep relevance/random pools unchanged; only move the guaranteed recent quota.
    remaining = [i for i in range(recent_start) if i not in selected]
    selected.update(random.sample(remaining, min(random_count, len(remaining))))
    if recent_memory_pool is None:
        selected.update(range(recent_start, len(facts)))
    else:
        prior = {m for m in recent_memory_pool if isinstance(m, str)}
        eligible = [i for i, fact in enumerate(facts) if fact in prior]
        selected.update(eligible[-recent_count:] if recent_count else [])
    return [facts[i] for i in sorted(selected)]
