# 新增一個世界（story）

**這份文件的目的**：把「開一個新世界」時**引擎真正會咬住的欄位**一次講清楚。
不知道這些，新世界會以難以察覺的方式壞掉（階段永遠不更新、貨幣顯示成別的世界的名字、
面板單位寫著「層」、副本選單冒出咒怨）。以 `urban_daily`（微光都市，都市低魔日常）為實例。

---

## 1. 兩個目錄，各自負責什麼

| 目錄 | 內容 | 進版控？ |
| --- | --- | --- |
| `story_design/<id>/` | 世界設計：system prompt、schema、預設狀態、NSFW 偏好 | 是 |
| `data/stories/<id>/` | Runtime：branches、catalog、lore.db、events.db、副本模板 | 否（runtime） |

`story_design/_shared/` 下的檔案**不是世界**（開頭 `_` 的資料夾會被 story 列舉略過），
是給各世界用 `@include _shared/<file>` 拉進 system prompt 的共用區塊。

## 2. 建立流程

```bash
# 1) 先寫 story_design/<id>/ 的內容（見 §3；wizard: python3 scripts/world_create.py <id>）
# 2) 再註冊 + scaffold runtime（design 目錄已有內容時不會被覆蓋）
python3 scripts/new_story.py <id> --name "顯示名稱" --description "一句話"
# 3) 補 data/stories/<id>/catalog.json 與 dungeons_template.json（見 §5、§6）
# 4) 重啟 server（或在 UI 📖 故事 面板切換）
```

`--switch` 會把它設為 active story；不加就只註冊，之後在 UI 切。

## 3. `story_design/<id>/` 檔案清單

| 檔案 | 說明 |
| --- | --- |
| `system_prompt.txt` | GM 人格與世界觀。**必寫** |
| `character_schema.json` | 狀態欄位、標籤、`phases`。**必寫**（引擎行為由它決定，見 §4） |
| `default_character_state.json` | 新 branch 的起始狀態 |
| `world_lore.json` | 種子 lore（可留 `[]`，世界觀寫在 prompt 裡即可） |
| `nsfw_preferences.json` | 親密場景偏好 |
| `parsed_conversation.json` | 匯入用開場腳本（可留 `[]`） |

### system_prompt.txt 的兩個地雷
1. **`.format()` 先跑，`@include` 後展開**。所以 prompt 本體裡任何**字面大括號**都要跳脫成 `{{` / `}}`
   （STATE tag 的 JSON 範例最常中招——漏跳脫＝每回合 500，錯誤訊息長得像 `KeyError: '"current_phase"'`）。
   被 include 進來的檔案**不需要**跳脫。
2. 可用的 placeholder 只有這些（別自己發明，會 KeyError）：
   `{character_state}`、`{world_lore}`、`{npc_profiles}`、`{narrative_recap}`、`{story_summary}`、
   `{team_rules}`、`{other_agents}`、`{critical_facts}`、`{dungeon_context}`。

## 4. 引擎咬住的欄位（換名字＝功能靜默消失）

這幾個 key 是**引擎的**，不是修真專用的。新世界請**沿用 key、只改 `label`**：

| key | 引擎用途 | urban_daily 的 label |
| --- | --- | --- |
| `fate_points` | 貨幣。任務發獎、purchase 扣款、UI 都寫死這個 key（`apply.py` / `actions.js`）。schema 標 `"engine_managed": true` 後，GM 只能用 `fate_points_delta` 增減，不能覆寫餘額 | 現金（元） |
| `sect_rank` | **身分**。變動時自動寫入 `completed_milestones`（進度錨，防 GM 重演已完成的橋段） | 職業／身份 |
| `major_realm` / `minor_realm` | **戰力階段／細分**。`級 = 階段base + 段`；階段變動同樣入 milestone | 能力階段／段 |
| `current_dungeon` / `completed_dungeons` / `technique_mastery` / `technique_days` | 引擎獨佔，GM 一律被 gate 擋下 | （不使用） |

### `phases`（新世界必加）
`character_schema.json` 的 `"phases": [...]` 宣告本世界合法的 `current_phase`。
**沒宣告就會 fallback 到全域 `VALID_PHASES`（主神空間/副本中/副本結算/傳送中/死亡）**，
於是這個世界每次寫 `current_phase` 都被 gate 當 `invalid_phase` 丟掉、階段永遠凍在預設值。
副本生命週期的 `副本中` / `副本結算` / `主神空間` 永遠是引擎獨佔，宣告了也不給 GM 寫
（見 `story_core/gm_v2/gate.py::_allowed_phases`）。

