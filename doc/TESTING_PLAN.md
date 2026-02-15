# Testing Plan — Story RPG

## Overview

Testing strategy for the Story RPG project, covering backend unit tests, integration tests, prompt/context structure tests, frontend E2E, and LLM behaviour regression.

**Framework choices:**
- Backend: `pytest` + SQLite `:memory:` + `tmp_path` fixtures
- Frontend: Playwright (TBD — Phase 3)
- CI: GitHub Actions (TBD — Phase 3)

**Current status:** Phase 1 + 2 complete (273 tests). Phase 3 + 4 TBD.

---

## Phase 1: Backend Unit Tests ✅

Pure logic and isolated modules — no Flask app context needed.

### 1.1 Tag Extraction Regex (`test_tag_extraction.py`)

| Function | Test Cases |
|----------|------------|
| `_extract_state_tag()` | single tag, multiple tags, malformed JSON (silent skip), nested braces, bracket `[STATE...]` format |
| `_extract_lore_tag()` | valid lore JSON, multi-tag, missing fields |
| `_extract_npc_tag()` | NPC with Big5 personality, partial NPC data |
| `_extract_event_tag()` | all 7 event types, status values, multi-event |
| `_extract_img_tag()` | prompt extraction, no tag present, multiple IMG (first only) |
| Mixed tags | STATE + LORE + NPC + EVENT + IMG in single response |
| Tag stripping | surrounding text intact after extraction |

### 1.2 World Timer (`test_world_timer.py`)

| Function | Test Cases |
|----------|------------|
| `process_time_tags()` | `days:N`, `hours:N`, multiple TIME tags, tag stripping |
| `advance_world_day()` | basic advance, negative delta rejected |
| `get_world_day()` / `set_world_day()` | read/write round-trip, missing file → default |
| `copy_world_day()` | parent → child copy, zero value skips write |
| Dungeon helpers | `advance_dungeon_enter()` (+3 days), `advance_dungeon_exit()` (+1 day) |

### 1.3 Lore Search (`test_lore_db.py`)

| Function | Test Cases |
|----------|------------|
| `extract_tags()` | `[tag: X/Y]` extraction |
| CJK bigram generation | 2-char + 3-char tokens from Chinese text |
| `search_lore()` keyword scoring | topic match (+10) > tag match (+5) > content match (+1) |
| `rebuild_index()` | builds from `world_lore.json` |
| `search_hybrid()` RRF fusion | keyword + embedding merge |
| Category boosting | phase-based category prioritization |
| Token budget | results respect 3000-token cap |

### 1.4 Event Search (`test_event_db.py`)

| Function | Test Cases |
|----------|------------|
| `insert_event()` | all 7 types, required fields |
| `search_events()` | CJK bigram scoring, branch filtering |
| `search_relevant_events()` | `active_only=True` filters resolved/abandoned |
| `get_event_titles()` | returns set for dedup |
| `update_event_status()` | planted → triggered → resolved |
| `get_active_foreshadowing()` | only planted events |

### 1.5 Gemini Key Manager (`test_gemini_key_manager.py`)

| Function | Test Cases |
|----------|------------|
| `load_keys()` | multi-key format, legacy single-key format |
| `get_available_keys()` | free-first ordering, skip cooled-down keys |
| `mark_rate_limited()` | 60s cooldown, re-available after expiry |
| Edge cases | all keys cooled down → empty list, single key pool |

### 1.6 Usage DB (`test_usage_db.py`)

| Function | Test Cases |
|----------|------------|
| `log_usage()` | all call types, null tokens (Claude CLI) |
| `get_usage_summary()` | daily aggregation, date range filter |
| `log_from_bridge()` | None usage with mocked `get_last_usage` → early return |

---

## Phase 2: Integration Tests ✅

Tests requiring multiple modules working together, Flask test client, or mocked LLM calls.

### 2.1 Branch Tree Logic (`test_branch_tree.py`)

| Function | Test Cases |
|----------|------------|
| `get_full_timeline()` | linear chain, forked at index N, blank branch (empty), 3-level deep |
| `_get_fork_points()` | filters blank/deleted/merged/pruned branches |
| `_get_sibling_groups()` | sibling count, current variant index |
| `_resolve_sibling_parent()` | prevents linear edit chains at same branch point |
| Blank branch | `branch_point_index: -1`, inherits zero messages |
| **Circular parent refs** | cycle detection terminates without infinite loop |
| **Missing parent** | deleted parent in chain does not crash with KeyError |

### 2.2 State Update Logic (`test_state_update.py`)

| Function | Test Cases |
|----------|------------|
| `_apply_state_update_inner()` | inventory_add/remove, reward_points_delta, text overwrite |
| Schema-driven ops | list `_add`/`_remove`, numeric `_delta` |
| LLM quirks | string-instead-of-list coercion, name-prefix matching for remove |
| Edge cases | remove item not in inventory, negative delta, combined multi-field |
| Relationships | relationship map merge semantics |

### 2.3 Compaction Logic (`test_compaction.py`)

| Function | Test Cases |
|----------|------------|
| `should_compact()` | >20 uncompacted → True, <=20 → False |
| `_format_messages()` | 【玩家】/【GM】 formatting, 1000-char truncation |
| `get_context_window()` | returns last 20 messages |
| `load_recap()` / `save_recap()` | round-trip, missing file → None |
| `copy_recap_to_branch()` | parent recap copied to new branch |
| Meta-compaction | recap >8000 chars → re-summarize |

### 2.4 Context Injection (`test_context_injection.py`)

