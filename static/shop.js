/* 商城／兌換 (v2 catalog shop) — frontend-only, twin of power_panel.js.
   Reads /api/v2/catalog (deterministic engine: vendors + items + affordability),
   buys via POST /api/v2/catalog/buy. NO LLM, no GM turn — instant transaction.
   Self-contained: loaded after app.js; reads the global `currentBranchId`. */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function domClass(d) {
    if (/剋|斬|阻斷/.test(d)) return "kill";
    if (/再生|多臂/.test(d)) return "regen";
    if (/精神|詭異|因果|規則|法則/.test(d)) return "hax";
    if (/感知|氣場|嗅覺|觀氣|神識|鑑定/.test(d)) return "sense";
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

  function itemRow(v, it) {
    var lvl = it.level ? '<span class="sablv">' + esc(it.level) + "</span>" : "";
    var owned = it.owned ? '<span class="sown">已持有</span>' : "";
    var req = it.requirement ? '<span class="sreq">門檻：' + esc(it.requirement) + "</span>" : "";
    var btn;
    if (it.affordable) {
      btn = '<button class="sbtn buy" data-vendor="' + esc(v.id) + '" data-id="' + esc(it.id) +
            '" data-name="' + esc(it.name) + '">兌換 ' + it.price + "</button>";
    } else {
      btn = '<span class="sbtn lock">深不可測 · 差 ' + it.shortfall + "</span>";
    }
    var refundBtn = it.refundable
      ? '<button class="sbtn refund" data-vendor="' + esc(v.id) + '" data-id="' + esc(it.id) +
        '" data-name="' + esc(it.name) + '">退貨</button>'
      : "";
    return '<div class="sitem' + (it.affordable ? "" : " dim") + '">' +
      '<div class="sitop"><span class="sname">' + lvl + esc(it.name) + owned + "</span>" +
      '<span class="sprice">' + it.price + "</span></div>" +
      (it.desc ? '<div class="sdesc">' + esc(it.desc) + "</div>" : "") +
      '<div class="smeta">' + chips(it.domains) + " " + req + "</div>" +
      '<div class="sact">' + btn + " " + refundBtn + "</div></div>";
  }

  function vendorBlock(v) {
    var items = (v.items && v.items.length)
      ? v.items.map(function (it) { return itemRow(v, it); }).join("")
      : '<span class="veil">—</span>';
    return '<div class="svendor">' +
      '<div class="svhead">' + esc(v.name) +
      '<span class="svbal">' + esc(v.currency_label) + "：" + v.balance + "</span></div>" +
      '<div class="sitems">' + items + "</div></div>";
  }

  function render(data) {
    var body = document.getElementById("shop-body");
    if (!body) return;
    if (!data || !data.ok || !data.vendors || !data.vendors.length) {
      body.innerHTML = '<span class="veil">此世界尚無兌換目錄</span>';
      return;
    }
    body.innerHTML = data.vendors.map(vendorBlock).join("");
    Array.prototype.forEach.call(body.querySelectorAll(".sbtn.buy"), function (b) {
      b.addEventListener("click", function () {
        buy(b.getAttribute("data-vendor"), b.getAttribute("data-id"), b.getAttribute("data-name"), b);
      });
    });
    Array.prototype.forEach.call(body.querySelectorAll(".sbtn.refund"), function (b) {
      b.addEventListener("click", function () {
        refund(b.getAttribute("data-vendor"), b.getAttribute("data-id"), b.getAttribute("data-name"), b);
      });
    });
  }

  function refresh() {
    try { if (window.refreshPendingOps) window.refreshPendingOps(); } catch (e) {}  // 買/退 → 更新待送操作 chip
    fetch("/api/v2/catalog?branch_id=" + encodeURIComponent(curBranch()))
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {
        var body = document.getElementById("shop-body");
        if (body) body.innerHTML = '<span class="veil">載入失敗</span>';
      });
  }

  function flash(msg) {
    var el = document.getElementById("shop-flash");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("show");
    setTimeout(function () { el.classList.remove("show"); }, 2600);
  }

  function buy(vendor, id, name, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "兌換中…"; }
    fetch("/api/v2/catalog/buy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: curBranch(), vendor: vendor, item_id: id })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) {
        flash("已兌換：" + name + "（-" + res.spent + " " + res.currency_label + " → 餘 " + res.new_balance + "）");
      } else if (res && res.reason === "insufficient") {
        flash("餘額不足，差 " + res.shortfall + " " + (res.currency_label || ""));
      } else {
        flash("兌換失敗：" + ((res && res.reason) || (res && res.error) || "未知"));
      }
      refresh();
    }).catch(function () { flash("兌換失敗：網路錯誤"); refresh(); });
  }

  function refund(vendor, id, name, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "退貨中…"; }
    fetch("/api/v2/catalog/refund", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ branch_id: curBranch(), vendor: vendor, item_id: id })
    }).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) {
        flash("已退貨：" + name + "（+" + res.amount + " " + res.currency_label + " → 餘 " + res.new_balance + "）");
      } else if (res && res.reason === "no_purchase") {
        flash("退貨失敗：查無此筆購買紀錄");
      } else {
        flash("退貨失敗：" + ((res && res.reason) || (res && res.error) || "未知"));
      }
      refresh();
    }).catch(function () { flash("退貨失敗：網路錯誤"); refresh(); });
  }

  var pollTimer = null;
  function open() {
    var modal = document.getElementById("shop-modal");
    if (!modal) return;
    modal.classList.remove("hidden");
    refresh();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refresh, 5000);
  }
  function close() {
    var modal = document.getElementById("shop-modal");
    if (modal) modal.classList.add("hidden");
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("shop-btn");
    var closeBtn = document.getElementById("shop-close");
    var modal = document.getElementById("shop-modal");
    if (btn) btn.addEventListener("click", open);
    if (closeBtn) closeBtn.addEventListener("click", close);
    if (modal) modal.addEventListener("click", function (e) { if (e.target === modal) close(); });
  });
})();
