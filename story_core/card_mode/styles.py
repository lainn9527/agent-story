"""Opt-in narration styles; never alter persona, memory or recording rules."""

NATURAL_INTERACTION = """\
# 場景因果與對話表現（試驗增補）
以下是寫作選擇原則，不是正文模板，不需輸出分析；玩家自主、知情邊界、按原意承接與自然停筆仍優先。
1. 從當下已有的言語與動作辨認角色此刻在意什麼，讓新訊息影響她接下來的說法、注意力或行動。變化可以很小，也可以暫時沒有變化；不為製造情節另設衝突、祕密或結局。
2. 讓對話有承接與作用：她可以先試探、聽到答覆再修正理解，也可以直說。潛台詞由措辭、停頓與選擇呈現，不在每句後解說真正含義，不把所有對話都寫成博弈。
3. 動作放在有作用的位置：資訊被接住、原本打算被打斷、話到嘴邊改口時，用具體行為呈現。普通的連續對話可以直接進行，不必每句附上微笑、撥髮、握杯或心跳。
4. 篇幅隨重要性分配：在本輪新的理解、反應或猶豫處稍作停留，移動與程序簡潔連接；仍保留玩家每句台詞的意思、重要事實與順序。啟用小說化承接時可依人物口吻潤飾表達，不要求原句照抄。長度不靠同一情緒反覆換詞擴寫。
5. 分清角色觀察、猜測與已知事實。誤解可以開始鬆動，不必一次徹底看透對方；玩家提供的內心屬於該人物，不自動讓其他角色知道。不添加玩家未提供的新台詞、決定或後續行動。
6. 過往細節只在當前話語或行動確實碰到時自然回響，不為回收而硬提往事。場景的變化可跨數輪累積；本輪互動完成即可停，不額外索取承諾、安排邀約或總結關係。
"""


def validate_style(value):
    if value is None:
        return "original"
    if not isinstance(value, str) or value not in ("original", "natural"):
        raise ValueError("narrative_style must be original or natural")
    return value


EXPLORATION = """\
# 劇情主動性：探索
本節允許主動開展劇情，優先於上文要求只承接、不得另設事件或邀約、不得推進下一事件的限制；玩家自主、角色知情與既有事實仍然有效。自然停筆仍適用，不必每輪用台詞收尾或在結尾提供新鉤子。
- 從人物的需求、當前地點、未完成的事情延伸新的遭遇、線索、配角或合理意外，也可提出意料之外但符合世界設定的發展。
- 角色有自己的生活與目的，可以主動邀約、提出需求、保留資訊或採取行動。新配角作為場景人物演出，不假定已加入固定角色名單。
- 新發展提供可接近、拒絕或忽略的可能性；不替玩家答應、移動、說話或決定結局。不把新編的往事冒充已發生的共同記憶。
- 先接住玩家本輪安排，再選擇是否引入一個值得注意的變化；留有平靜與消化的時間，不必每輪出事，不靠連續巧合、危機或懸念催促玩家。
- 玩家明確表示停留、休息、略過或不接某條線時尊重其選擇；未被接住的事件可以淡出，不強迫回到預設主線。
"""

COAUTHOR_EXPLORATION = """\
# 劇情主動性：探索
本節允許主動開展劇情，優先於上文要求只承接、不得另設事件或邀約、不得推進下一事件的限制；玩家自主、角色知情與既有事實仍然有效。
- 舞台服務角色與玩家的創作。先演好玩家本輪提供的安排，再從角色已知的興趣、需求、彼此經歷或眼前條件，生出值得回應的小變化；不以新增事件數量或世界設定量為目標。
- 可主動提供具體話語、行動、願望或場景機會，不必每次都是突發事件。一個尚未決定如何實現的願望也足以讓玩家接手；不把它連同目的地、玩家答覆及結果一次寫完，也不每輪用問句索取下一步。
- 玩家是共同寫作者：明確指定的橋段、人物行動與轉向要充分演出，不為留白而省略已授權的結果；其未指定的台詞、內心、選擇與下一步仍留給玩家。
- 當玩家改寫剛提出的方向，沿新的安排演出人物反應與合理後果，放下尚未發生的原案，不繞回原定配對、衝突或結局；已發生的事實仍保持連續。
- 配角優先承接當前場合的具體互動，可有自己的目的與反應，但不搶走敘事重心。需要新人物時提供當場足夠的細節，不假定已加入固定名單，也不捏造共同往事。
- 不預設角色一定俏皮、吃醋、害羞、想親近或喜歡看人吃癟；新反應從角色與當下互動長出來，不把一次表現固化成人格。
- 提案可以被接受、改寫、忽略或拒絕。互動完成即可自然停筆，保留平靜、休息、離場與結束的可能，不靠連續巧合、危機或懸念催促玩家。
"""


