"""Compact on-disk encoding for card-mode branch messages.json.

Every card turn snapshots the companion's whole memory list twice
(pre_npcs_snapshot / npcs_snapshot), so a plain JSON list grows with
turns x memories: a ~1500-turn story reached 181 MB, almost all repeated
memory strings. The encoded file stores each distinct memory string once in
a pool; each snapshot's memory list becomes [start, length] runs into it.

decode(encode(messages)) == messages, and decoded lists are fresh objects,
so callers may mutate them freely. Loading is shape-driven (FORMAT marker),
so a plain list keeps working and either form can be read anywhere.
"""

FORMAT = "story.card_messages.pooled"
VERSION = 1
SNAPSHOT_FIELDS = ("pre_npcs_snapshot", "npcs_snapshot")
_POOL_KEY = "$pool"


def is_encoded(doc) -> bool:
    return isinstance(doc, dict) and doc.get("format") == FORMAT


def _poolable(npc) -> bool:
    memory = npc.get("memory") if isinstance(npc, dict) else None
    return isinstance(memory, list) and bool(memory) and all(isinstance(m, str) for m in memory)


def has_poolable_memory(messages) -> bool:
    return isinstance(messages, list) and any(
        isinstance(m, dict) and isinstance(m.get(field), list) and any(_poolable(n) for n in m[field])
        for m in messages for field in SNAPSHOT_FIELDS)


def _runs(ids):
    runs = []
    for i in ids:
        if runs and runs[-1][0] + runs[-1][1] == i:
            runs[-1][1] += 1
        else:
            runs.append([i, 1])
    return runs


def encode(messages: list) -> dict:
    """Return the pooled document; the input is never mutated."""
    pool, ids = [], {}

    def intern(text):
        i = ids.get(text)
        if i is None:
            i = ids[text] = len(pool)
            pool.append(text)
        return i

    out = []
    for message in messages:
        if not isinstance(message, dict) or not any(isinstance(message.get(f), list) for f in SNAPSHOT_FIELDS):
            out.append(message)
            continue
        message = dict(message)
        for field in SNAPSHOT_FIELDS:
            snapshot = message.get(field)
            if not isinstance(snapshot, list):
                continue
            message[field] = [
                {**npc, "memory": {_POOL_KEY: _runs([intern(t) for t in npc["memory"]])}}
                if _poolable(npc) else npc
                for npc in snapshot]
        out.append(message)
    return {"format": FORMAT, "version": VERSION, "memory_pool": pool, "messages": out}


def decode(doc):
    """Return the plain message list; non-encoded input is returned unchanged."""
    if not is_encoded(doc):
        return doc
    if doc.get("version") != VERSION:
        raise ValueError(f"unsupported messages format version: {doc.get('version')}")
    pool = doc["memory_pool"]
    messages = doc["messages"]
    for message in messages:
        if not isinstance(message, dict):
            continue
        for field in SNAPSHOT_FIELDS:
            snapshot = message.get(field)
            if not isinstance(snapshot, list):
                continue
            for npc in snapshot:
                memory = npc.get("memory") if isinstance(npc, dict) else None
                if isinstance(memory, dict) and _POOL_KEY in memory:
                    npc["memory"] = [pool[i] for start, length in memory[_POOL_KEY]
                                     for i in range(start, start + length)]
    return messages
