/* 距 deadline 倒數 (v2 time economy) — frontend-only header badge.
   Polls /api/v2/time and shows the nearest world deadline (距魔潮 N 天), turning
   urgent (red) inside the warn window. Self-contained: reads global currentBranchId. */
(function () {
  "use strict";

  function curBranch() {
    try { if (typeof currentBranchId !== "undefined" && currentBranchId) return currentBranchId; } catch (e) {}
    return "main";
  }

  function render(d) {
    var el = document.getElementById("deadline-display");
    if (!el) return;
    if (!d || !d.ok || !(d.upcoming && d.upcoming.length)) {
      el.textContent = ""; el.className = ""; return;
    }
    var u = d.upcoming[0];
    var unit = d.unit || "天";
    el.textContent = "距" + u.label + " " + Math.max(0, Math.round(u.in_n)) + unit + (u.warn ? " ⚠" : "");
    el.className = u.warn ? "urgent" : "";
    el.title = "第 " + (d.now || 0) + " " + unit;
  }

  function refresh() {
    fetch("/api/v2/time?branch_id=" + encodeURIComponent(curBranch()))
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {});
  }

  document.addEventListener("DOMContentLoaded", function () {
    refresh();
    setInterval(refresh, 8000);
  });
})();