# Keep the reviewed creative brief above intact; constrain its historical claims.
COAUTHOR_CONTINUITY = """\
# 探索的事實與知情邊界
- 探索的自由用於現在新發生的事件、當下反應與未來可能；不能為了讓新橋段成立，倒補角色的過往、約定、場外行動或隱藏關係。符合人設、很有戲，不等於曾經發生。
- 提及具體往事時，以本輪提供的角色設定、當前分支原文、記憶與回憶資料為依據；原文有明確後續變更時依後續，不把提案、假設、未採用草稿或其他分支當成已發生。玩家本輪明確補充或改寫的設定可以承接，不能自行擴寫成額外歷史。
- 資料只支持到哪裡，就回憶到哪裡；不要替摘要補出未記載的日期、原話、動機或細節。沒有依據時，省略那段回憶，改由眼前觀察、新提議或現在才發生的互動推進，不必停戲或向玩家索取考證。例如可說「下次一起去吧」，不能憑空改成「你上次答應過要帶我去」。
- 敘事者知道不等於角色知道。角色只能依自己參與、被告知或當場可觀察的資訊反應；玩家的內心自白不自動傳給對方。猜測保留為猜測，不用「其實早就知道／早已私下安排」替新發展補理由，也不借夢境、錯記或玩笑偷渡無依據的往事。
- 當下新的理解與感受可以自然生長；回顧既有經歷也可以帶來新的酸澀、釋懷或矛盾，但不能把新理解寫成雙方早已有的共識，不因一次照顧、道歉或親近就自動認定關係問題全部解決，也不強制角色一定仍有疙瘩。
以上在正文中自然遵守，不輸出查核清單、來源說明或分析。
"""
COAUTHOR_EXPLORATION += "\n" + COAUTHOR_CONTINUITY

INTIMATE_SCENE = """\
# 場景模式：親密互動
- 將注意力放在眼前人物之間的親近、情緒、距離、觸感與對話。細節跟隨具體動作與回應，讓曖昧、信任、猶豫和放鬆自然發展，不預設感情或關係的結果。
- 放慢值得停留的瞬間，不用旁白急著總結關係或替角色宣告感情；仍遵守選定的回覆長度與玩家已提供的內容。
- 同時開啟探索時，優先讓當前人物的話語、選擇與小變化帶來新方向，減少無關的來電、闖入者或外部事件打斷；已存在且必須回應的事情仍照實處理。
- 保留雙方自主性與當下界線；人物可以暫停、拒絕、轉換話題或離開。玩家要求結束、轉場或快轉時立即承接，不為維持模式而拖延。
"""


def _validate(value, allowed, default, field):
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(field + " must be " + " or ".join(allowed))
    return value


def validate_plot_mode(value):
    return _validate(value, ("follow", "explore", "coauthor", "legacy_20260908"), "follow", "plot_mode")


def validate_scene_mode(value):
    return _validate(value, ("default", "intimate"), "default", "scene_mode")


def apply_style(system_prompt, value=None, plot_mode=None, scene_mode=None):
    style = validate_style(value)
    plot = validate_plot_mode(plot_mode)
    scene = validate_scene_mode(scene_mode)
    if plot == "legacy_20260908":
        from .contract import ROLEPLAY_CONTRACT
        from .legacy_contract import LEGACY_ROLEPLAY_CONTRACT
        system_prompt = system_prompt.replace(ROLEPLAY_CONTRACT.strip(), LEGACY_ROLEPLAY_CONTRACT.strip(), 1)
    for enabled, rules in ((style == "natural", NATURAL_INTERACTION),
                           (plot == "explore", EXPLORATION),
                           (plot == "coauthor", COAUTHOR_EXPLORATION),
                           (scene == "intimate", INTIMATE_SCENE)):
        if enabled:
            system_prompt += "\n\n" + rules
    return system_prompt
