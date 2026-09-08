# v2 狀態生命週期與除錯地圖（fork / snapshot / 副本 / recap）

> **這份文件解決什麼**：v2 引擎的 runtime 狀態怎麼流動、存哪、fork/regenerate 時怎麼搬，以及**一再復發的同一類 bug（確定性變更在 fork 後被回捲）**和它的修法。
> 目的是讓人（或 agent）**直接跳到對的檔案/函式**，不要每次都重新 grep 半天考古。
>
> **分工**：設計與「為什麼」看 [`architecture_v2.md`](architecture_v2.md)（北極星）＋[`design_decisions_v2.md`](design_decisions_v2.md)（邊界決策）；請求管線看 [`flow.md`](flow.md)。**這份只講 runtime 狀態與除錯。**
>
> 適用：`gm_engine=v2`、`transport="tools"`（兩段式）。v1 已凍結（見記憶 `v1-v2-split`）。

---

## 0. TL;DR（最容易踩的三件事）

1. **確定性變更（買東西／進出副本／修煉／任務）寫的是 `character_state.json`，但 fork/regenerate 還原的是「每則 GM 訊息裡的 `state_snapshot`」。** 兩者不同步 → fork 會把你的變更回捲。**修法：變更後呼叫 `_sync_latest_gm_snapshot`**（已在 5 個 deterministic 路由接好）。
2. **引擎獨佔欄位（`current_dungeon` / `completed_dungeons` / `current_phase` / `technique_mastery` / `technique_days`）GM 寫不進去**（gate 擋掉）。它們只能由引擎工具改。看到它們的值「對不上敘事」，先想是不是**該觸發的引擎工具沒觸發**（不是 GM 寫錯）。
3. **手改狀態不要直接編輯 json**（會被回捲）。用 §9 的配方：`apply_state_delta` → `_sync_latest_gm_snapshot` → 驗快照。

---

## 1. 每條 branch 的檔案地圖

路徑：`data/stories/<story_id>/branches/branch_<id>/`

| 檔案 | 內容 | 由誰寫 / 備註 |
|---|---|---|
| `character_state.json` | 角色狀態（境界/級、inventory、techniques、fate_points、quests、current_dungeon/phase、completed_dungeons…） | **真相來源（live）**。引擎透過 `apply_state_delta` 改 |
| `messages.json` | 該 branch **自己的** 訊息增量（delta）。每則 GM 訊息夾帶 `state_snapshot` / `npcs_snapshot` / `world_day_snapshot` / `dungeon_progress_snapshot` | fork/regen 的還原來源（**不是** live json） |
| `conversation_recap.json` | 壓縮摘要：`recap_text` + `compacted_through_index` + `total_turns_compacted` | compaction。`compacted_through_index=-1`＝/play 手寫 seed，永不自動壓 |
| `scenario.json` | **當前副本的 blueprint**（objectives/beats/rewards/`clear_reward`/climax） | `enter_dungeon` 時 `scenario_gen` 非同步生成。**通關獎勵靠它發** |
| `dungeon_progress.json` | v1 結構化副本追蹤（history/total/current_dungeon dict） | v2 多半**留存但不主導**（completed_dungeons 才是 v2 正典）。空的 `{history:[],total:0}` 在 v2 屬正常 |
| `dungeon_return_memory.json` | 可重入副本記憶（`visited_dungeons` / `pending_reentry_dungeon`） | 離場時記，供「重返副本」 |
| `world_day.json` | 世界日（時間經濟） | time engine |
| `npcs.json` / `npc_identity_cache.json` | NPC 全量（含 archived）／ 名稱解析快取 | |
| `gm_cheats.json` | `dice_always_success` / `pistol_mode` / `fate_mode` | debug seed（注意：v2 `pistol_mode` 多為 dead toggle，限制級在 base prompt 寫死） |
| `config.json` | `gm_engine` / `gm_transport` / image 設定 | 決定走 v1 還是 v2、delta/tools |
| `op_undo.json` / `state.db` | 動作 undo 堆疊 / SQLite 鏡像（state sync） | |

