# Time Economy — 規格（時間作為通用資源底層）

> 狀態：草案（待 review）。目標＝把「時間」做成 genre-agnostic 的基礎層，讓每個
> montage-scale 的 action 都有機會成本（代價感）。修真/武俠/校園戀愛/偵探共用同一引擎。

## 0. 問題診斷（現在的 world-day 為何沒屌用）

不是「更新會失敗」，是它**從沒被當資源用**：

1. **三套互不相通的計時**：v1 `<!--TIME-->` tag（`world_timer.process_time_tags`）、v2 `time` delta（`gm_v2/apply.advance_time`）、固定副本成本（`advance_dungeon_enter/exit`）。各走各的。
2. **harness（/play，玩家實際在玩的地方）完全不推進**：`gm_post` payload 無時間欄位，play 分支連 `world_day.json` 都不存在 → 鐘是死的。
3. **沒有消費者**：就算推進了，沒有 deadline、沒判定讀它、UI 不突顯 → 空轉。
4. 存在獨立 `world_day.json`（`BASE_DIR` 路徑），與 harness 路徑邏輯不一致。

**結論**：time_economy **取代**這三套入口，並給它一個會咬人的消費者（deadline）。

## 1. 核心概念

時間＝**第二資源帳本**（相對 catalog 的命運點數）：

| | 命運點數（catalog） | 時間（time economy） |
|---|---|---|
| 來源 | 可賺 | 不可賺、單向遞減 |
| 消費 | 買道具/功法 | 每個 montage action 抽一筆 |
| 結算 | 隨時 | deadline 到就**強制 fire** |

三件物件：**Clock**（branch 當前時間）＋ **Action→cost**（montage action 的時間價）＋ **Deadline**（倒數、到點強制發生）。modifier＝天賦/資源/環境對 cost 的倍率。

**代價感公式**：`機會成本 ＝ 有限預算 ＝（時間成本）＋（有牙的 deadline）`。少了 deadline，時間成本是空的。

## 2. Schema：`data/stories/<world>/time_economy.json`（per-world config）

```json
{
  "unit": "day",
  "actions": {
    "cultivate":   {"label": "閉關修煉", "base_cost": 30, "scale": "calendar"},
    "travel":      {"label": "趕路",     "base_cost": 3,  "scale": "calendar"},
    "rest":        {"label": "休整療傷", "base_cost": 2,  "scale": "calendar"},
    "sect_duty":   {"label": "宗門任務", "base_cost": 5,  "scale": "calendar"}
  },
  "modifiers": [
    {"id": "rixi_talent", "applies_to": ["cultivate"], "source": "talents",
     "match": "日息", "mult": 0.1, "note": "陽系天賦＝修煉快十倍"}
  ],
  "deadlines": [
    {"id": "mochao", "label": "魔潮", "next_at": 365, "recurring": 365,
     "warn_at": [90, 30, 7], "on_fire": "強制觸發魔潮事件，不論玩家是否準備好"}
  ],
  "dungeon_time": "charged"
}
```

- `scale: "calendar"` 標記「會抽時間」的 action；場景級動作（對話、單場戰鬥、搜一個房間）**不在表內＝不抽**（紀律①）。
- 修煉的每「成」一步成本可在功法樹節點上以 `cultivate_cost` 覆寫，預設取 `actions.cultivate.base_cost`。
- `dungeon_time` ∈ `frozen | charged | dilated`，見 §5。

## 3. 引擎介面（app-free，catalog_tools 的雙生子）

```
status(story, branch) -> {now, unit, upcoming: [{id, label, in_n, warn:bool}]}
advance(story, branch, action_id, *, ctx=state) ->
    {now, spent, fired: [deadline...], warnings: [...]}     # 算 base×modifier，推進鐘，回傳跨過的 deadline
spend(story, branch, days)  -> raw 推進（逃生口）
```

- **單一真相來源**：取代 `world_timer` 的三套入口；底層仍可用 `advance_world_day` 寫值，但所有推進都走 `advance(action)`。Clock 經 `story_io` 存取（修掉 BASE_DIR 路徑碎裂）。
- **deadline firing**：`now` 跨過 `next_at` 時放進 `fired`，並重算 `next_at`（recurring）。呼叫端（pipeline/harness）**必須**對 `fired` 反應。

## 4. 三條紀律的「落地點」（在哪段 code/流程強制）

| 紀律 | 落地點 |
|---|---|
| ① 只算 montage-scale | action 表只列 calendar 級；GM 只對閉關/趕路呼叫 `advance`，對話/單場戰鬥不呼叫。contract 加規則。 |
| ② cost 是 data 不是 GM 心證 | `base_cost` 在 config；GM 只給 `action_id`，引擎算 `cost×modifier`。GM 不能自填天數。 |
| ③ deadline 有牙 | `advance` 回傳 `fired`；pipeline/harness 在下一回合把 fired 當**強制事件**注入（魔潮已至，不管準備好沒）。 |

**修煉串接（Q1 收進來）**：引擎額外累計每個功法樹節點的「已投入天數」；達門檻（base×talent_mult）→ 自動升「成」。於是「天才 3 天 vs 凡人 1 月」是真實數字，不是 GM 隨口。

## 5. 副本時間政策 — ✅ 已決議：**B. Charged（副本扣主世界鐘）**

進副本＝消費主世界時間，所以**刷副本也有機會成本**（得點數但燒掉逼近魔潮的天數）。這是「每個 action 都有代價」核心的一致延伸。

**換算規則**：
- 每個副本 blueprint 宣告 `world_day_cost`（該副本消耗的主世界天數）；未宣告時取 `time_economy.dungeon_time_default`（demon_tide 暫定 **7 天**）。
- **進副本時即扣**（upfront commit）——玩家進去前就知道「這趟大約燒 7 天」，形成承諾感；取代舊的 `advance_dungeon_enter` 固定 +3。
- 出副本不再額外加（移除舊 `advance_dungeon_exit` +1）；療傷另算成 `rest` action。
- 副本**內部**的局部時間純敘事（世界觀隔離），不回灌主世界鐘——只有那筆 `world_day_cost` 進帳。

A（凍結）／C（時間膨脹）保留為**特例獎勵**：某些命運道具/法寶可提供「此趟副本不計時」或「副本修煉時間膨脹」，作為稀有獎勵，不是預設。`dungeon_time` 欄位＝政策開關（預設 `charged`）。

## 6. 收斂 / 遷移

- `world_timer` 的三套入口（v1 TIME tag、v2 time delta、固定副本成本）全改走 `advance(action)`。
- **harness `gm_post` payload 加 `actions`／`time`**——讓玩家實際在玩的地方終於會推進鐘（修掉根因 #2）。
- Clock 存取統一走 `story_io`（修掉根因 #4）。
- **UI**：header/戰力面板顯示「距魔潮 N 天」——讓 deadline 有存在感（修掉根因 #3）。

## 7. 分期

1. **核心引擎 + demon_tide config**：clock + action cost + deadline，跑通「閉關抽天數 → 推進鐘 → 逼近魔潮 → fire」。
2. **串修煉**：累計天數 → 自動升成（接上功法樹）。
3. **接 GM**：contract/SKILL 規則 + harness payload 欄位。
4. **副本政策**：依 §5 決定實作 B（換算率）。
5. **UI**：距 deadline 倒數。
