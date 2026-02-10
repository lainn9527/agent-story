# RisuAI-Inspired Features

這個資料夾包含從 [RisuAI](https://github.com/kwaroran/RisuAI) 借鑒的增強功能模組。

## 功能列表

### 1. Narrative Enhancer (敘述增強器)

基於正則表達式的文本轉換引擎，用於提升敘述品質。

**規則集：**
- `default` - 格式化行動標記和系統提示
- `combat` - 戰鬥描寫增強（傷害、狀態效果、暴擊）
- `literary` - 文學化風格（替換平淡動詞）
- `emotion` - 情緒指示器（emoji 標記）

**範例：**
```python
from scripts.narrative_enhancer import create_enhancer

enhancer = create_enhancer({"rules": ["combat", "emotion"]})

text = "你憤怒地攻擊敵人，造成100點傷害！"
result = enhancer.enhance(text)
# Output: "你😠 憤怒地⚔️ 攻擊敵人，💥 造成 100 點傷害！"
```

### 2. Math Engine (數學計算引擎)

使用 RPN（逆波蘭表示法）的數學表達式求值器，支援變數替換。

**支援運算：**
- 基本運算：`+, -, *, /, ^ (次方), % (取餘)`
- 邏輯運算：`&, |, !, >, <, >=, <=, =, !=`
- 函數：`abs, ceil, floor, round, min, max, sqrt`
- 變數：`$變數名` 或 `@全域變數`

**範例：**
```python
from scripts.math_engine import create_math_engine

engine = create_math_engine({"precision": 2})

# 計算表達式
result = engine.evaluate("(5 + $strength) * 1.5 - 2", {"strength": 10})
# Result: 20.5

# 處理 CALC 標籤
text = "你造成 <!--CALC (5 + $strength) * 1.5 CALC--> 點傷害"
processed = engine.process_text(text, {"strength": 10})
# Output: "你造成 22.5 點傷害"
```

### 3. Trigger System (觸發系統) - 🚧 開發中

事件驅動的自動化系統，根據條件觸發效果。

**計劃功能：**
- 條件檢測（關鍵字、變數比較）
- 效果執行（設定變數、修改訊息、顯示提示）
- 連鎖觸發

### 4. Command Parser (指令解析器) - 🚧 開發中

遊戲內指令系統，支援管道串接。

**計劃功能：**
- `/setvar`, `/getvar` - 變數操作
- `/calculate` - 計算表達式
- `/send` - 發送訊息
- 管道支援：`/echo "test" | /setvar key=result`

## Per-Story 配置

每個 story 的功能配置儲存在 `data/stories/<story_id>/features.json`：

```json
{
  "narrative_enhancer": {
    "enabled": true,
    "rules": ["combat", "emotion"]
  },
  "math_engine": {
    "enabled": true,
    "precision": 2
  },
  "trigger_system": {
    "enabled": false
  },
  "command_parser": {
    "enabled": false
  }
}
```

## API Endpoints

### GET /api/features
獲取當前 story 的所有功能配置。

### POST /api/features
更新當前 story 的功能配置。

```json
{
  "narrative_enhancer": {
    "enabled": true,
    "rules": ["combat", "emotion"]
  }
}
```

### POST /api/features/<feature_name>/enable
啟用特定功能。

### POST /api/features/<feature_name>/disable
停用特定功能。

## 整合流程

在 `app.py` 的 `_process_gm_response()` 中：

1. **Math Engine** - 在提取其他 tag 之前處理 `<!--CALC ... CALC-->`
   - 讓計算結果可以被用在 state updates 裡

2. **Narrative Enhancer** - 在所有 tag 提取完成後應用
   - 確保不會破壞 tag 結構

```python
# 1. 處理 CALC 標籤（早期）
engines = _get_feature_engines(story_id)
if "math" in engines:
    gm_response = engines["math"].process_text(gm_response, variables)

# 2. 提取所有 tags...
# (STATE, LORE, NPC, EVENT, IMG, TIME)

# 3. 應用敘述增強（最後）
if "enhancer" in engines:
    gm_response = engines["enhancer"].enhance(gm_response, mode="output")
```

## 測試

執行測試腳本：

```bash
python3 test_features.py
```

## 開發計劃

### Phase 1: ✅ 已完成
- [x] Feature configuration system
- [x] Narrative Enhancer
- [x] Math Engine
- [x] API endpoints
- [x] Integration into app.py

### Phase 2: 🚧 進行中
- [ ] Trigger System
- [ ] Command Parser

### Phase 3: 📅 未來
- [ ] Frontend UI for feature management
- [ ] Custom rule editor
- [ ] Community rule sharing

## 設計理念

這些功能採用 **per-story 可選啟用** 的設計，原因：

1. **Debug 方便** - 可以單獨開關某個功能來測試
2. **靈活性高** - 不同 story 可以有不同的功能集
3. **向後兼容** - 舊的 story 不會受影響
4. **性能優化** - 只載入啟用的功能，避免不必要的計算

與 RisuAI 不同的是，我們的系統更注重 **自動化**：
- RisuAI: 用戶手動開關 20 個選項
- 我們: AI 自動判斷，用戶只需要選擇「啟用敘述增強」

## 授權

這些功能的核心概念來自 [RisuAI](https://github.com/kwaroran/RisuAI) (GPL-3.0)。
我們的實作是獨立開發的 Python 版本，但保持相同的精神。
