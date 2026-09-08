/* 行動選單 (v2 deterministic action menu) — frontend-only, twin of shop.js/tree.js.
   Reads /api/v2/actions (engine: menu actions + costs + time/deadline status), runs
   via POST /api/v2/actions/do. **NO LLM, no GM turn** — the 確定性路徑 of the v2
   architecture (doc/architecture_v2.md). Spending an action advances the world clock
   and may fire deadlines (魔潮); the narrator acknowledges it next turn via op-log.
   Self-contained: loaded after app.js; reads the global `currentBranchId`. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function curBranch() {
    try { if (typeof currentBranchId !== "undefined" && currentBranchId) return currentBranchId; } catch (e) {}
    return "main";
  }

  function gainStr(g, curLabel) {
    var parts = Object.keys(g || {}).map(function (k) {
      return "+" + g[k] + " " + (k === "fate_points" ? curLabel : k);
    });
    return parts.join("、");
  }

  function statusBar(d) {
    // 距 deadline 不在這裡顯示——它是「事件」，統一在 📋 事件/任務面板（避免重複）
    return '<div class="ac-status">第 <b>' + d.now + "</b> " + esc(d.unit) +
      '　·　' + esc(d.currency_label) + "：<b>" + d.balance + "</b></div>";
  }

  function costStr(a, d) {
    return (a.cost_days != null)
      ? '<span class="ac-cost">' + a.cost_days + esc(d.unit) + "</span>" : "";
  }

  function paramFields(a) {
    return (a.params || []).map(function (p) {
      var opts = (p.options || []).map(function (o) {
        var v = (o && typeof o === "object") ? o.value : o;
        var l = (o && typeof o === "object") ? (o.label || o.value) : o;
        var hint = (o && typeof o === "object" && o.hint) ? "（" + esc(o.hint) + "）" : "";
        var sel = (p.default != null && p.default === v) ? " selected" : "";
        return '<option value="' + esc(v) + '"' + sel + ">" + esc(l) + hint + "</option>";
      }).join("");
      // allow_other: a 「其他（自己輸入）」 option that reveals a free-text box — the pool is a
      // suggestion list, not a limit, so the player can enter ANY real IP world not listed.
      var other = p.allow_other
        ? '<option value="__other__">其他（自己輸入）</option>'
        : "";
      var input = p.allow_other
        ? '<input class="ac-other" data-pname="' + esc(p.name) + '" style="display:none" placeholder="輸入作品名，如：海賊王 / 進擊的巨人">'
        : "";
      return '<label class="ac-pl">' + esc(p.label || p.name) + "</label>" +
        '<select class="ac-sel" data-pname="' + esc(p.name) + '">' +
        (opts || '<option value="">（無可選）</option>') + other + "</select>" + input;
    }).join("");
  }

  function actionCard(a, d) {
    var cost = costStr(a, d);
    var head = '<span class="ac-top"><span class="ac-name">' + esc(a.icon || "") + " " + esc(a.label) +
      "</span>" + cost + "</span>" + (a.desc ? '<span class="ac-desc">' + esc(a.desc) + "</span>" : "");
    // structural / picker actions: inline param selects + a run button (can't nest <select> in <button>)
    if (a.params && a.params.length) {
      return '<div class="ac-item structural" data-id="' + esc(a.id) + '" data-name="' + esc(a.label) + '">' +
        head + '<div class="ac-params">' + paramFields(a) + "</div>" +
        '<button class="ac-run" type="button">執行</button></div>';
    }
    var gain = (a.gain && Object.keys(a.gain).length)
      ? '<span class="ac-gain">' + esc(gainStr(a.gain, d.currency_label)) + "</span>" : "";
    var cls = "ac-item" + (a.kind === "cultivate" ? " cultivate" : "") +
      (a.kind === "structural" ? " structural" : "") + (a.available ? "" : " dim");
    return '<button class="' + cls + '" data-id="' + esc(a.id) + '" data-name="' + esc(a.label) + '"' +
      (a.available ? "" : " disabled") + ">" + head +
      (gain ? '<span class="ac-foot">' + gain + "</span>" : "") + "</button>";
  }

  function render(d) {
    var body = document.getElementById("actions-body");
    if (!body) return;
    if (!d || !d.ok || !d.actions || !d.actions.length) {
      body.innerHTML = statusBar(d || {}) + '<span class="veil">此處暫無可選行動</span>';
      return;
    }
    var cats = {};
    d.actions.forEach(function (a) { (cats[a.category] = cats[a.category] || []).push(a); });
    var html = statusBar(d);
    Object.keys(cats).forEach(function (c) {
      html += '<div class="ac-cat">' + esc(c) + '</div><div class="ac-grid">'
        + cats[c].map(function (a) { return actionCard(a, d); }).join("") + "</div>";
    });
    body.innerHTML = html;
    // simple actions: the whole button runs immediately
    Array.prototype.forEach.call(body.querySelectorAll("button.ac-item:not([disabled])"), function (b) {
      b.addEventListener("click", function () { run(b.getAttribute("data-id"), b.getAttribute("data-name"), b); });
    });
    // allow_other: reveal the free-text box when 「其他」 is selected
    Array.prototype.forEach.call(body.querySelectorAll(".ac-sel"), function (s) {
      s.addEventListener("change", function () {
        var inp = s.parentNode.querySelector('.ac-other[data-pname="' + s.getAttribute("data-pname") + '"]');
        if (inp) { inp.style.display = (s.value === "__other__") ? "block" : "none"; if (s.value === "__other__") inp.focus(); }
      });
    });
    // picker actions: gather the selects → run with args (free-text wins when 「其他」 chosen)
    Array.prototype.forEach.call(body.querySelectorAll(".ac-run"), function (rb) {
      rb.addEventListener("click", function () {
        var card = rb.closest(".ac-item");
        if (!card) return;
        var args = {};
        Array.prototype.forEach.call(card.querySelectorAll(".ac-sel"), function (s) {
          var name = s.getAttribute("data-pname");
          var val = s.value;
          if (val === "__other__") {
            var inp = card.querySelector('.ac-other[data-pname="' + name + '"]');
            val = (inp && inp.value.trim()) || "";
          }
          args[name] = val;
        });
        run(card.getAttribute("data-id"), card.getAttribute("data-name"), rb, args);
      });
    });
  }

  function refresh() {
    try { if (window.refreshWorldDay) window.refreshWorldDay(); } catch (e) {}  // 動作會花時間→同步 header 第N天
    try { if (window.refreshPendingOps) window.refreshPendingOps(); } catch (e) {}  // ⚡行動 → 更新待送操作 chip
    fetch("/api/v2/actions?branch_id=" + encodeURIComponent(curBranch()))
      .then(function (r) { return r.json(); }).then(render)
      .catch(function () {
        var body = document.getElementById("actions-body");
        if (body) body.innerHTML = '<span class="veil">載入失敗</span>';
      });
  }

  function flash(msg, warn) {
    var el = document.getElementById("actions-flash");
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle("warn", !!warn);
    el.classList.add("show");
    setTimeout(function () { el.classList.remove("show"); }, warn ? 5200 : 3000);
  }

  function run(id, name, btn, args) {
    if (btn) { btn.disabled = true; }
    fetch("/api/v2/actions/do", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: curBranch(), action: id, args: args || {} })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) {
        var fired = (res.fired && res.fired.length) ? res.fired : null;
        if (res.tool === "enter_dungeon") {           // 進副本
          flash("🌀 進入副本：" + esc(res.current_dungeon || "") + (res.note ? "（" + esc(res.note) + "）" : ""));
        } else if (res.tool === "exit_dungeon") {     // 通關離開
          var rw = res.rewards || {};
          var bits = [];
          if (rw.fate_points) bits.push("＋" + rw.fate_points + " 命運點數");
          if (rw.items && rw.items.length) bits.push(rw.items.join("、"));
          flash("🏁 通關離開：" + esc(res.left || "") + (bits.length ? "　獲得 " + esc(bits.join("、")) : ""));
        } else if (res.mastery_label !== undefined) { // cultivate result
          flash("閉關 " + (res.spent || 0) + " 天 → " + name + "：" + res.mastery_label +
            (res.promoted ? "（突破！）" : "") + "　第 " + res.now + " 天");
        } else {
          var g = (res.gained && Object.keys(res.gained).length) ? "，" + gainStr(res.gained, res.currency_label || "") : "";
          flash(name + "：耗 " + res.spent_days + " 天" + g + "　→ 第 " + res.now + " 天");
        }
        if (fired) flash("⚠️ " + fired.map(function (e) { return e.label + "：" + (e.on_fire || "已觸發"); }).join("；"), true);
      } else {
        flash("行動失敗：" + ((res && (res.error || res.note || res.reason)) || "未知"));
      }
      refresh();
    }).catch(function () { flash("行動失敗：網路錯誤"); refresh(); });
  }

  var pollTimer = null;
  function open() {
    var m = document.getElementById("actions-modal");
    if (!m) return;
    m.classList.remove("hidden");
    refresh();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refresh, 5000);
  }
  function close() {
    var m = document.getElementById("actions-modal");
    if (m) m.classList.add("hidden");
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("actions-btn");
    var closeBtn = document.getElementById("actions-close");
    var modal = document.getElementById("actions-modal");
    if (btn) btn.addEventListener("click", open);
    if (closeBtn) closeBtn.addEventListener("click", close);
    if (modal) modal.addEventListener("click", function (e) { if (e.target === modal) close(); });
  });
})();
