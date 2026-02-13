# Testing Plan — Story RPG

## Overview

Testing strategy for the Story RPG project, covering backend unit tests, integration tests, prompt/context structure tests, frontend E2E, and LLM behaviour regression.

**Framework choices:**
- Backend: `pytest` + SQLite `:memory:` + `tmp_path` fixtures
- Frontend: Playwright (TBD — Phase 3)
- CI: GitHub Actions (TBD — Phase 3)

---

## Phase 1: Backend Unit Tests (Pure Logic + Isolated Modules)

### 1.1 Tag Extraction Regex (`tests/test_tag_extraction.py`)

Test all 5 tag extractors from `app.py`:

| Function | Test Cases |
|----------|------------|
| `_extract_state_tag()` | single tag, multiple tags, malformed JSON (silent skip), nested braces, bracket `[STATE...]` format |
| `_extract_lore_tag()` | valid lore JSON, multi-tag, missing fields |
| `_extract_npc_tag()` | NPC with Big5 personality, partial NPC data |
| `_extract_event_tag()` | all 7 event types, status values, multi-event |
| `_extract_img_tag()` | prompt extraction, no tag present, multiple IMG (first only) |

Also test:
- Mixed tags in single response (STATE + LORE + NPC + EVENT + IMG)
- Tag stripping leaves surrounding text intact
- Both `<!-- -->` and `[ ]` delimiter formats

### 1.2 World Timer (`tests/test_world_timer.py`)

| Function | Test Cases |
|----------|------------|
| `process_time_tags()` | `days:N`, `hours:N`, multiple TIME tags, tag stripping |
| `advance_world_day()` | basic advance, hour overflow → day increment, fractional days |
| `get_world_day()` / `set_world_day()` | read/write round-trip, missing file → default `{day:1, hour:0}` |
| `copy_world_day()` | parent → child copy |
| Dungeon helpers | `advance_dungeon_enter()` (+3 days), `advance_dungeon_exit()` (+1 day) |

### 1.3 Lore Search — CJK Bigram Scoring (`tests/test_lore_db.py`)

| Function | Test Cases |
|----------|------------|
| `extract_tags()` | `[tag: X/Y]` extraction |
| CJK bigram generation | 2-char + 3-char tokens from Chinese text |
| `search_lore()` keyword scoring | topic match (+10) > tag match (+5) > content match (+1) |
| `rebuild_index()` | builds from `world_lore.json`, FTS5 sync |
| `upsert_entry()` | insert new, update existing by topic |
| `search_hybrid()` RRF fusion | keyword + embedding merge, K=60 constant |
| Category boosting | 副本中 → boost 副本世界觀; 主神空間 → boost 主神設定 |
| Token budget | results respect 3000-token cap |

Use SQLite `:memory:` + fixture lore entries (5-10 entries covering different categories).

### 1.4 Event Search (`tests/test_event_db.py`)

| Function | Test Cases |
|----------|------------|
| `insert_event()` | all 7 types, required fields |
| `search_events()` | CJK bigram scoring (same pattern as lore) |
| `search_relevant_events()` | `active_only=True` filters out resolved/abandoned |
| `get_event_titles()` | returns set of existing titles (for dedup) |
| `update_event_status()` | planted → triggered → resolved |
| `get_active_foreshadowing()` | only status=planted events |

### 1.5 Gemini Key Manager (`tests/test_gemini_key_manager.py`)

| Function | Test Cases |
|----------|------------|
| `load_keys()` | multi-key format, legacy single-key format |
| `get_available_keys()` | free-first ordering, skip cooled-down keys |
| `mark_rate_limited()` | 60s cooldown, re-available after expiry |
| Edge cases | all keys cooled down → empty list, single key pool |

Mock `time.time()` for deterministic cooldown testing.

### 1.6 Usage DB (`tests/test_usage_db.py`)

| Function | Test Cases |
|----------|------------|
| `log_usage()` | all call types, null tokens (Claude CLI) |
| `get_usage_summary()` | daily aggregation, date range filter |
| `get_total_usage()` | cross-story totals |

---

## Phase 2: Integration Tests + Context Structure

### 2.1 Branch Tree Logic (`tests/test_branch_tree.py`)

Fixture: construct timeline trees of varying complexity (linear, forked, 3+ depth, blank branches, deleted branches).

| Function | Test Cases |
|----------|------------|
| `get_full_timeline()` | linear chain, forked at index N, blank branch (empty), 3-level deep ancestry |
| `_get_fork_points()` | filters blank/deleted/merged/pruned branches |
| `_get_sibling_groups()` | sibling count, current variant index, `< 1/N >` data |
| `_resolve_sibling_parent()` | prevents linear edit chains at same branch point |
| `_auto_prune_siblings()` | prunes abandoned siblings (>=5 steps past, <=2 delta msgs, no children) |
| Blank branch handling | `branch_point_index: -1`, inherits zero messages |

