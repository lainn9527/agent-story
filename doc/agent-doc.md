# Agent Engineering Notes

這份文件不是把這個 project 當成功能集合，而是把它當成一組 agent 設計假設的驗證紀錄。

核心觀點：

- 這個專案的主體不是「讓 LLM 自由發揮」，而是「把 LLM 放進一個有 memory、validation、trace、fallback 的 runtime」。
- 真正要講的不是做了多少 feature，而是遇到哪些 failure mode，後來怎麼把它們變成可約束、可回放、可量測的 harness。

---

## 1. 系統定位

這個專案的主流程是：

1. 玩家送出一個 turn。
2. 系統從 branch runtime、state、lore、events、NPC、recap 組出一個受控上下文。
3. LLM 產生 GM 回覆。
4. 回覆不會直接當真，而是再經過同步 parser、非同步 extractor、validation gate、repair path、snapshot 與背景整理。
5. 最後才把可接受的結果寫回 branch memory。

所以這是一個 **agent runtime + memory runtime + commit pipeline**，不是單純 prompt engineering。

---

## 2. Tool Calling Runtime

### 2.1 現在的 tool surface 其實是三層

| 層級 | 形式 | 目前用途 | 安全邊界 |
| --- | --- | --- | --- |
| Provider-level tools | Gemini `googleSearch` | Lore Console grounding | 只在特定路由開放，結果不直接改 runtime |
| Agent workspace tools | Codex read-only workspace | `codex_agent` provider 讀 branch/context 檔案 | 只讀、ephemeral、branch-scoped |
| App-level pseudo tools | `STATE/LORE/NPC/EVENT/IMG/TIME` tags、`event_ops/state_ops` | GM 回覆後的結構化 side effects | 所有 side effect 都先經 parser / gate / fallback |

這個設計的重點是：**把 side effect 從模型本體剝離，變成一個可驗證的提交流程。**

GM 主流程本身並不是讓模型直接呼叫任意工具，而是讓模型輸出「受限的結構化意圖」，再由 runtime 決定是否提交。

### 2.2 Provider runtime 的設計取向

| Provider | 角色 | 特點 | 取捨 |
| --- | --- | --- | --- |
| `gemini` | 一般 GM / lore chat / grounding | 支援 API tool、key fallback、network retry、usage metadata | 最完整的 API control surface |
| `claude_cli` | 一般 GM provider | CLI 包裝、無正式 tool runtime、回傳失敗時走錯誤字串 | 可快速切換，但可觀測性較弱 |
| `codex_agent` | read-only GM provider | 為每回合建立臨時 workspace，模型先讀檔再輸出最終回覆 | 安全、context 完整，但不是 open-ended tool use |

`codex_agent` 的重要設計不是「讓 agent 更自由」，而是「讓 agent 能讀更多 context，但仍不能改 runtime data」。

### 2.3 這個 runtime 真正的鏈路：schema -> validation -> retry -> repair -> fallback

#### A. Schema / contract

這個專案不是讓模型輸出任意 JSON，而是逐步把契約收斂到幾種明確格式：

- GM 同步 tag：
  - `STATE`
  - `LORE`
  - `NPC`
  - `EVENT`
  - `IMG`
  - `TIME`
- Async extraction 契約：
  - `event_ops`: `update[{id,status}]` + `create[...]`
  - `state_ops`: `set / delta / map_upsert / map_remove / list_add / list_remove`
- Lore Console proposal 契約：
  - `<!--LORE_PROPOSE {...} -->`
- 角色狀態 schema：
  - `character_schema.json`
  - `default_character_state.json`

這表示模型不是直接操作 DB；它只能在既定 schema 內提出 patch。

#### B. Validation

真正寫入 runtime 前，會先走 deterministic gate：

- `current_phase` 只能是 allowlist
- 點數欄位必須是數值
- map/list 類型必須符合 schema
- 非 schema `_add/_remove` 直接丟棄
- scene / instruction / system keys 不可污染 `character_state`
- NPC `tier` 走 allowlist 正規化
- event status 走標準化與升級順序

這層的設計假設是：

