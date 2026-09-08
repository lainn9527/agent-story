#!/usr/bin/env python3
"""v2 戰力面板 — read-only live power panel for in-session GM play.

Standalone stdlib HTTP server. **v2-owned & app-free**: it reads a play branch's
character_state.json + npcs.json via the gm_tools loaders and NEVER imports the
v1 Flask `app`. Serves a browser panel showing 級 / domains / 對位讀數, auto-
refreshing, implementing the layered-disclosure model (深不可測 / 未知 for out-of-
range / concealed; narration stays the place for relative danger — this panel is
the "what you've learned" ledger).

Run:  python3 scripts/play/panel.py [--port 5053] [--story demon_tide]
Open: http://localhost:5053         (auto-picks the latest play_* branch)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gm_tools  # noqa: E402  app-free loaders (story_io only)
from story_core import story_io as sio  # noqa: E402

DEFAULT_STORY = "demon_tide"

# 修仙 home-ladder anchors. 級 = 大境界base + 層；大境界 = 級//10（每大境界佔 10 格）。
# 這裡是「大境界 base」（級的十位×10）；玩家實際級 = base + minor_realm(層)。
REALM_TO_LEVEL = {
    "凡人": 0, "練氣": 0, "築基": 10, "金丹": 20,
    "元嬰": 30, "化神": 40, "煉虛": 50, "合體": 60, "大乘": 70,
}


def _latest_play_branch(story: str) -> str | None:
    base = os.path.join(sio.STORIES_DIR, story, "branches")
    best, best_t = None, -1.0
    for d in glob.glob(os.path.join(base, "play_*")):
        m = os.path.join(d, "messages.json")
        if os.path.exists(m):
            t = os.path.getmtime(m)
            if t > best_t:
                best_t, best = t, os.path.basename(d)
    return best


def _protag_level(st: dict):
    """主角細分級 = 大境界base + 層(minor_realm)。練氣一層→1、築基初期→11…。"""
    mr = st.get("major_realm")
    base = REALM_TO_LEVEL.get(mr)
    if base is None:
        return None
    layer = st.get("minor_realm")
    if isinstance(layer, str):
        m = re.search(r"\d+", layer)
        layer = int(m.group()) if m else None
    if not isinstance(layer, (int, float)) or isinstance(layer, bool):
        layer = 0 if mr in (None, "凡人") else 1
    return int(base + max(0, min(9, int(layer))))


def _matchup(p, e):
    """Relative readout from the protagonist's POV. 質變碾壓按【大境界】(級//10)：跨大境界才有
    須代價/只能逃/深不可測。同一大境界內再用【層】(個位)細分強弱，但同境界無質變 → 最多到 warn
    (吃力/難纏)，不會是只能逃/深不可測。"""
    if e is None or p is None:
        return ("未知", "unknown")
    dmaj = (e // 10) - (p // 10)  # Δ大境界（質變軸）
    if dmaj <= -2:
        return ("你遠勝", "adv")
    if dmaj == -1:
        return ("優勢", "adv")
    if dmaj == 1:
        return ("須代價才贏", "warn")
    if dmaj == 2:
        return ("只能逃", "danger")
    if dmaj >= 3:
        return ("深不可測", "danger")
    dd = e - p  # 同大境界：用層(個位)細分，同境界無質變碾壓 → 最多 warn
    if dd <= -5:
        return ("佔上風", "adv")
    if dd <= -2:
        return ("優勢", "adv")
    if dd <= 1:
        return ("勢均可拚", "even")
    if dd <= 4:
        return ("吃力（須策略）", "warn")
    return ("難纏（須剋制）", "warn")


_HOSTILE_HINT = ("鬼", "弦", "無慘", "再生", "多臂", "敵")


def _hostile(n: dict) -> bool:
    blob = " ".join([
        n.get("rank_label") or "",
        " ".join(n.get("domains") or []),
        n.get("name") or "",
    ])
    return any(k in blob for k in _HOSTILE_HINT) or n.get("lifecycle_status") == "terminal"


def _parse_ability(name: str, raw) -> dict:
    """Parse 功法/天賦 tags: [級N] [成:X] [天花板:Y] [性質:a／b] — rest is desc."""
    s = str(raw)
    lvl = re.search(r"\[級([\d.]+)\]", s)
    cheng = re.search(r"\[成[:：](.+?)\]", s)
    ceil = re.search(r"\[天花板[:：](.+?)\]", s)
    dom = re.search(r"\[性質[:：](.+?)\]", s)
    doms = [x.strip() for x in re.split(r"[／/、,，]", dom.group(1)) if x.strip()] if dom else []
    return {
        "name": name,
        "level": float(lvl.group(1)) if lvl else None,
        "cheng": cheng.group(1) if cheng else None,
        "ceiling": ceil.group(1) if ceil else None,
        "domains": doms,
    }


def _protag_abilities(st: dict) -> list[dict]:
    out = []
    for k, v in (st.get("talents") or {}).items():
        out.append(_parse_ability(k, v))
    for k, v in (st.get("techniques") or {}).items():
        out.append(_parse_ability(k, v))
    return out


def compute_panel(story: str, branch: str) -> dict:
    st = gm_tools.load_state(story, branch)
    npcs = gm_tools.load_npcs(story, branch)
    p = _protag_level(st)
    protag = {
        "name": st.get("name"),
        "level": p,
        "level_label": (f"{p}級（{st.get('major_realm')}{st.get('minor_realm')}層）"
                        if p is not None else "未知"),
        "phase": st.get("current_phase"),
        "status": st.get("current_status"),
        "dungeon": st.get("current_dungeon"),
        "fate_points": st.get("fate_points"),
        "abilities": _protag_abilities(st),
        "inventory": [{"k": k, "v": str(v)[:48]} for k, v in (st.get("inventory") or {}).items()][:10],
    }
    roster = []
    for n in npcs:
        e = n.get("level")
        e = e if isinstance(e, (int, float)) else None
        hostile = _hostile(n)
        tag, cls = _matchup(p, e) if hostile else ("", "")
        roster.append({
            "name": n.get("name"),
            "level": e,
            "rank_label": n.get("rank_label") or "",
            "domains": n.get("domains") or [],
            "lifecycle": n.get("lifecycle_status", "active"),
            "hostile": hostile,
            "matchup": tag,
            "matchup_cls": cls,
            "abilities": [_parse_ability(k, v) for k, v in (n.get("abilities") or {}).items()],
            "recall": (n.get("lifecycle_status") == "offstage"
                       and gm_tools._matches_current_scope(n.get("home_scope"), st)),
            "memory": (n.get("memory") or [])[-1:],
        })

    def sort_key(r):
        rank = 0 if (r["hostile"] and r["lifecycle"] == "active") else (1 if r["lifecycle"] == "active" else 2)
        return (rank, -(r["level"] or 0))

    roster.sort(key=sort_key)
    return {"protagonist": protag, "roster": roster, "story": story, "branch": branch}


HTML = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>戰力面板</title>
<style>
:root{--bg:#0f1117;--card:#171a23;--card2:#1d2130;--line:#2a2f3e;--txt:#e6e8ef;--dim:#8b91a3;--gold:#e0b25a;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font-family:"PingFang TC","Noto Sans TC",system-ui,sans-serif;font-size:14px}
.wrap{max-width:1080px;margin:0 auto;padding:18px;display:grid;grid-template-columns:340px 1fr;gap:16px}
@media(max-width:760px){.wrap{grid-template-columns:1fr}}
h1{font-size:15px;letter-spacing:2px;color:var(--gold);margin:0 0 10px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
.lvlbadge{display:inline-block;background:linear-gradient(135deg,#3a2f15,#5a4520);color:var(--gold);
  border:1px solid #6b5526;border-radius:8px;padding:6px 12px;font-size:18px;font-weight:700;letter-spacing:1px}
.ablv{display:inline-block;min-width:30px;text-align:center;background:#2a2410;color:var(--gold);border:1px solid #6b5526;
  border-radius:5px;padding:1px 6px;font-size:11px;font-weight:700;margin-right:6px}
.meta{color:var(--dim);font-size:12px;margin-top:8px;line-height:1.6}
.sec{color:var(--gold);font-size:11px;letter-spacing:1px;margin:14px 0 6px;text-transform:uppercase;opacity:.8}
.chip{display:inline-block;border-radius:6px;padding:2px 8px;font-size:11px;margin:2px 4px 2px 0;border:1px solid var(--line);background:var(--card2);color:var(--dim)}
.chip.kill{color:#7ee0a0;border-color:#2f5a3f}
.chip.regen{color:#e98d8d;border-color:#5a2f2f}
.chip.hax{color:#c79df0;border-color:#46305a}
.chip.sense{color:#8db8e8;border-color:#2f4a5a}
.npc{display:flex;gap:10px;align-items:flex-start;padding:11px;border:1px solid var(--line);border-radius:10px;margin-bottom:9px;background:var(--card2)}
.npc.off{opacity:.5}
.npc .nlvl{flex:0 0 52px;text-align:center}
.npc .nlvl b{display:block;font-size:20px;color:var(--gold);font-weight:700;line-height:1.1}
.npc .nlvl span{font-size:10px;color:var(--dim)}
.npc .nbody{flex:1;min-width:0}
.npc .nname{font-weight:600}
.npc .nrank{color:var(--dim);font-size:11px;margin-left:6px}
.npc .nmem{color:var(--dim);font-size:11px;margin-top:5px;line-height:1.5;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.npc .recall{margin-left:8px;font-size:10px;color:#8db8e8;border:1px solid #2f4a5a;border-radius:5px;padding:1px 6px}
.npc .nskills{margin-top:6px}
.npc .skill{display:inline-block;margin-right:14px;font-size:12px}
.mu{float:right;font-size:11px;padding:2px 8px;border-radius:6px;border:1px solid}
.mu.adv{color:#7ee0a0;border-color:#2f5a3f}.mu.even{color:var(--gold);border-color:#6b5526}
.mu.warn{color:#e0b25a;border-color:#6b5526}.mu.danger{color:#e98d8d;border-color:#5a2f2f}.mu.unknown{color:var(--dim);border-color:var(--line)}
.ally{float:right;font-size:11px;color:#8db8e8;border:1px solid #2f4a5a;border-radius:6px;padding:2px 8px}
.foot{grid-column:1/-1;text-align:center;color:var(--dim);font-size:11px}
.unknown-veil{color:var(--dim);font-style:italic}
</style></head>
<body><div class="wrap">
  <div>
    <h1>⚔ 戰力面板</h1>
    <div class="card" id="protag">載入中…</div>
  </div>
  <div>
    <h1>敵我態勢</h1>
    <div id="roster"></div>
  </div>
  <div class="foot" id="foot"></div>
</div>
<script>
function domClass(d){if(/剋|斬|阻斷/.test(d))return'kill';if(/再生|多臂/.test(d))return'regen';
 if(/精神|詭異|因果|規則/.test(d))return'hax';if(/感知|氣場|嗅覺|觀氣|神識|鑑定/.test(d))return'sense';return''}
function chips(ds){return (ds||[]).map(d=>`<span class="chip ${domClass(d)}">${d}</span>`).join('')}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function load(){
 let r; try{r=await(await fetch('/api/panel'+location.search,{cache:'no-store'})).json()}catch(e){return}
 if(r.error){document.getElementById('protag').innerHTML='<span class="unknown-veil">'+esc(r.error)+'</span>';return}
 const p=r.protagonist;
 let ab=(p.abilities||[]).map(a=>{
   const lv=a.level!=null?`<span class="ablv">${a.level}級</span>`:'';
   const meta=[a.cheng?'成:'+esc(a.cheng):'',a.ceiling?'天花板:'+esc(a.ceiling):''].filter(Boolean).join(' · ');
   return `<div style="margin:5px 0">${lv}<b>${esc(a.name)}</b>${meta?` <span class="meta">${meta}</span>`:''} ${chips(a.domains)}</div>`;
 }).join('')||'<span class="meta">—</span>';
 let inv=(p.inventory||[]).map(i=>`<span class="chip">${esc(i.k)}</span>`).join('')||'<span class="meta">—</span>';
 document.getElementById('protag').innerHTML=
  `<div class="lvlbadge">${esc(p.level_label)}</div>`+
  `<div class="meta">${esc(p.phase||'')} ｜ 命運點 ${p.fate_points??'—'}<br>${esc(p.dungeon||'')}<br>${esc(p.status||'')}</div>`+
  `<div class="sec">功法 / 天賦（性質）</div>${ab}`+
  `<div class="sec">關鍵物品</div>${inv}`;
 function npcCard(n){
   const lvl = n.level==null ? '<span class="unknown-veil">?</span>' : n.level;
   const right = n.hostile ? `<span class="mu ${n.matchup_cls}">${esc(n.matchup)}</span>` : `<span class="ally">隊友/中立</span>`;
   const skills = (n.abilities||[]).map(a=>{
     const lv=a.level!=null?`<span class="ablv">${a.level}級</span>`:'';
     return `<span class="skill">${lv}${esc(a.name)} ${chips(a.domains)}</span>`;
   }).join('');
   const skillLine = skills?`<div class="nskills">${skills}</div>`:'';
   const mem = (n.memory&&n.memory[0])?`<div class="nmem">${esc(n.memory[0])}</div>`:'';
   return `<div class="npc ${n.lifecycle!=='active'?'off':''}">
     <div class="nlvl"><b>${lvl}</b><span>級</span></div>
     <div class="nbody">${right}<span class="nname">${esc(n.name)}</span><span class="nrank">${esc(n.rank_label)}</span>${n.recall?'<span class="recall">可召回</span>':''}
     <div>${chips(n.domains)}</div>${skillLine}${mem}</div></div>`;
 }
 const groups=[['active','在場'],['offstage','離場'],['terminal','已逝']];
 let rhtml='';
 groups.forEach(([k,label])=>{
   const items=(r.roster||[]).filter(n=>n.lifecycle===k);
   if(items.length) rhtml+=`<div class="sec">${label}（${items.length}）</div>`+items.map(npcCard).join('');
 });
 document.getElementById('roster').innerHTML=rhtml||'<span class="meta">尚無已記錄單位</span>';
 document.getElementById('foot').textContent=`${r.story} / ${r.branch} · 每 4 秒更新 · ${new Date().toLocaleTimeString()}`;
}
load();setInterval(load,4000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, ctype: str, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, "text/html; charset=utf-8", HTML)
            return
        if u.path == "/api/panel":
            q = parse_qs(u.query)
            story = (q.get("story") or [DEFAULT_STORY])[0]
            branch = (q.get("branch") or [None])[0] or _latest_play_branch(story)
            try:
                if not branch:
                    raise RuntimeError(f"no play_* branch found under {story}")
                data = compute_panel(story, branch)
            except Exception as exc:  # pragma: no cover - defensive
                data = {"error": str(exc)}
            self._send(200, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False))
            return
        self._send(404, "text/plain; charset=utf-8", "not found")

    def log_message(self, *a):  # silence
        pass


def main() -> int:
    global DEFAULT_STORY
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5053)
    ap.add_argument("--story", default=DEFAULT_STORY)
    args = ap.parse_args()
    DEFAULT_STORY = args.story
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"戰力面板 on http://localhost:{args.port}  (story={args.story})")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