> ⚠️ **已知**：`demon_tide` 尚未宣告 `phases`，而它的 prompt 教 GM 用 `宗門/試煉秘境中/秘境結算/虛潮`
> ——目前這些全部被 gate 丟棄。要修就在它的 schema 加 `"phases": ["宗門","虛潮","傳送中","死亡"]`，
> 並把 prompt 的 STATE 段改成「副本階段由系統自動切換」（副本 phase 不該讓 GM 寫）。

### UI 標籤
戰力面板與 HUD 會讀 schema 的 `label` / `suffix`（貨幣名、`minor_realm` 單位、能力欄位標題），
所以 `urban_daily` 顯示「現金 8600」「1 段」「微光能力 / 天賦」而不是「命運點／層」。
新增欄位時記得給 `label`，不要在 JS 裡再寫死中文。

## 5. `data/stories/<id>/catalog.json`

```jsonc
{
  "realm_ladder": ["常人", "微光", "顯性", "越界"],  // index0 = 場外(凡人)；index1→0級、index2→10級…
  "rank_map": {},                                     // 職稱→階段（沒有階序的世界給 {}）
  "vendors": [{"id": "...", "name": "...", "currency_field": "fate_points",
               "currency_label": "現金", "scope": "home"}],
  "items":   [{"id": "...", "vendor": "...", "name": "...", "price": 900,
               "category": "工具", "level": "", "domains": [], "desc": "...",
               "grant_to": "inventory"}]
}
```

`realm_ladder` 同時是**賦級判斷**（`leveljudge.py`）的錨：主角級 = 階段 base + `minor_realm`。
每個階段固定佔 10 級，這是引擎算術，改不了——低魔世界的做法是**重新定義每一階能做到什麼**
（見 `_shared/urban_anchor.txt`），而不是去壓縮數字。

## 6. 沒有副本的世界

第一次載入時引擎會 seed 一份預設 `dungeons_template.json`（咒怨等 IP 副本）。
不吃無限流那一套的世界請把它清成 `{"dungeons": []}`——空清單所有讀取路徑都安全，
副本選單與「選一個真實 IP 世界」的 grounding 區塊會自動消失。

## 7. 引擎預設走 v2

新 branch 的 `data/stories/<id>/branches/<bid>/config.json`：

```json
{"gm_engine": "v2", "gm_transport": "tools", "image_gen_enabled": false}
```

`gm_engine` 不給就是 v1（已凍結的舊 pipeline）。`image_gen_enabled` **預設是 true**，
不想每回合燒圖就明寫 false。

## 8. 已知的粗糙處（新世界會踩到，但不致命）

- `contract.py` 的輸出契約（OUTPUT/TOOLS/NARRATE/RECORDER）文案是**修真語彙**寫死的
  （命運點數／功法／副本／境界）。欄位 key 是對的，所以功能正常，只是提示詞讀起來有修真味。
- `leveljudge.py` 的賦級 prompt 同樣寫死練氣／築基階梯；它只在敵對 NPC 缺 `level` 時才呼叫，
  低魔世界很少觸發，但真的觸發時給的 `rank_label` 會是修真語彙。
- 里程碑動詞寫死「晉身」「突破至」（`apply.py::_MILESTONE_FIELDS`）。

要讓這三處也 per-world，就得把契約文案與判官 prompt 參數化——尚未做，先記在這。

## 9. 實例：`urban_daily`（微光都市）

都市低魔日常 × 玩家自選能力 × 玩家自訂原創 NPC。可直接當範本抄：

- `story_design/urban_daily/system_prompt.txt`：世界觀、兩個核心賣點（自選能力／自訂 NPC）、開場指引、STATE 契約
- `story_design/_shared/urban_anchor.txt`：低魔版戰力錨（3 個階段、代價律、玩家自訂能力的校準程序）
  ——取代 `power_scale.txt` + `xianxia_anchor.txt`，**不要同時 include**（兩套級表會互相打架）
- `character_schema.json`：`phases` + 全部 label 換成都市語彙的示範
- `data/stories/urban_daily/catalog.json`：4 個生活向 vendor（超商／電子行／診所／《夜燈》交換板）