| Function | Test Cases |
|----------|------------|
| `_build_augmented_message()` | contains `[相關世界設定]`, `[相關事件追蹤]`, `[NPC 近期動態]` |
| Section ordering | lore → events → NPC activities → dice → user text |
| System prompt | `_build_story_system_prompt()` has no residual `{...}` placeholders |

### 2.5 Flask Route Integration (`test_api_routes.py`)

| Route | Test Cases |
|-------|------------|
| `GET/POST /api/stories` | CRUD, switch story |
| `POST /api/branches` | create at index N, verify tree updated |
| `POST /api/branches/blank` | `branch_point_index: -1`, default state |
| `GET /api/messages` | full timeline, `after_index` incremental polling |
| `GET /api/events` | event list for branch |
| `GET /api/lore` | lore search, lore list |
| `GET /api/npcs` | NPC list for branch |
| `GET /api/config` | sanitized config (no API keys exposed) |
| Cheats | dice toggle, pistol mode |

### 2.6 Async Tag Extraction (`test_extract_tags_async.py`)

| Test Cases |
|------------|
| Valid JSON → lore/events/npcs/state parsed and saved |
| Markdown fenced JSON → stripped and parsed |
| Malformed JSON → regex fallback |
| Short text (<200 chars) → skipped entirely |
| **Event dedup: same title, same status → no duplicate** |
| **Event dedup: same title, advanced status → status updated (planted→triggered)** |
| **Event dedup: backward status transition blocked (resolved→planted)** |
| Lore: user-edited entries (`edited_by: user`) not overwritten |
| `skip_state=True` → state extraction skipped |
| Time advancement via async extraction |
| Branch title set-once semantics |

---

## Phase 3: Frontend E2E (TBD)

**Framework:** Playwright

**Approach:** Mock all API calls via `page.route()`, test real DOM rendering.

### Planned Test Flows

| Flow | Description |
|------|-------------|
| Send message | Type → send → GM response appears |
| Edit message | Click ✎ → edit text → submit → new branch created |
| Regenerate | Click ↻ → new GM response → sibling switcher appears |
| Branch switching | Drawer → click branch → messages update |
| Blank branch creation | ⊕ button → new branch → character creation prompt auto-sent |
| Drawer operations | Open/close, story list, character panel, NPC cards, events |
| Sibling navigation | `< 1/N >` switcher → messages change |
| Live auto-play view | AUTO badge, LIVE indicator, polling, input disabled |
| World day display | `✦ 世界第 N 天·時段` updates correctly |
| Mobile viewport | Novel reader layout, drawer behavior |

### Setup Needed

- [ ] `package.json` with Playwright dependency
- [ ] `playwright.config.ts` with test server setup
- [ ] API mock fixtures (canned responses for all endpoints)
- [ ] Visual regression baseline screenshots

---

## Phase 4: LLM Behaviour Regression (TBD)

### 4.1 Search Relevance Golden Set

Fixed lore database (10-15 entries) + fixed queries → expected top-3 results.

```python
@pytest.mark.golden
def test_lore_search_golden_set():
    """Verify search returns expected entries for known queries"""
    setup_golden_lore_db()  # 15 固定 entries
    golden_cases = [
        ("基因鎖怎麼開", ["基因鎖", "體質強化"]),
        ("咒怨副本", ["咒怨", "任務規則"]),
        ("修煉功法", ["修真", "鬥氣"]),
    ]
    for query, expected_topics in golden_cases:
        results = search_lore("test_story", query, limit=3)
        topics = [r["topic"] for r in results]
        for expected in expected_topics:
            assert expected in topics, f"Query '{query}' missing '{expected}'"
```

### 4.2 Prompt Regression (TBD)

- Snapshot system prompt template + verify structure on changes
- LLM-as-judge for GM response quality (expensive, manual trigger only)
- Track prompt version in code for change detection

### 4.3 Visual Regression (TBD)

- Playwright screenshot comparison
- Percy or Chromatic integration
- Baseline per component (message, drawer, NPC card, event item)

---

## Phase 2.5: Coverage Gaps (Known, Not Yet Implemented)

Bugs and gaps discovered during Phase 1+2 test writing. Listed here for future prioritization.

### High Priority
- **`_process_gm_response` integration test** — the unified tag pipeline that every GM response flows through. Currently only individual extractors are tested in isolation.
- **Gameplay routes** (`/api/send`, `/api/edit`, `/api/regenerate`) — require mocking the full LLM streaming pipeline. Most exercised production paths.
- **`/api/init` route** — application entry point called on every page load. Zero coverage.

### Medium Priority
- **`/api/messages` response shape** — missing assertions for `fork_points`, `sibling_groups`, `world_day`, `original_count`, `branch_id` fields that frontend destructures
- **Reward points floor** — `reward_points_delta` can result in negative points (no `max(0, ...)` clamp)
- **Death state transition** — `current_phase: "死亡"`, `current_status: "end"` path untested
- **Auto-play pure functions** — `analyze_response`, `update_phase`, `should_stop` in `auto_play.py`
- **Save/load system** — 5 endpoints (list, create, load, delete, rename)
- **Branch promote/merge** — complex state transitions

### Low Priority
- **`set_world_day` negative value** — accepts negative day without validation
- **Time advance upper bound** — `process_time_tags` has no cap (vs async extraction's 30-day cap)
- **Thread-local usage leakage** — `get_last_usage()` could return stale data from previous call on same thread
- **Cross-category lore near-duplicates** — similar topics in different categories create separate entries

---

## CI Pipeline (TBD — Phase 3)

```yaml
# .github/workflows/test.yml (TBD)
name: Tests
on: [push, pull_request]
jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install pytest
      - run: pytest -m "not slow and not golden" --tb=short
```