> LLM 可以提 patch，但不能決定 commit 規則。

#### C. Retry

重試不是一個統一的 global retry，而是分散在不同失敗型別：

- Gemini：
  - key fallback
  - network retry + backoff
  - rate-limit key cooldown
- Auto-play：
  - 以 turn 為單位 retry
  - consecutive error backoff
- 串流 / regenerate：
  - 補了 retry 與 placeholder handling

這裡的設計假設是：

> 先區分 provider fault、transport fault、contract fault；不是所有錯都該重試。

#### D. Repair

`state` 路徑在 deterministic gate 之後，多了一層 LLM reviewer：

1. 原始 update 先進 `_validate_state_update`
2. 若有 violations，才進 `_review_state_update_llm`
3. reviewer 只能回：
   - `patch`
   - `drop_keys`
4. reviewer 回來的 patch 還要再進一次 deterministic gate

所以 repair 不是第二個自由 agent，而是 **一個被 gate 包住的 repair worker**。

#### E. Fallback

這個 project 的 fallback 很多，而且有分層：

- provider fallback：
  - Gemini key fallback
  - image provider fallback
- contract fallback：
  - `event_ops/state_ops` 優先，legacy `events/state` 仍保留
- parse fallback：
  - Claude JSON parse 失敗時退回 plain text
  - 某些 LLM JSON response 失敗時用 regex 擷取
- workflow fallback：
  - async extractor 某一路失敗時跳過，不阻斷主流程
  - trace 寫檔 best-effort，不阻斷主流程
- capability fallback：
  - tools 不支援時直接 ignore，不假裝支援

這裡的重點不是「永不失敗」，而是 **把失敗限制在局部，不讓它污染 canonical state**。

### 2.4 一個重要的設計結論

從這個 repo 的演化來看，這個專案最後沒有往「讓 GM agent 自由 tool call」發展，反而往：

- 明確 contract
- 嚴格 gate
- 分層 memory
- read-only workspace

這條路走。

這是一個很典型的 agent engineering 判斷：

> 對長期狀態敏感的系統，open-ended tool use 往往不如 constrained workflow 穩定。

---

## 3. Memory / Retrieval

### 3.1 記憶分層

| 層級 | 內容 | 寫入方式 | 讀取方式 | 是否 authoritative |
| --- | --- | --- | --- | --- |
| Turn-local context | recent messages、dice、debug directive | 當回合即時計算 | 直接注入當前 prompt | 否 |
| Branch runtime state | `character_state.json`、`npcs.json`、`world_day.json`、`dungeon_progress.json` | 同步 tag + gated apply | system prompt + runtime 邏輯 | 是 |
| Retrieval index | `state.db`、`lore.db`、`events.db` | canonical state/lore/event 同步更新或重建 | 按 query / must-include 檢索 | 否，索引層 |
| Mid-term memory | `conversation_recap.json`、`gm_plan.json` | compaction / async extraction | system prompt / augmentation | 否 |
| Long-term memory | `story_anchors`、base lore | async extraction + normalize | `critical_facts` / lore retrieval | 否，但高度持久 |
| Branch snapshots | 每則 GM message 的 state/npc/world/day/dungeon snapshot | `_process_gm_response()` | fork/edit/regen/merge 回放 | branch-time authority |

### 3.2 短期 state：什麼該常駐、什麼不該常駐

短期 state 現在被明確拆成兩種：

#### 常駐注入

- core state text
- critical facts
- NPC summary
- recap
- dungeon context

這些是每回合都要讓模型看到的最小上下文。

#### 按需檢索

- 詳細 inventory / abilities / relationships / missions / systems / NPC 條目
- branch lore
- 相關事件

這些不再全量塞進 system prompt，而是透過 retrieval 在 `_build_augmented_message()` 按 query 注入。

這個改法背後的假設是：

> 「永遠給很多」不等於「記得更準」；對 agent 來說，更重要的是給對的 context。

### 3.3 長期檢索：不是只有 lore，還包括 state / event / plan / anchors

目前實際上有四種長期檢索來源：