分支樹：`data/stories/<story_id>/timeline_tree.json`（節點：`id` / `parent_branch_id` / `branch_point_index` / `name` / `character_state_file`）。

---

## 2. 確定性 vs 生成 的執行邊界 ＋ 引擎獨佔欄位（the gate）

- **引擎是真相，LLM 只旁白/評判**（北極星見 `architecture_v2.md`）。
- **引擎獨佔欄位**（`story_core/gm_v2/gate.py` `_ENGINE_OWNED` / `_ENGINE_PHASES`）：GM 不管走 delta 還是 `set_state` 工具**一律被 gate 丟棄**——

  | 欄位 | 只能由誰改 |
  |---|---|
  | `current_dungeon` | `enter_dungeon` / `exit_dungeon` |
  | `completed_dungeons` | `exit_dungeon`（通關才 append） |
  | `current_phase` ∈ {`主神空間`,`副本中`,`副本結算`} | enter/exit_dungeon（`死亡`/`傳送中` GM 仍可敘事驅動） |
  | `technique_mastery`（「成」） | cultivate / 解鎖 |
  | `technique_days` | time engine |

- **引擎路徑繞過 gate**：`apply_state_delta`（`gm_v2/apply.py`）直接套用、無 lossy guard——所以**手術式修狀態要用它**（§9）。
- 推論：**這些欄位「對不上敘事」≠ GM 寫錯，而是該跑的引擎工具沒跑**（最常見：敘事進副本但 `enter_dungeon` 沒被 PASS2 吐出來）。

---

## 3. 兩段式 tools transport（PASS1 旁白 / PASS2 記錄）

- **PASS1**：flash-3.5 真串流旁白（玩家看到的）。
- **PASS2**：flash-lite 背景**記錄**——把這回合該做的**引擎工具呼叫**吐出來（`enter_dungeon` / `exit_dungeon` / `complete_quest` / catalog buy / set_state…），由 `gm_v2/toolexec.py` 執行。
- **玩家動作（按鈕）是另一條確定性入口**，不經 PASS2：`scripts/play/action_tools.py`（🌀 進入試煉秘境=`enter_dungeon`、🚪 離開副本/回歸=`exit_dungeon`、完成任務…），前端 `static/actions.js`。
- ⚠️ **可靠度坑**：敘事進/出副本靠 PASS2「自覺」吐工具，**會漏**（漏 `enter_dungeon` → 整個副本引擎不知情）。按鈕是 deterministic、最穩。

---

## 4. 副本生命週期（端到端）

```
進入  enter_dungeon(ip)            apply.py:enter_dungeon
        ├ set current_dungeon=ip, current_phase=副本中   （引擎獨佔，繞 gate）
        ├ scenario_gen.generate_blueprint_async → 寫 scenario.json   （含 clear_reward）
        └ time engine 扣 dungeon_time（預設 7 天）
進行中  in-dungeon grounding ← render_blueprint_brief(scenario.json)   世界觀隔離（present_world）
離開  exit_dungeon(outcome)        apply.py:exit_dungeon
        ├ _grant_dungeon_rewards   apply.py:353  讀 scenario.json：
        │     fate += clear_reward(=target_level×400)；通關-gated 道具 → inventory/techniques
        ├ set current_dungeon="", current_phase=主神空間/死亡
        ├ completed_dungeons_add=[dungeon]   （cleared 才記）
        └ _archive_dungeon_npcs  副本原生 NPC offstage、隊友帶回
```

- **兩套追蹤別搞混**：`character_state.completed_dungeons`（v2 正典）vs `dungeon_progress.json`（v1 結構化，v2 多半空）。
- **獎勵閘門**：`clear_reward`（fate，定額）＋ `rewards[].gated_by` 含「通關」的道具才在 exit 自動發；`gated_by:支線sN` 要支線達成才給（`_grant_dungeon_rewards` 只發含「通關」者）。
- **blueprint 是 per-branch 檔**：見 §5「fork 搬什麼」——它不跟 fork 走過（曾經的 bug），會害下游 exit 讀不到 → 發 0。

劇本架構師：`/scenario` skill ＋ `gm_v2/scenario_gen.py`（generate→validate→save，重試一次）。

