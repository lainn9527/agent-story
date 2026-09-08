/* 戰力面板 (v2 power panel) — frontend-only.
   Reads /api/status + /api/npcs (which already return 級/domains/home_scope/talent
   tags), computes 級 / matchup / recall client-side, renders into #power-panel-modal.
   Self-contained: loaded after app.js; reads the global `currentBranchId`. */
(function () {
  "use strict";

  // 修仙 home-ladder anchors (matches demon_tide system_prompt 戰力錨點).
  // 大境界 base（級的十位×10）；玩家實際級 = base + 層(minor_realm)。大境界 = 級//10。
  var REALM_TO_LEVEL = {
    "凡人": 0, "練氣": 0, "築基": 10, "金丹": 20,
    "元嬰": 30, "化神": 40, "煉虛": 50, "合體": 60, "大乘": 70
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function domClass(d) {
    if (/剋|斬|阻斷/.test(d)) return "kill";
    if (/再生|多臂/.test(d)) return "regen";
    if (/精神|詭異|因果|規則/.test(d)) return "hax";
    if (/感知|氣場|嗅覺|觀氣|神識|鑑定/.test(d)) return "sense";
    return "";
  }
  function chips(ds) {
    return (ds || []).map(function (d) {
      return '<span class="chip ' + domClass(d) + '">' + esc(d) + "</span>";
    }).join("");
  }

  function parseAbility(name, raw) {
    var s = String(raw || "");
    var lvl = s.match(/\[級([\d.]+)\]/);
    var cheng = s.match(/\[成[:：](.+?)\]/);
    var ceil = s.match(/\[天花板[:：](.+?)\]/);
    var dom = s.match(/\[性質[:：](.+?)\]/);
    var doms = dom ? dom[1].split(/[／/、,，]/).map(function (x) { return x.trim(); }).filter(Boolean) : [];
    return {
      name: name,
      level: lvl ? parseFloat(lvl[1]) : null,
      cheng: cheng ? cheng[1] : null,
      ceiling: ceil ? ceil[1] : null,
      domains: doms
    };
  }

  function protagLevel(st) {
    var base = REALM_TO_LEVEL[st.major_realm];
    if (base == null) return null;
    var layer = st.minor_realm;
    if (typeof layer === "string") { var m = layer.match(/\d+/); layer = m ? parseInt(m[0], 10) : null; }
    if (typeof layer !== "number" || isNaN(layer)) layer = (st.major_realm == null || st.major_realm === "凡人") ? 0 : 1;
    return base + Math.max(0, Math.min(9, layer));
  }

  // 質變碾壓按【大境界】(級//10)：跨大境界才有須代價/只能逃/深不可測。同大境界內再用【層】細分，
  // 但同境界無質變 → 最多到 warn(吃力/難纏)，不會是只能逃/深不可測。
  function matchup(p, e) {
    if (e == null || p == null) return ["未知", "unknown"];
    var dmaj = Math.floor(e / 10) - Math.floor(p / 10);  // Δ大境界（質變軸）
    if (dmaj <= -2) return ["你遠勝", "adv"];
    if (dmaj === -1) return ["優勢", "adv"];
    if (dmaj === 1) return ["須代價才贏", "warn"];
    if (dmaj === 2) return ["只能逃", "danger"];
    if (dmaj >= 3) return ["深不可測", "danger"];
    var dd = e - p;  // 同大境界：用層(個位)細分，同境界無質變碾壓 → 最多 warn
    if (dd <= -5) return ["佔上風", "adv"];
    if (dd <= -2) return ["優勢", "adv"];
    if (dd <= 1) return ["勢均可拚", "even"];
    if (dd <= 4) return ["吃力（須策略）", "warn"];
    return ["難纏（須剋制）", "warn"];
  }

  var HOSTILE_HINT = ["鬼", "弦", "無慘", "再生", "多臂", "敵"];
  function isHostile(n) {
    var blob = [n.rank_label || "", (n.domains || []).join(" "), n.name || ""].join(" ");
    if (n.lifecycle_status === "terminal") return true;
    return HOSTILE_HINT.some(function (k) { return blob.indexOf(k) >= 0; });
  }

  function matchesScope(hs, st) {
    hs = String(hs || "").trim();
    if (!hs) return false;
    var dungeon = String((st && st.current_dungeon) || "").trim();
    if (dungeon) {
      var body = hs;
      [":", "："].some(function (sep) { if (body.indexOf(sep) >= 0) { body = body.split(sep)[1]; return true; } return false; });
      ["（", "("].some(function (br) { if (body.indexOf(br) >= 0) { body = body.split(br)[0]; return true; } return false; });
      body = (body || "").trim();
      return !!body && (dungeon.indexOf(body) >= 0 || body.indexOf(dungeon) >= 0);
    }
    return hs.indexOf("本世界") >= 0;
  }

  function abilityRow(a) {
    var lv = a.level != null ? '<span class="ablv">' + a.level + "級</span>" : "";
    var meta = [a.cheng ? "成:" + esc(a.cheng) : "", a.ceiling ? "天花板:" + esc(a.ceiling) : ""].filter(Boolean).join(" · ");
    return '<div style="margin:5px 0">' + lv + "<b>" + esc(a.name) + "</b>" +
      (meta ? ' <span class="meta">' + meta + "</span>" : "") + " " + chips(a.domains) + "</div>";
  }

  function personaText(n) {
    var p = n.personality;
    if (typeof p === "string" && p.trim()) return p.trim();
    if (p && typeof p === "object" && p.summary) return String(p.summary);
    return "";
  }

  function personaLine(n) {
    var bits = [];
    if (n.background) bits.push(esc(n.background));
    var pers = personaText(n);
    if (pers) bits.push(esc(pers));
    if (n.speech_style) bits.push("口吻「" + esc(n.speech_style) + "」");
    return bits.length ? '<div class="npersona">' + bits.join("　·　") + "</div>" : "";
  }

  function relParts(n, st) {
    var rv = st && st.relationships && st.relationships[n.name];
    if (!rv) return null;
    if (typeof rv === "object") return { tier: rv.tier || "", note: rv.note || rv.desc || "" };
    return { tier: "", note: String(rv) };
  }

  function relLine(n, st) {
    var rp = relParts(n, st);
    if (!rp || (!rp.tier && !rp.note)) return "";
    var tier = rp.tier ? '<b class="rtier">' + esc(rp.tier) + "</b> " : "";
    return '<div class="nrel">與你：' + tier + esc(rp.note) + "</div>";
  }

  function npcCard(n, p, st) {
    var lvl = (n.level == null || isNaN(n.level)) ? '<span class="veil">?</span>' : n.level;
    var hostile = isHostile(n);
    var right;
    if (hostile) {
      var mu = matchup(p, (typeof n.level === "number") ? n.level : null);
      right = '<span class="mu ' + mu[1] + '">' + esc(mu[0]) + "</span>";
    } else {
      right = '<span class="ally">隊友/中立</span>';
    }
    var recall = (n.lifecycle_status === "offstage" && matchesScope(n.home_scope, st))
      ? '<span class="recall">可召回</span>' : "";
    var skills = Object.keys(n.abilities || {}).map(function (k) {
      var a = parseAbility(k, n.abilities[k]);
      var lv = a.level != null ? '<span class="ablv">' + a.level + "級</span>" : "";
      return '<span class="skill">' + lv + esc(a.name) + " " + chips(a.domains) + "</span>";
    }).join("");
    var skillLine = skills ? '<div class="nskills">' + skills + "</div>" : "";
    var mem = (n.memory && n.memory.length) ? '<div class="nmem">' + esc(n.memory[n.memory.length - 1]) + "</div>" : "";
    return '<div class="npc ' + (n.lifecycle_status !== "active" ? "off" : "") + '">' +
      '<div class="nlvl"><b>' + lvl + "</b><span>級</span></div>" +
      '<div class="nbody">' + right + '<span class="nname">' + esc(n.name) + "</span>" +
      '<span class="nrank">' + esc(n.rank_label || "") + "</span>" + recall +
      "<div>" + chips(n.domains) + "</div>" + personaLine(n) + relLine(n, st) +
      skillLine + mem + "</div></div>";
  }

  // Per-story labels: the schema (app.js `characterSchema`) names the currency / 能力欄位 for
  // THIS world (demon_tide 命運點數·功法, urban_daily 現金·微光能力). Hardcoding 修真 wording here
  // made every other world's panel read wrong. Falls back to the 修真 defaults.
  function schemaField(key) {
    try {
      var s = (typeof characterSchema !== "undefined" && characterSchema) || {};
      var all = (s.fields || []).concat(s.lists || []);
      for (var i = 0; i < all.length; i++) { if (all[i] && all[i].key === key) return all[i]; }
    } catch (e) {}
    return null;
  }
  function labelOf(key, dflt) { var f = schemaField(key); return (f && f.label) ? f.label : dflt; }

  function render(st, npcs) {
    var p = protagLevel(st);
    var abilities = [];
    ["talents", "techniques"].forEach(function (key) {
      var m = st[key] || {};
      Object.keys(m).forEach(function (k) { abilities.push(parseAbility(k, m[k])); });
    });
    var minorSuffix = ((schemaField("minor_realm") || {}).suffix || "層").trim();
    var levelLabel = p != null
      ? p + "級（" + esc(st.major_realm) + (st.minor_realm != null ? st.minor_realm + minorSuffix : "") + "）"
      : "未知";
    var invMap = st.inventory || {};
    var invKeys = Object.keys(invMap);
    var inv = invKeys.length
      ? invKeys.map(function (k) {
          return '<span class="chip inv" title="' + esc(invMap[k] || k) + '">' + esc(k) +
            '<button class="inv-del" data-name="' + esc(k) + '" title="丟棄此道具">✕</button></span>';
        }).join("")
      : '<span class="meta">—</span>';
    var left = '<div>' +
      '<div class="lvlbadge">' + levelLabel + "</div>" +
      '<div class="meta" style="margin-top:8px">' + esc(st.current_phase || "") +
      " ｜ " + esc(labelOf("fate_points", "命運點")) + " " + (st.fate_points != null ? st.fate_points : "—") +
      (st.current_dungeon ? "<br>" + esc(st.current_dungeon) : "") +
      (st.current_status ? "<br>" + esc(st.current_status) : "") + "</div>" +
      '<div class="pp-sec">' + esc(labelOf("techniques", "功法")) + " / " + esc(labelOf("talents", "天賦")) + '（級·性質）</div>' +
      (abilities.length ? abilities.map(abilityRow).join("") : '<span class="meta">—</span>') +
      '<div class="pp-sec">關鍵物品</div>' + inv +
      "</div>";

    // Normalize lifecycle to a display bucket. Production stores {lifecycle_status:
    // "archived", archive_kind: offstage|terminal}; some records carry the raw GM
    // vocab ("offstage"/"terminal") as lifecycle_status. Without this, "archived"
    // NPCs matched NO group and silently vanished from the panel.
    function bucketOf(n) {
      var ls = n.lifecycle_status || "active";
      if (ls === "archived") return n.archive_kind === "terminal" ? "terminal" : "offstage";
      return ls;  // active / offstage / terminal (raw)
    }
    var groups = [["active", "在場"], ["offstage", "離場"], ["terminal", "已逝"]];
    var rhtml = groups.map(function (g) {
      var items = npcs.filter(function (n) { return bucketOf(n) === g[0]; });
      if (!items.length) return "";
      return '<div class="pp-sec">' + g[1] + "（" + items.length + "）</div>" +
        items.map(function (n) { return npcCard(n, p, st); }).join("");
    }).join("") || '<span class="meta">尚無已記錄單位</span>';
    var right = "<div>" + rhtml + "</div>";

    document.getElementById("power-panel-body").innerHTML = left + right;
    Array.prototype.forEach.call(
      document.querySelectorAll("#power-panel-body .inv-del"),
      function (b) {
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          discardItem(b.getAttribute("data-name"), b);
        });
      }
    );
  }

  // 丟棄一件物品欄道具（手動清掉 GM 漏移除的任務／戰利品）。非退款：不退命運點數。
  // 走 /api/v2/inventory/remove（deterministic + undo + snapshot-sync，扛得過 regen）。
  function discardItem(name, btn) {
    if (!name) return;
    // 無確認彈窗 — 丟棄可在標題列『本回合操作』chip 即時撤銷（送出下一則訊息前）。
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    fetch("/api/v2/inventory/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: curBranch(), name: name })
    }).then(function (r) { return r.json(); }).then(function () {
      try { if (window.refreshPendingOps) window.refreshPendingOps(); } catch (e) {}  // 更新待送操作 chip
      refresh();
    }).catch(function () { refresh(); });
  }

  function curBranch() {
    try { if (typeof currentBranchId !== "undefined" && currentBranchId) return currentBranchId; } catch (e) {}
    return "main";
  }

  function refresh() {
    var bid = curBranch();
    Promise.all([
      fetch("/api/status?branch_id=" + encodeURIComponent(bid)).then(function (r) { return r.json(); }),
      fetch("/api/npcs?branch_id=" + encodeURIComponent(bid) + "&include_archived=true").then(function (r) { return r.json(); })
    ]).then(function (res) {
      render(res[0] || {}, (res[1] && res[1].npcs) || []);
    }).catch(function () {
      document.getElementById("power-panel-body").innerHTML = '<span class="veil">載入失敗</span>';
    });
  }

  var pollTimer = null;
  function open() {
    document.getElementById("power-panel-modal").classList.remove("hidden");
    refresh();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refresh, 4000);
  }
  function close() {
    document.getElementById("power-panel-modal").classList.add("hidden");
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("power-panel-btn");
    var closeBtn = document.getElementById("power-panel-close");
    var modal = document.getElementById("power-panel-modal");
    if (btn) btn.addEventListener("click", open);
    if (closeBtn) closeBtn.addEventListener("click", close);
    if (modal) modal.addEventListener("click", function (e) { if (e.target === modal) close(); });
  });
})();