1. `lore.db`
   - 世界層知識
   - base lore 為主
2. `branch_lore.json`
   - 分支局部設定
   - 避免污染全域 lore
3. `state.db`
   - 角色狀態與 NPC 索引
   - 支援 must-include 與 archived NPC 過濾
4. `events.db` + `gm_plan.json` + `story_anchors`
   - plot pressure、前瞻敘事、身份層記憶

也就是說，這個系統不是只有單一 RAG，而是 **multi-store retrieval**。

### 3.4 Compaction 的邊界

compaction 在這個專案裡很重要，但它有明確邊界：

- 它負責壓縮對話 continuity
- 它不負責成為 canonical state
- 它不能取代 snapshot
- 它在摘要前會清掉 `ACTIONS` / 可選行動區塊，避免把未發生的選項壓進長期記憶

所以 compaction 的角色比較像：

> 把 conversation 變短，不把 narrative 變真。

### 3.5 一條很關鍵的 memory 邊界

這個 repo 後期非常明確地把以下三者分開：

- `canonical state`: 可寫回、可分支、可 snapshot
- `retrieval memory`: 幫助生成，但不是 authority
- `compressed narrative`: 幫助 continuity，但不能回寫 state truth

這個邊界一旦沒分清，就會出現：

- option 被當成既成事實
- scene transient 汙染角色狀態
- branch time drift
- archived NPC 長期污染 prompt

---

## 4. Workflow vs Free Agent

### 4.1 最值得拿來講的例子：async extraction 從 free-form 改成 phase-based

這是目前最像「agent workflow 設計演進」的一段。

#### 舊設計

原本 async extraction 比較接近單次 free-form extractor：

- 一次讀 GM text
- 一次輸出 lore / events / npcs / state / time / dungeon 等混合結果
- 同一個 worker 同時碰 narrative memory 與 canonical state

這種設計的問題是：

- state 與 non-state 的錯誤容易互相污染
- 寫入責任不清楚
- 很難分辨是 prompt 問題、parser 問題、還是 commit 問題
- `current_dungeon` / state / NPC provenance 這種跨 writer 欄位容易出現雙寫衝突

#### 新設計

後來它被拆成 phase-based pipeline：

1. `non-state` extractor
   - lore
   - events / event_ops
   - plan
   - npcs
   - story_anchors
   - time
   - branch_title
   - dungeon
2. 取得 per-branch state lock
3. 先套 `story_anchors`
4. `state-only` extractor
   - 只處理 `state_ops`
   - 明確禁止 scene/transient 汙染
   - v1 明確不允許 async 更新 `current_dungeon`

#### 為什麼 phase-based 會更穩

這裡的設計假設是：

> 當一個 agent 同時在做「敘事整理」和「canonical state commit」時，應該拆 phase，而不是讓它一次自由輸出所有結果。

phase-based 之後，穩定性改善來自：

- blast radius 變小：
  - `non-state` 壞掉，不一定會污染 state
- lock ownership 變清楚：
  - state 寫入集中到單一路徑
- trace 更清楚：
  - `extract_tags_non_state_*`
  - `extract_tags_state_only_*`
- contract 更可測：
  - `state-only` 可以獨立補 prompt 規則與測試

#### 觀察到的穩定性差異

這裡沒有完整的 production success-rate dashboard，但從 PR 與後續 guardrail 的演化可以看出幾個明顯趨勢：

- state 的 contract 越來越窄，不再允許 free-form 更新一切
- `current_dungeon` 被移出 async state writer，表示雙寫衝突曾是實際問題
- `state_ops` / `event_ops` 被明確定義，表示 free-form title/object 漂移已經造成過不穩定
- 測試從單純 extractor test，擴展到：
  - `tests/test_extract_tags_async.py`
  - `tests/test_state_update.py`
  - `tests/test_state_review.py`
  - `tests/test_state_reviewer.py`

換句話說，這不是「多了一個 feature」，而是把一段 agent workflow 從 **自由生成** 改成 **分 phase 提交**。

### 4.2 另一個很有代表性的結論：explicit workflow 有時比 free agent 更好

