/* 功法樹 (v2 skill tree) — frontend-only, twin of shop.js/power_panel.js.
   Reads /api/v2/tree (deterministic engine: nodes + lock state + prereq edges),
   unlocks via POST /api/v2/tree/unlock (requires-gated buy, NO LLM). Renders a
   tiered DAG: SVG edge layer + absolutely-positioned node cards.
   Self-contained: loaded after app.js; reads the global `currentBranchId`. */
(function () {
  "use strict";

  var NODE_W = 158, NODE_H = 150, ROW_H = 198, TOP_PAD = 26;
  var selectedVendor = null;  // which 宗門 tree is shown (null = engine default)

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function domClass(d) {
    if (/剋|斬|阻斷|穿透|破防/.test(d)) return "kill";
    if (/再生|多臂|體修/.test(d)) return "regen";
    if (/精神|詭異|因果|規則|法則|範圍|陽|光/.test(d)) return "hax";
    if (/感知|氣場|身法|敏捷|神識|鑑定/.test(d)) return "sense";
    return "";
  }
  function chips(ds) {
    return (ds || []).map(function (d) {
      return '<span class="chip ' + domClass(d) + '">' + esc(d) + "</span>";
    }).join("");
  }
  function curBranch() {
    try { if (typeof currentBranchId !== "undefined" && currentBranchId) return currentBranchId; } catch (e) {}
    return "main";
  }

  var STATE_ICON = { unlocked: "✓", available: "✦", broke: "⊘", locked: "🔒" };

  // assign (cx, top) to every node; tiers are rows, nodes spread within a row
  function layout(nodes) {
    var tiers = {};
    nodes.forEach(function (n) { (tiers[n.tier] = tiers[n.tier] || []).push(n); });
    var tierKeys = Object.keys(tiers).map(Number).sort(function (a, b) { return a - b; });
    var maxK = tierKeys.reduce(function (m, t) { return Math.max(m, tiers[t].length); }, 1);
    var VW = Math.max(maxK * 200, 600);
    var maxTier = tierKeys[tierKeys.length - 1];
    var pos = {};
    tierKeys.forEach(function (t) {
      var row = tiers[t], k = row.length;
      row.forEach(function (n, i) {
        pos[n.id] = { cx: VW * (i + 0.5) / k, top: t * ROW_H + TOP_PAD, isUlt: t === maxTier };
      });
    });
    return { pos: pos, VW: VW, VH: (maxTier + 1) * ROW_H + 30 };
  }

  function edgesSVG(nodes, pos, VW, VH) {
    var byId = {}; nodes.forEach(function (n) { byId[n.id] = n; });
    var paths = nodes.map(function (n) {
      var p = pos[n.id];
      return (n.prereq || []).map(function (pid) {
        var pp = pos[pid]; if (!pp) return "";
        var x1 = pp.cx, y1 = pp.top + NODE_H, x2 = p.cx, y2 = p.top;
        var my = (y1 + y2) / 2;
        var lit = n.owned || n.status === "available" || n.status === "broke";
        return '<path d="M' + x1 + ' ' + y1 + ' C' + x1 + ' ' + my + ' ' + x2 + ' ' + my + ' ' + x2 + ' ' + y2 +
          '" fill="none" stroke="' + (lit ? "#6b5526" : "#262a38") + '" stroke-width="' + (lit ? 2.5 : 1.5) + '"/>';
      }).join("");
    }).join("");
    return '<svg class="tree-edges" width="' + VW + '" height="' + VH + '" viewBox="0 0 ' + VW + ' ' + VH + '">' + paths + "</svg>";
  }

  function nodeCard(n, p) {
    var foot;
    if (n.status === "unlocked") {
      foot = '<span class="tn-mast">✓ ' + esc(n.mastery_label) + "</span>";
    } else if (n.status === "available") {
      var label = n.price ? ("解鎖 " + n.price) : "習得";
      foot = '<button class="tn-btn unlock" data-id="' + esc(n.id) + '" data-name="' + esc(n.name) + '">' + label + "</button>";
    } else if (n.status === "broke") {
      foot = '<span class="tn-btn broke">差 ' + n.shortfall + "</span>";
    } else {
      var first = (n.unmet && n.unmet[0]) || "未達條件";
      foot = '<span class="tn-lock" title="' + esc((n.unmet || []).join("\n")) + '">🔒 ' + esc(first) +
        ((n.unmet && n.unmet.length > 1) ? " …" : "") + "</span>";
    }
    var tip = n.name + (n.level ? "（" + n.level + "）" : "") +
      (n.desc ? "\n" + n.desc : "") +
      (n.unmet && n.unmet.length ? "\n— 需求：" + n.unmet.join("；") : "");
    var cls = "tnode " + n.status + (p.isUlt ? " ult" : "");
    return '<div class="' + cls + '" title="' + esc(tip) + '" style="left:' + (p.cx - NODE_W / 2) + "px;top:" + p.top + "px;width:" + NODE_W + 'px">' +
      '<div class="tn-top"><span class="tn-lvl">' + esc(n.level || "?") + "</span>" +
      '<span class="tn-ico">' + (STATE_ICON[n.status] || "") + "</span></div>" +
      '<div class="tn-name">' + esc(n.name) + "</div>" +
      '<div class="tn-doms">' + chips(n.domains) + "</div>" +
      (n.desc ? '<div class="tn-desc">' + esc(n.desc) + "</div>" : "") +
      '<div class="tn-foot">' + foot + "</div></div>";
  }

  function render(data) {
    var body = document.getElementById("tree-body");
    if (!body) return;
    if (!data || !data.ok || !data.nodes || !data.nodes.length) {
      body.innerHTML = '<span class="veil">此宗門尚無功法樹</span>';
      return;
    }
    selectedVendor = data.vendor;
    var L = layout(data.nodes);
    var tabs = (data.vendors && data.vendors.length > 1)
      ? '<div class="tree-tabs">' + data.vendors.map(function (v) {
          return '<button class="tree-tab' + (v.id === data.vendor ? " on" : "") +
            '" data-vendor="' + esc(v.id) + '">' + esc(v.name) + "</button>";
        }).join("") + "</div>"
      : "";
    var head = '<div class="tree-head"><span class="th-sect">' + esc(data.vendor_name) + " · 功法樹</span>" +
      '<span class="th-bal">' + esc(data.currency_label) + "：" + data.balance + "</span></div>";
    var canvas = '<div class="tree-canvas" style="width:' + L.VW + "px;height:" + L.VH + 'px">' +
      edgesSVG(data.nodes, L.pos, L.VW, L.VH) +
      data.nodes.map(function (n) { return nodeCard(n, L.pos[n.id]); }).join("") + "</div>";
    body.innerHTML = tabs + head + '<div class="tree-scroll">' + canvas + "</div>";
    Array.prototype.forEach.call(body.querySelectorAll(".tree-tab"), function (t) {
      t.addEventListener("click", function () {
        selectedVendor = t.getAttribute("data-vendor");
        refresh();
      });
    });
    Array.prototype.forEach.call(body.querySelectorAll(".tn-btn.unlock"), function (b) {
      b.addEventListener("click", function () {
        unlock(b.getAttribute("data-id"), b.getAttribute("data-name"), b);
      });
    });
  }

  function refresh() {
    try { if (window.refreshPendingOps) window.refreshPendingOps(); } catch (e) {}  // 解鎖/閉關 → 更新待送操作 chip
    var url = "/api/v2/tree?branch_id=" + encodeURIComponent(curBranch());
    if (selectedVendor) url += "&vendor=" + encodeURIComponent(selectedVendor);
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        var body = document.getElementById("tree-body");
        if (body) body.innerHTML = '<span class="veil">載入失敗</span>';
      });
  }

  function flash(msg) {
    var el = document.getElementById("tree-flash");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("show");
    setTimeout(function () { el.classList.remove("show"); }, 2600);
  }

  function unlock(id, name, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "解鎖中…"; }
    fetch("/api/v2/tree/unlock", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: curBranch(), vendor: selectedVendor || "sect_exchange", item_id: id })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) {
        flash("習得：" + name + "（-" + res.spent + " " + res.currency_label + " → 餘 " + res.new_balance + "）");
      } else if (res && res.reason === "locked") {
        flash("尚未解鎖：" + ((res.unmet || []).join("，")));
      } else if (res && res.reason === "insufficient") {
        flash("點數不足，差 " + res.shortfall);
      } else {
        flash("解鎖失敗：" + ((res && res.reason) || (res && res.error) || "未知"));
      }
      refresh();
    }).catch(function () { flash("解鎖失敗：網路錯誤"); refresh(); });
  }

  var pollTimer = null;
  function open() {
    var modal = document.getElementById("tree-modal");
    if (!modal) return;
    modal.classList.remove("hidden");
    refresh();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refresh, 5000);
  }
  function close() {
    var modal = document.getElementById("tree-modal");
    if (modal) modal.classList.add("hidden");
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("tree-btn");
    var closeBtn = document.getElementById("tree-close");
    var modal = document.getElementById("tree-modal");
    if (btn) btn.addEventListener("click", open);
    if (closeBtn) closeBtn.addEventListener("click", close);
    if (modal) modal.addEventListener("click", function (e) { if (e.target === modal) close(); });
  });
})();