---

## 5. snapshot / fork / regenerate

- **每則 GM 訊息**在 `messages.json` 夾帶 `state_snapshot`（＋ npcs/world_day/dungeon_progress 快照）。寫入：`gm_pipeline.py` 的 `_sync_gm_message_snapshot_after_async`；最新一則一鍵重捕：**`_sync_latest_gm_snapshot(story_id, branch_id)`**（`gm_pipeline.py`，app 經 `__all__` 再匯出）。
- **fork/regenerate 還原狀態**：`_find_state_at_index(story_id, branch_id, target_index)`（`gm_pipeline.py`）讀目標訊息的 `state_snapshot`，**不是 live json**。`routes/branch_routes.py` 用它。
- **fork 搬哪些檔**（`routes/branch_routes.py`，多個 fork/regen/edit 站）：`copy_recap_to_branch` / `copy_cheats` / `_copy_branch_lore_for_fork` / `copy_events_for_fork` / `_copy_gm_plan` / **`copy_dungeon_progress`（現在也搬 `scenario.json`）** / `copy_dungeon_return_memory` / `copy_world_day` / char_state＋npcs（`shutil.copy2`）。
  - 💡 **要讓某 per-branch 檔在 fork 後存活**：把它掛進對應 copy 函式（blueprint 就是掛在 `dungeon_system.py:copy_dungeon_progress` 裡，一處涵蓋全部 fork 站）。

---

## 6. recap / compaction

- 檔：`conversation_recap.json`。邏輯：`story_core/compaction.py`。
- `should_compact(recap, timeline_len)`：未壓縮量 `(timeline_len-RECENT_WINDOW)-(compacted_through_index+1) > MIN_UNCOMPACTED(10)` 才壓。`_run_compaction` 設 `compacted_through_index = len-RECENT_WINDOW-1`。
- ⚠️ **v2 曾經從不觸發壓縮**（compaction 只掛在 v1 路徑）→ recap 凍結、舊任務 GM 演不出。修法：**`gm_v2/pipeline.py:_maybe_compact_recap`** 掛在 v2 三個出口（tools / tools-inline / delta）。
- `compacted_through_index=-1` ＝ /play 手寫 seed，永不自動壓。

---

## 7. 賦級 / leveljudge（固定世界人物戰力）

- `gm_v2/leveljudge.py`：`rank_map`（`catalog.json`）把**宗門職稱→境界**（長老→元嬰、掌門/宗主→化神…），`_rank_anchored_level` 直接定級，不隨主角縮放。
- **相對主角的 judge 只在「對手境界不明／剛進新副本還沒建錨」時用**。錨點文件：`story_design/_shared/xianxia_anchor.txt`（§六 宗門位階↔境界）。
- 級＝大境界×10＋層；大境界＝級÷10。

---

## 8. 🐛 反覆出現的 bug 家族：「確定性變更在 fork/regenerate 後被回捲」

**元樣態**：某確定性引擎變更寫了 `character_state.json`，但**對應的 snapshot 沒重捕** 或 **per-branch 檔沒被 fork 複製** → 之後 fork/regen 從舊 snapshot 還原，變更消失。

| 實例 | 根因 | 修法 |
|---|---|---|
| 買功法沒扣／被退、買了又不見 | 買賣改 live json，沒同步 snapshot | `_sync_latest_gm_snapshot` 接到 5 個 deterministic 路由（buy/refund/action/unlock/cultivate，`routes/gm_v2_routes.py`） |
| 整個副本沒登記＋通關獎勵沒發 | fork 到「傳送前」再敘事重演進入，`enter_dungeon` 沒觸發；且 blueprint 不跟 fork | (a) `copy_dungeon_progress` 一併搬 `scenario.json`；(b) PASS2 進入端對稱觸發（規劃中：強化 contract + backstop/UX 確認） |
| recap 凍結、舊任務演不出 | v2 不觸發 compaction | `_maybe_compact_recap` 掛三出口 |
| 賦級把長老縮成 15 級、且事後卡死 | ① judge 相對主角縮放 ② `leveljudge._needs_level` 對已賦級者 idempotent skip → rank_map 修好後舊值仍永不重判、fork 一路繼承 | `rank_map` 職稱→戰力（relative 只用於不明/新副本）；**已賦級但職稱被低估→重錨**（純頭銜才補；rank_label 帶任何境界詞＝敘事優先，不硬拉）。批次掃 `_needs_level`+`_rank_anchored_level` |