`#74` 一度加入 Claude CLI `Read,Grep` tool access，想讓 GM 做事實查核；`#75` 很快又移除，理由是它和 `critical facts injection` 重疊，後者更穩定。

這是一個非常好的面試例子：

- 一開始的假設：
  - 給 agent 工具，它會更一致
- 後來的結論：
  - 對這種 stateful RPG 系統，**先把關鍵事實顯式注入 prompt**，比讓 agent 自己再去找檔案更穩

這個結論很 agent engineering，不是 prompt engineering。

---

## 5. Eval / Tracing

### 5.1 現有觀測基礎

目前 repo 已經有兩個很好的基礎設施：

#### A. `usage.db`

記錄：

- provider
- model
- call_type
- token usage
- `elapsed_ms`
- branch_id

可以從 `/api/usage` 拉 summary。

這讓系統已經具備：

- call volume
- provider/model 分佈
- latency 基礎資料
- cost / token 使用量

#### B. `llm_traces`

按 stage 寫 request / response raw payload：

- `gm_request`
- `gm_response_raw`
- `extract_tags_*_request`
- `extract_tags_*_response_raw`
- `state_cleanup_request`
- `state_cleanup_response_raw`
- lore chat / lore promote review 等

這層的價值不是 dashboard，而是 **replay / root cause analysis**。

### 5.2 如果要把「tool success rate、latency、failure type」變成基本觀測，最小單位應該是 stage，不是 feature

建議把這個系統裡的「tool」定義成 **tool-like stage**：

- `gm`
- `gm_stream`
- `extract_non_state`
- `extract_state_only`
- `oneshot_state_review`
- `state_cleanup`
- `compaction`
- `npc_evolution`
- `lore_chat`
- `image_gen`

### 5.3 最小觀測模型

| 指標 | 定義 | 目前資料來源 | 建議補強 |
| --- | --- | --- | --- |
| Success rate | `success / attempts` | trace + route logs | 每個 stage 補 `status=success|error|fallback|skip` |
| Latency | p50 / p95 / max | `usage.db.elapsed_ms` | 把更多 background stage 也統一落 usage |
| Failure type | timeout / network / parse / validation / reviewer / empty_output / unsupported_tool | provider log + trace raw | 寫成標準化 `error_type` 欄位 |
| Fallback rate | `fallback_used / attempts` | 目前只能從 code path 推測 | 補 `fallback_used=true` 與 `fallback_kind` |
| Repair rate | `reviewer_invoked / violations`、`reviewer_patch_applied / reviewer_invoked` | reviewer usage + tests | 補 structured reviewer trace tag |
| Schema violation rate | `violations / state_updates` | gate 目前主要在 log / tests | 把 violation summary 落 trace |

### 5.4 Failure taxonomy 建議

最少應該把失敗分成：

- `provider_timeout`
- `provider_network`
- `provider_auth_or_quota`
- `empty_response`
- `json_parse_error`
- `schema_validation_drop`
- `repair_timeout`
- `repair_invalid_patch`
- `unsupported_tool`
- `workflow_skip`
- `snapshot_drift`

這樣才看得出來問題是在：

- provider availability
- model contract
- parser robustness
- commit gate 太嚴或太鬆
- workflow coupling

### 5.5 這個 project 最值得延伸的一點

`usage.db` 目前主要看 token / call / latency，`llm_trace` 主要看 raw payload。

下一步最划算的不是做更大的 dashboard，而是把兩者接起來：

- `usage.db` 看量化趨勢
- `llm_trace` 看失敗樣本
- `tests/*` 固化已知 failure mode

這就是一個很典型的 agent harness 閉環：

> trace 找 case -> test 固化 -> workflow/gate 修正 -> usage/trace 再看是否改善

---

## 6. Harness Engineering：這個 project 驗證了哪些假設

下面這張表不是 feature list，而是「遇到什麼問題、做了哪些 runtime/harness 改動、解掉什麼、怎麼衡量」。