### 2.2 State Update Logic (`tests/test_state_update.py`)

| Function | Test Cases |
|----------|------------|
| `_apply_state_update_inner()` | inventory_add, inventory_remove, reward_points_delta, text field overwrite |
| Schema-driven ops | list fields use `_add`/`_remove` suffix, numeric fields use `_delta` |
| Unknown keys | keys not in schema trigger async normalization (test normalization is called, mock LLM) |
| Edge cases | remove item not in inventory, negative delta, empty update |

### 2.3 Compaction Logic (`tests/test_compaction.py`)

| Function | Test Cases |
|----------|------------|
| `should_compact()` | >20 uncompacted messages → True, <=20 → False |
| `_format_messages()` | 【玩家】/【GM】 formatting, 1000-char truncation |
| `get_context_window()` | returns last 20 messages |
| `load_recap()` / `save_recap()` | round-trip, missing file → None |
| `copy_recap_to_branch()` | parent recap copied to new branch |
| Meta-compaction trigger | recap >8000 chars → should meta-compact |

### 2.4 Context Injection Structure (`tests/test_context_injection.py`)

| Function | Test Cases |
|----------|------------|
| `_build_augmented_message()` | output contains `[相關世界設定]`, `[相關事件追蹤]`, `[NPC 近期動態]`, original user text preserved at end |
| Section ordering | lore → events → NPC activities → dice → `---` → user text |
| Blank branch | event search skipped, default state used |
| System prompt placeholder fill | `_build_story_system_prompt()` has no residual `{...}` placeholders |

Mock: `search_relevant_lore()`, `search_relevant_events()`, `get_recent_activities()`, `roll_fate()`.

### 2.5 Flask Route Integration (`tests/test_api_routes.py`)

| Route | Test Cases |
|-------|------------|
| `POST /api/send` | normal flow: mock LLM → tag extraction → state update → response |
| `POST /api/branches` | create branch at index N, verify timeline_tree updated |
| `POST /api/branches/blank` | blank branch init: `branch_point_index: -1`, default state, empty NPCs |
| `POST /api/branches/edit` | edit user message → new branch created → GM re-generates |
| `POST /api/branches/regenerate` | same branch point, new sibling branch |
| `GET /api/messages` | returns full timeline for branch |
| `DELETE /api/branches/{id}` | soft delete (was_main) vs hard delete |
| `POST /api/branches/promote` | ancestor soft-deleted, branch becomes main |

### 2.6 Tag Async Extraction — Parse Logic (`tests/test_extract_tags_async.py`)

Test the JSON parsing and dedup logic of `_extract_tags_async()`, NOT the LLM output itself.

| Test Cases |
|------------|
| Valid JSON response → lore/events/npcs/state all parsed and saved |
| JSON wrapped in markdown fences → stripped and parsed |
| Malformed JSON → graceful fallback (regex search for `[...]`) |
| Lore dedup: existing topic → upsert, new topic → insert |
| Event dedup: title in `get_event_titles()` → skip |
| NPC dedup: same name → merge, new name → append |
| `skip_state=True` → state extraction skipped |
| `skip_time=True` → time extraction skipped |
| Protected lore (edited_by: user) → not overwritten |

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

## Test Infrastructure

### Directory Structure

```
tests/
  conftest.py              # Shared fixtures: tmp story dir, sample data, app client
  test_tag_extraction.py   # Phase 1.1
  test_world_timer.py      # Phase 1.2
  test_lore_db.py          # Phase 1.3
  test_event_db.py         # Phase 1.4
  test_gemini_key_manager.py  # Phase 1.5
  test_usage_db.py         # Phase 1.6
  test_branch_tree.py      # Phase 2.1
  test_state_update.py     # Phase 2.2
  test_compaction.py       # Phase 2.3
  test_context_injection.py   # Phase 2.4
  test_api_routes.py       # Phase 2.5
  test_extract_tags_async.py  # Phase 2.6
  fixtures/
    sample_system_prompt.txt
    sample_lore.json        # 10-15 entries for search tests
    sample_timeline_tree.json  # Multi-branch tree fixture
    sample_messages.json
    sample_character_schema.json
    sample_character_state.json
    sample_npcs.json
```

### Shared Fixtures (`conftest.py`)

```python
@pytest.fixture
def story_dir(tmp_path):
    """Creates a minimal story directory with all required files"""

@pytest.fixture
def lore_db_memory():
    """In-memory SQLite lore database with sample entries"""

@pytest.fixture
def event_db_memory():
    """In-memory SQLite event database"""

@pytest.fixture
def flask_client(story_dir):
    """Flask test client with mocked LLM and tmp data dir"""

@pytest.fixture
def sample_timeline_tree():
    """Multi-branch timeline tree for branch logic tests"""
```

### pytest Configuration

```ini
# pytest.ini
[pytest]
testpaths = tests
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    golden: golden set regression tests
    integration: integration tests requiring Flask client
```

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