**修任一新實例的三招**：① 變更後重捕 snapshot；② 該檔掛進 fork copy 函式；③ 讓 PASS2/動作可靠地吐出引擎工具。

---

## 9. 🔧 安全手改狀態的配方（surgical fix）

**不要直接編輯 json**（會被回捲）。走引擎路徑＋重捕快照：

```python
import sys; sys.path.insert(0, '.')
import app
from story_core.gm_v2 import apply
sid, bid = 'demon_tide', 'branch_<id>'

# 1) 先備份 character_state.json + messages.json 到 /tmp

# 2) 引擎路徑套用變更（繞 gate、含 state.db sync、不碰未動欄位）
apply.apply_state_delta(sid, bid, {
    'fate_points_delta': 1200,                      # 數值用 *_delta（引擎算 current+delta，免 double-count）
    'inventory': {'某道具': '效果說明'},             # map 直接 merge
    'completed_dungeons_add': ['某副本'],            # list 用 *_add
})

# 3) 重捕最新 GM 訊息的 snapshot —— 這步沒做，下次 regen 就回捲
ok = app._sync_latest_gm_snapshot(sid, bid)

# 4) 驗：messages.json 最後一則 GM 的 state_snapshot 要有新值
```

- 任務結算用 `apply.complete_quest(sid, bid, quest_id)`（冪等、發已宣告獎勵）。
- 副本補償（通關獎勵沒發）：照上面補 `fate_points_delta`＝blueprint `clear_reward`、通關道具進 inventory、`completed_dungeons_add`。
- ⚠️ Python 改完要**重啟** dev server：`bash scripts/dev/run_dev_server.sh restart`（detached :5056，`NO_RELOAD=1`）。Prompt/data 改是 live、免重啟。

---

## 10. 「我想找 X → 看 Y」速查

| 想找 | 檔案 / 函式 |
|---|---|
| 進/出副本、結算獎勵 | `gm_v2/apply.py`：`enter_dungeon` / `exit_dungeon` / `_grant_dungeon_rewards` |
| blueprint 生成/載入/渲染 | `gm_v2/scenario_gen.py`：`generate_blueprint(_async)` / `load_blueprint` / `render_blueprint_brief` |
| 引擎獨佔欄位、gate 規則 | `gm_v2/gate.py`：`_ENGINE_OWNED` / `_ENGINE_PHASES` |
| 工具定義/執行（PASS2） | `gm_v2/toolexec.py`、契約 `gm_v2/contract.py` |
| 動作按鈕（玩家確定性入口） | `scripts/play/action_tools.py`、前端 `static/actions.js` |
| snapshot 重捕 / fork 還原 | `gm_pipeline.py`：`_sync_latest_gm_snapshot` / `_sync_gm_message_snapshot_after_async` / `_find_state_at_index` |
| fork 搬哪些檔 | `routes/branch_routes.py`（copy_* 呼叫群）；blueprint 在 `dungeon_system.py:copy_dungeon_progress` |
| recap 壓縮 | `compaction.py`；v2 觸發 `gm_v2/pipeline.py:_maybe_compact_recap` |
| 賦級 | `gm_v2/leveljudge.py`、`catalog.json:rank_map`、`story_design/_shared/xianxia_anchor.txt` |
| 旁白切 delta/legacy tag | `gm_v2/parse.py` |
| 商店目錄/買賣 | `catalog.json`、`scripts/play/catalog_tools.py`、`static/shop.js` |

---

*維護提醒：這份是「runtime 狀態與除錯」地圖，刻意只放會反覆查的東西。設計層的「為什麼」請留在 `architecture_v2.md` / `design_decisions_v2.md`，別在這裡長出第二份設計文件（避免 drift）。*