| 假設 | 遇到的問題 | 做的改變 | 解決什麼 | 怎麼衡量 |
| --- | --- | --- | --- | --- |
| 顯式關鍵事實比自由檔案查詢更穩 | GM 對 phase / inventory /關鍵關係常搞混 | `critical_facts` 注入；一度開 `Read,Grep`，後來移除 | 降低對 agent 自主搜尋的依賴，改成 deterministic context | `#74` 加入、`#75` 移除；以行為穩定性與維護成本判斷 |
| 全量 prompt 不是最好的 memory | lore TOC 6k-8k tokens、上下文成本高、相關性差 | `#61` 改成 hybrid lore retrieval + compact lore summary；`#136` 改成 Core State + State RAG | 把 prompt 從「全部都放」改成「常駐最小集 + 按需檢索」 | changelog 有約 50-token lore summary、~11ms embedding query；另有 `/api/lore/embedding-stats` 與 `state.db` summary |
| LLM 不能直接寫 canonical state | 非法 phase、錯型別、scene key、instruction key 汙染 state | `#72` 先擋垃圾 key；`#128` 加 deterministic validation gate | 把 state corruption 從 commit path 擋掉 | `tests/test_state_review.py`、違規項目可 trace/log |
| Repair 應該是 gated，不該是第二個自由 agent | 單靠 deterministic gate 會 drop 太多資訊；單靠 LLM 又不安全 | `#128` 加 reviewer `patch/drop_keys`，且 reviewer output 再進第二次 gate | 保留可修復資訊，同時不放棄可控性 | `tests/test_state_reviewer.py`、`oneshot_state_review` usage、timeout/semaphore 保護 |
| 單一 free-form extractor 太耦合 | state / lore / event / dungeon 混在一起，寫入責任不清楚 | `#140` / `#166`：`event_ops/state_ops` + split async extractor state pipeline | 將 narrative extraction 和 canonical state commit 分 phase，縮小 blast radius | 新增 extractor/state tests；trace stage 也更細 |
| 長期記憶會被模型自己的輸出污染 | 未選的 `ACTIONS` 被 recent / compaction 當成已發生事實 | `#139` 加 ACTIONS marker 與 choice block stripping | 阻止 self-reinforcing memory contamination | regression tests + 後續 prompt docs 固化契約 |
| 需要 stage-level 可觀測性，不是只有 server.log | 出錯時不知道是 prompt、provider、parser、extractor 哪一段壞 | `#132` 加 structured `llm_trace` | 能對單則 message 回放 request/response/後處理 | `tests/test_llm_trace.py` + `data/llm_traces/...` |
| read-only agent workspace 比 open tool use 更適合這個 domain | prompt-only 對 Codex 類 agent context 不夠，但直接開寫權限風險太高 | `#170` 加 `codex_agent` read-only provider | 給 agent 足夠上下文，又不讓它直接改 runtime data | 安全性由設計保證；仍沿用原 parser / snapshot / extractor pipeline |
| availability 問題要分 fault class 處理 | Gemini transient network、quota/key exhaustion、image provider 不穩 | `#69` key/network retry；image key/provider fallback；auto-play retry/backoff | 提高 availability，不把 transient fault 當 model fault | 可從 `usage.db` latency、provider error log、後續 trace failure type 量測 |

---

## 7. 面試時可以怎麼講

如果要用一句話總結這個 project 的 agent engineering 方向，可以這樣講：

> 我不是把 LLM 當作直接做事的 agent，而是把它放進一個有 schema、validation、repair、fallback、memory 分層和 trace 的 runtime。很多改動看起來像功能，其實是在驗證一個假設：對長期狀態敏感的系統，phase-based workflow 與 constrained commit pipeline，通常比 free-form agent autonomy 更穩。

如果要再更具體一點，可以講三個關鍵學到的設計原則：

1. **把 side effect contract 化**
   - 先讓模型輸出結構化意圖，再決定是否 commit。
2. **把 memory 分層，不要全部塞進 prompt**
   - 常駐最小集、按需檢索、snapshot 保真、compaction 只做 continuity。
3. **把 harness 當成產品的一部分**
   - trace、tests、gate、repair、fallback 不是附加品，而是 agent 能上 production 的前提。
