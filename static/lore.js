/* ============================================================
   Lore Console — Frontend Logic
   ============================================================ */

(function () {
  "use strict";

  // ------------------------------------------------------------------
  // State
  // ------------------------------------------------------------------
  let allEntries = [];
  let categories = [];
  let collapsedCats = new Set(); // preserve collapsed state across re-renders
  let allCollapsed = true; // default: everything collapsed
  let selectedTopics = new Set(); // allow multiple expanded entries
  let checkedTopics = new Set(); // for batch delete
  let chatMessages = []; // {role, content} history for LLM
  let isStreaming = false;
  let editingEntry = null; // null = create, {topic, ...} = edit
  let streamAbortController = null;
  let activeBranchId = ""; // current branch for branch lore
  let isPromoting = false; // promotion review in progress

  // ------------------------------------------------------------------
  // DOM refs
  // ------------------------------------------------------------------
  const $loreList = document.getElementById("lore-list");
  const $searchInput = document.getElementById("lore-search");
  const $addBtn = document.getElementById("lore-add-btn");
  const $chatMessages = document.getElementById("chat-messages");
  const $chatInput = document.getElementById("chat-input");
  const $chatSendBtn = document.getElementById("chat-send-btn");
  const $modal = document.getElementById("edit-modal");
  const $modalTitle = document.getElementById("modal-title");
  const $modalCategory = document.getElementById("modal-category");
  const $modalSubcategory = document.getElementById("modal-subcategory");
  const $modalTopic = document.getElementById("modal-topic");
  const $modalContent = document.getElementById("modal-content");
  const $modalSave = document.getElementById("modal-save");
  const $modalCancel = document.getElementById("modal-cancel");
  const $modalClose = document.getElementById("modal-close");
  const $modalDelete = document.getElementById("modal-delete");
  const $storyName = document.getElementById("story-name");
  const $chatStopBtn = document.getElementById("chat-stop-btn");
  const $batchBar = document.getElementById("batch-bar");
  const $batchCount = document.getElementById("batch-count");
  const $batchDeleteBtn = document.getElementById("batch-delete-btn");
  const $batchClearBtn = document.getElementById("batch-clear-btn");

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------
  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function truncate(str, len) {
    if (!str) return "";
    return str.length > len ? str.slice(0, len) + "..." : str;
  }

  /** Render markdown to sanitized HTML */
  function renderMarkdown(text) {
    if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined" && text) {
      return DOMPurify.sanitize(marked.parse(text, { breaks: true, gfm: true }));
    }
    return escapeHtml(text || "");
  }

  /** Update batch action bar visibility */
  function updateBatchBar() {
    if (checkedTopics.size > 0) {
      $batchBar.classList.remove("hidden");
      $batchCount.textContent = `已選 ${checkedTopics.size} 項`;
    } else {
      $batchBar.classList.add("hidden");
    }
  }

  // ------------------------------------------------------------------
  // API calls
  // ------------------------------------------------------------------
  async function fetchLoreAll() {
    const res = await fetch("/api/lore/all");
    const data = await res.json();
    if (data.ok) {
      allEntries = data.entries || [];
      categories = data.categories || [];
      activeBranchId = data.branch_id || "";
    }
    return data;
  }

  async function createEntry(entry) {
    const res = await fetch("/api/lore/entry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(entry),
    });
    return res.json();
  }

  async function updateEntry(topic, subcategory, updates) {
    const res = await fetch("/api/lore/entry", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, subcategory: subcategory || "", ...updates }),
    });
    return res.json();
  }

  async function deleteEntry(topic, subcategory) {
    const res = await fetch("/api/lore/entry", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic, subcategory: subcategory || "" }),
    });
    return res.json();
  }

  async function applyProposals(proposals) {
    const res = await fetch("/api/lore/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proposals }),
    });
    return res.json();
  }

  // ------------------------------------------------------------------
  // Lore list rendering
  // ------------------------------------------------------------------

  /** Parse topic prefix: "生化危機：浣熊市" → {prefix:"生化危機", suffix:"浣熊市"} */
  function parseTopicPrefix(topic) {
    for (const d of [" — ", "：", ":"]) {
      const idx = topic.indexOf(d);
      if (idx > 0) {
        return { prefix: topic.slice(0, idx).trim(), suffix: topic.slice(idx + d.length).trim() };
      }
    }
    return { prefix: null, suffix: topic };
  }

  /** Build sub-groups for a category's entries. Returns [{type:"entry",e}|{type:"subgroup",prefix,entries}] */
  function buildSubGroups(entries) {
    // Count prefix occurrences
    const prefixCount = new Map();
    for (const e of entries) {
      const { prefix } = parseTopicPrefix(e.topic);
      if (prefix) prefixCount.set(prefix, (prefixCount.get(prefix) || 0) + 1);
    }

    // Build ordered list: sub-groups (2+ entries with same prefix) and standalone entries
    const result = [];
    const usedPrefixes = new Set();

    for (const e of entries) {
      const { prefix } = parseTopicPrefix(e.topic);
      if (prefix && prefixCount.get(prefix) >= 2) {
        if (!usedPrefixes.has(prefix)) {
          usedPrefixes.add(prefix);
          const grouped = entries.filter((x) => parseTopicPrefix(x.topic).prefix === prefix);
          grouped.sort((a, b) => a.topic.localeCompare(b.topic, "zh-Hant"));
          result.push({ type: "subgroup", prefix, entries: grouped });
        }
      } else {
        result.push({ type: "entry", e });
      }
    }
    // Sort: sub-groups first, then standalone entries; alpha within each group
    result.sort((a, b) => {
      if (a.type !== b.type) return a.type === "subgroup" ? -1 : 1;
      const nameA = a.type === "subgroup" ? a.prefix : a.e.topic;
      const nameB = b.type === "subgroup" ? b.prefix : b.e.topic;
      return nameA.localeCompare(nameB, "zh-Hant");
    });
    return result;
  }

  function formatSourceLine(source) {
    if (!source) return "";
    const branch = source.branch_id || "?";
    const idx = source.msg_index != null ? `#${source.msg_index}` : "";
    const date = source.timestamp ? source.timestamp.slice(0, 10) : "";
    const parts = [`分支 ${branch}`];
    if (idx) parts.push(`訊息 ${idx}`);
    if (date) parts.push(date);
    return parts.join(" · ");
  }

  function buildSourceUrl(source) {
    if (!source || !source.branch_id) return null;
    let url = `/?branch=${encodeURIComponent(source.branch_id)}`;
    if (source.msg_index != null) url += `&msg=${source.msg_index}`;
    return url;
  }

  function renderEntryHtml(e, displayName) {
    const label = displayName || e.topic;
    const sel = selectedTopics.has(e.topic) ? " selected" : "";
    const checked = checkedTopics.has(e.topic) ? " checked" : "";
    const layerClass = e.layer === "branch" ? " branch-entry" : "";
    let html = `<div class="lore-entry${sel}${layerClass}" data-topic="${escapeHtml(e.topic)}" data-layer="${e.layer || 'base'}">`;
    html += `<span class="lore-entry-topic">${escapeHtml(label)}</span>`;
    if (e.layer === "branch") {
      html += `<span class="lore-layer-badge branch">分支</span>`;
    }
    html += `<input type="checkbox" class="lore-entry-check" data-topic="${escapeHtml(e.topic)}" aria-label="選取 ${escapeHtml(label)}"${checked}>`;
    html += `<button class="lore-entry-edit" data-topic="${escapeHtml(e.topic)}" title="編輯">&#x270E;</button>`;
    html += `</div>`;
    const previewText = sel ? e.content || "" : truncate(e.content, 120);
    html += `<div class="lore-entry-preview">${escapeHtml(previewText)}`;
    if (e.source) {
      const srcUrl = buildSourceUrl(e.source);
      const srcText = escapeHtml(formatSourceLine(e.source));
      if (srcUrl) {
        html += `<div class="lore-entry-source"><a href="${escapeHtml(srcUrl)}" target="_blank" title="跳轉到原始對話">來源：${srcText}</a></div>`;
      } else {
        html += `<div class="lore-entry-source">來源：${srcText}</div>`;
      }
    }
    html += `</div>`;
    return html;
  }

  function renderCategoryGroup(entries, html) {
    // Group by category → subcategory
    const groups = new Map();
    for (const e of entries) {
      const cat = e.category || "其他";
      if (!groups.has(cat)) groups.set(cat, new Map());
      const subMap = groups.get(cat);
      const sub = e.subcategory || "";
      if (!subMap.has(sub)) subMap.set(sub, []);
      subMap.get(sub).push(e);
    }
    const orderedCats = [...groups.keys()].sort((a, b) => a.localeCompare(b, "zh-Hant"));
    for (const cat of orderedCats) {
      const subMap = groups.get(cat);
      const totalCount = [...subMap.values()].reduce((s, arr) => s + arr.length, 0);
      const isException = collapsedCats.has(cat);
      const isOpen = allCollapsed ? isException : !isException;
      const collapsed = isOpen ? "" : " collapsed";
      html += `<div class="lore-category${collapsed}">`;
      html += `<div class="lore-cat-header" data-cat="${escapeHtml(cat)}">`;
      html += `<span class="lore-cat-arrow">&#x25BC;</span> `;
      html += `${escapeHtml(cat)}`;
      html += `<span class="lore-cat-count">(${totalCount})</span>`;
      html += `</div>`;
      html += `<div class="lore-cat-entries">`;

      const orderedSubs = [...subMap.keys()].sort((a, b) => a.localeCompare(b, "zh-Hant"));
      for (const sub of orderedSubs) {
        const subEntries = subMap.get(sub);
        if (sub) {
          // Render subcategory as a collapsible subgroup
          const subKey = cat + "::" + sub;
          const subException = collapsedCats.has(subKey);
          const subOpen = allCollapsed ? subException : !subException;
          const subCollapsed = subOpen ? "" : " collapsed";
          html += `<div class="lore-subgroup${subCollapsed}">`;
          html += `<div class="lore-subgroup-header" data-subkey="${escapeHtml(subKey)}">`;
          html += `<span class="lore-cat-arrow">&#x25BC;</span> `;
          html += `${escapeHtml(sub)}`;
          html += `<span class="lore-cat-count">(${subEntries.length})</span>`;
          html += `</div>`;
          html += `<div class="lore-subgroup-entries">`;
          subEntries.sort((a, b) => a.topic.localeCompare(b.topic, "zh-Hant"));
          for (const e of subEntries) html += renderEntryHtml(e);
          html += `</div></div>`;
        } else {
          // No subcategory — render with topic-prefix subgroups as before
          subEntries.sort((a, b) => a.topic.localeCompare(b.topic, "zh-Hant"));
          const items = buildSubGroups(subEntries);
          for (const item of items) {
            if (item.type === "subgroup") {
              const subKey = cat + "/" + item.prefix;
              const subException = collapsedCats.has(subKey);
              const subOpen = allCollapsed ? subException : !subException;
              const subCollapsed = subOpen ? "" : " collapsed";
              html += `<div class="lore-subgroup${subCollapsed}">`;
              html += `<div class="lore-subgroup-header" data-subkey="${escapeHtml(subKey)}">`;
              html += `<span class="lore-cat-arrow">&#x25BC;</span> `;
              html += `${escapeHtml(item.prefix)}`;
              html += `<span class="lore-cat-count">(${item.entries.length})</span>`;
              html += `</div>`;
              html += `<div class="lore-subgroup-entries">`;
              for (const e of item.entries) {
                const { suffix } = parseTopicPrefix(e.topic);
                html += renderEntryHtml(e, suffix);
              }
              html += `</div></div>`;
            } else {
              html += renderEntryHtml(item.e);
            }
          }
        }
      }
      html += `</div></div>`;
    }
    return html;
  }

  function renderLoreList(filter) {
    const q = (filter || "").toLowerCase();
    const filtered = q
      ? allEntries.filter(
          (e) =>
            e.topic.toLowerCase().includes(q) ||
            (e.content || "").toLowerCase().includes(q) ||
            e.category.toLowerCase().includes(q) ||
            (e.subcategory || "").toLowerCase().includes(q)
        )
      : allEntries;

    // Separate base and branch entries
    const baseEntries = filtered.filter((e) => e.layer !== "branch");
    const branchEntries = filtered.filter((e) => e.layer === "branch");

    let html = "";
    html = renderCategoryGroup(baseEntries, html);

    // Branch lore section
    if (branchEntries.length > 0) {
      const branchKey = "__branch_section__";
      const branchException = collapsedCats.has(branchKey);
      const branchOpen = allCollapsed ? branchException : !branchException;
      const branchCollapsed = branchOpen ? "" : " collapsed";
      html += `<div class="lore-branch-section${branchCollapsed}">`;
      html += `<div class="lore-branch-header" data-cat="${branchKey}">`;
      html += `<span class="lore-cat-arrow">&#x25BC;</span> `;
      html += `分支知識`;
      html += `<span class="lore-cat-count">(${branchEntries.length})</span>`;
      html += `<button class="lore-promote-btn" title="審核並提升為永久設定">審核提升</button>`;
      html += `</div>`;
      html += `<div class="lore-branch-entries">`;
      html = renderCategoryGroup(branchEntries, html);
      html += `</div></div>`;
    }

    if (!html) {
      html = `<div style="padding:20px;color:var(--text-dim);text-align:center;">沒有符合的設定</div>`;
    }

    $loreList.innerHTML = html;

    // Bind click handlers — categories (and branch section header)
    $loreList.querySelectorAll(".lore-cat-header, .lore-branch-header").forEach((el) => {
      el.addEventListener("click", (ev) => {
        if (ev.target.closest(".lore-promote-btn")) return;
        const cat = el.dataset.cat;
        el.parentElement.classList.toggle("collapsed");
        if (collapsedCats.has(cat)) collapsedCats.delete(cat);
        else collapsedCats.add(cat);
      });
    });

    // Bind promote button
    $loreList.querySelectorAll(".lore-promote-btn").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        reviewAndPromote();
      });
    });

    // Bind click handlers — sub-groups
    $loreList.querySelectorAll(".lore-subgroup-header").forEach((el) => {
      el.addEventListener("click", () => {
        const key = el.dataset.subkey;
        el.parentElement.classList.toggle("collapsed");
        if (collapsedCats.has(key)) collapsedCats.delete(key);
        else collapsedCats.add(key);
      });
    });

    // Bind click handlers — entries (multiple selection)
    $loreList.querySelectorAll(".lore-entry").forEach((el) => {
      el.addEventListener("click", (ev) => {
        if (ev.target.closest(".lore-entry-edit")) return;
        if (ev.target.closest(".lore-entry-check")) return;
        const topic = el.dataset.topic;
        if (selectedTopics.has(topic)) selectedTopics.delete(topic);
        else selectedTopics.add(topic);
        renderLoreList($searchInput.value);
      });
    });

    // Bind checkbox handlers
    $loreList.querySelectorAll(".lore-entry-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        const topic = cb.dataset.topic;
        if (cb.checked) checkedTopics.add(topic);
        else checkedTopics.delete(topic);
        updateBatchBar();
      });
    });

    $loreList.querySelectorAll(".lore-entry-edit").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const topic = btn.dataset.topic;
        const entryEl = btn.closest(".lore-entry");
        const layer = entryEl ? entryEl.dataset.layer : "base";
        const entry = allEntries.find((e) => e.topic === topic && (e.layer || "base") === layer);
        if (entry) openModal(entry);
      });
    });
  }

  // ------------------------------------------------------------------
  // Modal
  // ------------------------------------------------------------------
  function populateCategoryDropdown(selectedCat) {
    const cats = [...new Set([...categories, "其他"])];
    $modalCategory.innerHTML = cats
      .map(
        (c) =>
          `<option value="${escapeHtml(c)}"${c === selectedCat ? " selected" : ""}>${escapeHtml(c)}</option>`
      )
      .join("");
  }

  function openModal(entry) {
    editingEntry = entry || null;
    // Remove any previous source info block
    const oldSource = document.getElementById("modal-source-info");
    if (oldSource) oldSource.remove();

    if (entry) {
      $modalTitle.textContent = entry.layer === "branch" ? "編輯分支設定" : "編輯設定";
      $modalDelete.style.display = "";
      populateCategoryDropdown(entry.category);
      $modalSubcategory.value = entry.subcategory || "";
      $modalTopic.value = entry.topic;
      $modalContent.value = entry.content || "";

      // Show read-only source provenance
      if (entry.source) {
        const srcDiv = document.createElement("div");
        srcDiv.id = "modal-source-info";
        srcDiv.className = "modal-source-info";
        const srcUrl = buildSourceUrl(entry.source);
        let srcHtml = `<label>來源</label>`;
        srcHtml += `<div class="modal-source-detail">`;
        if (srcUrl) {
          srcHtml += `<a href="${escapeHtml(srcUrl)}" target="_blank" class="modal-source-link" title="跳轉到原始對話">${escapeHtml(formatSourceLine(entry.source))} ↗</a>`;
        } else {
          srcHtml += `<span>${escapeHtml(formatSourceLine(entry.source))}</span>`;
        }
        if (entry.source.excerpt) {
          srcHtml += `<div class="modal-source-excerpt">「${escapeHtml(entry.source.excerpt)}」</div>`;
        }
        srcHtml += `</div>`;
        srcDiv.innerHTML = srcHtml;
        $modalContent.parentElement.appendChild(srcDiv);
      }
    } else {
      $modalTitle.textContent = "新增設定";
      $modalDelete.style.display = "none";
      populateCategoryDropdown(categories[0] || "其他");
      $modalSubcategory.value = "";
      $modalTopic.value = "";
      $modalContent.value = "";
    }
    $modal.classList.remove("hidden");
    ($modalTopic.value ? $modalContent : $modalTopic).focus();
  }

  function closeModal() {
    $modal.classList.add("hidden");
    editingEntry = null;
  }

  async function saveModal() {
    const category = $modalCategory.value;
    const subcategory = $modalSubcategory.value.trim();
    const topic = $modalTopic.value.trim();
    const content = $modalContent.value.trim();
    if (!topic) {
      $modalTopic.focus();
      return;
    }

    if (editingEntry) {
      if (editingEntry.layer === "branch") {
        // Branch entries are read-only in the modal (promote or delete only)
        alert("分支設定無法直接編輯。請使用「審核提升」功能。");
        return;
      }
      const updates = { category, subcategory, content };
      if (topic !== editingEntry.topic) updates.new_topic = topic;
      const res = await updateEntry(editingEntry.topic, editingEntry.subcategory, updates);
      if (!res.ok) {
        alert(res.error || "更新失敗");
        return;
      }
    } else {
      const res = await createEntry({ category, subcategory, topic, content });
      if (!res.ok) {
        alert(res.error || "新增失敗");
        return;
      }
    }

    closeModal();
    await refreshLoreList();
  }

  async function deleteFromModal() {
    if (!editingEntry) return;
    if (!confirm(`確定要刪除「${editingEntry.topic}」？`)) return;
    let res;
    if (editingEntry.layer === "branch") {
      res = await fetch("/api/lore/branch/entry", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic: editingEntry.topic, subcategory: editingEntry.subcategory || "", branch_id: activeBranchId }),
      }).then((r) => r.json());
    } else {
      res = await deleteEntry(editingEntry.topic, editingEntry.subcategory);
    }
    if (!res.ok) {
      alert(res.error || "刪除失敗");
      return;
    }
    const deletedTopic = editingEntry.topic;
    closeModal();
    selectedTopics.delete(deletedTopic);
    await refreshLoreList();
  }

  // ------------------------------------------------------------------
  // Refresh lore list
  // ------------------------------------------------------------------
  async function refreshLoreList() {
    await fetchLoreAll();
    // Prune stale checked/selected entries
    const existing = new Set(allEntries.map((e) => e.topic));
    for (const t of checkedTopics) {
      if (!existing.has(t)) checkedTopics.delete(t);
    }
    for (const t of selectedTopics) {
      if (!existing.has(t)) selectedTopics.delete(t);
    }
    updateBatchBar();
    renderLoreList($searchInput.value);
  }

  // ------------------------------------------------------------------
  // Branch lore promotion
  // ------------------------------------------------------------------
  async function reviewAndPromote() {
    if (isPromoting || !activeBranchId) return;
    const branchEntries = allEntries.filter((e) => e.layer === "branch");
    if (branchEntries.length === 0) return;

    isPromoting = true;
    // Show reviewing state in chat panel
    appendChatMessage("assistant", "正在審核分支知識...");

    try {
      const res = await fetch("/api/lore/promote/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ branch_id: activeBranchId }),
      });
      const data = await res.json();
      if (!data.ok) {
        appendChatMessage("assistant", `審核失敗：${data.error || "未知錯誤"}`);
        isPromoting = false;
        return;
      }

      const proposals = data.proposals || [];
      if (proposals.length === 0) {
        appendChatMessage("assistant", "沒有需要審核的分支設定。");
        isPromoting = false;
        return;
      }

      // Group proposals by action
      const promote = proposals.filter((p) => p.action === "promote");
      const rewrite = proposals.filter((p) => p.action === "rewrite");
      const reject = proposals.filter((p) => p.action === "reject");

      let summary = `## 審核結果\n`;
      summary += `- 可直接提升：${promote.length} 條\n`;
      summary += `- 需改寫提升：${rewrite.length} 條\n`;
      summary += `- 建議保留在分支：${reject.length} 條\n`;

      const { div: msgDiv } = appendChatMessage("assistant", summary);

      // Render proposal cards
      const cardsDiv = document.createElement("div");
      cardsDiv.className = "proposal-cards";
      for (const p of proposals) {
        cardsDiv.appendChild(createPromoteCard(p));
      }
      msgDiv.appendChild(cardsDiv);
    } catch (err) {
      appendChatMessage("assistant", `連線錯誤：${err.message}`);
    }
    isPromoting = false;
  }

  function createPromoteCard(proposal) {
    const card = document.createElement("div");
    card.className = "proposal-card";

    const actionLabels = { promote: "提升", rewrite: "改寫提升", reject: "保留分支" };
    const actionColors = { promote: "add", rewrite: "edit", reject: "delete" };
    const action = proposal.action || "reject";
    const badgeClass = actionColors[action] || "delete";
    const badgeText = actionLabels[action] || action;

    let html = `<div class="proposal-header">`;
    html += `<span class="proposal-badge ${escapeHtml(badgeClass)}">${escapeHtml(badgeText)}</span>`;
    html += `<span class="proposal-topic">${escapeHtml(proposal.topic || "")}</span>`;
    if (proposal.category) {
      html += ` <span style="color:var(--text-dim);font-size:0.8rem;">(${escapeHtml(proposal.category)})</span>`;
    }
    html += `</div>`;

    if (proposal.reason) {
      html += `<div class="proposal-reason" style="color:var(--text-dim);font-size:0.85rem;margin:4px 0;">${escapeHtml(proposal.reason)}</div>`;
    }

    // Show content (or rewritten content for rewrite action)
    const displayContent = action === "rewrite" && proposal.rewritten_content
      ? proposal.rewritten_content
      : proposal.content || "";
    if (displayContent) {
      const full = escapeHtml(displayContent);
      const short = escapeHtml(truncate(displayContent, 200));
      const needsExpand = displayContent.length > 200;
      html += `<div class="proposal-content">${needsExpand ? short : full}</div>`;
      if (needsExpand) {
        html += `<button class="proposal-expand-btn">展開全文</button>`;
      }
    }

    if (action !== "reject") {
      html += `<div class="proposal-actions">`;
      html += `<button class="btn-accept">採用</button>`;
      html += `<button class="btn-reject">忽略</button>`;
      html += `</div>`;
    } else {
      html += `<div class="proposal-actions">`;
      html += `<span style="color:var(--text-dim);font-size:0.8rem;">不適合提升</span>`;
      html += `</div>`;
    }

    card.innerHTML = html;

    // Expand/collapse
    const expandBtn = card.querySelector(".proposal-expand-btn");
    if (expandBtn) {
      let expanded = false;
      const contentDiv = card.querySelector(".proposal-content");
      const full = escapeHtml(displayContent);
      const short = escapeHtml(truncate(displayContent, 200));
      expandBtn.addEventListener("click", () => {
        expanded = !expanded;
        contentDiv.innerHTML = expanded ? full : short;
        expandBtn.textContent = expanded ? "收起" : "展開全文";
      });
    }

    // Accept: promote to base
    card.addEventListener("click", async (ev) => {
      if (!ev.target.closest(".btn-accept")) return;
      const actionsDiv = card.querySelector(".proposal-actions");
      actionsDiv.innerHTML = '<span style="color:var(--text-dim);font-size:0.8rem;">提升中...</span>';
      try {
        const body = {
          branch_id: activeBranchId,
          topic: proposal.topic,
          subcategory: proposal.subcategory || "",
        };
        // For rewrite, send the rewritten content
        if (action === "rewrite" && proposal.rewritten_content) {
          body.content = proposal.rewritten_content;
        }
        const res = await fetch("/api/lore/promote", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
          card.classList.add("applied");
          actionsDiv.innerHTML = '<span style="color:#7fdb96;font-size:0.8rem;">已提升</span>';
          await refreshLoreList();
        } else {
          actionsDiv.innerHTML =
            `<span style="color:#db7f7f;font-size:0.8rem;">${escapeHtml(data.error || "失敗")}</span> ` +
            '<button class="btn-accept" style="margin-left:8px;">重試</button>';
        }
      } catch {
        actionsDiv.innerHTML =
          '<span style="color:#db7f7f;font-size:0.8rem;">連線錯誤</span> ' +
          '<button class="btn-accept" style="margin-left:8px;">重試</button>';
      }
    });

    // Reject
    const rejectBtn = card.querySelector(".btn-reject");
    if (rejectBtn) {
      rejectBtn.addEventListener("click", () => {
        card.classList.add("rejected");
        card.querySelector(".proposal-actions").innerHTML =
          '<span style="color:var(--text-dim);font-size:0.8rem;">已忽略</span>';
      });
    }

    return card;
  }

  // ------------------------------------------------------------------
  // Chat
  // ------------------------------------------------------------------
  function appendChatMessage(role, content, proposals) {
    const div = document.createElement("div");
    div.className = `chat-msg ${role}`;

    const label = document.createElement("div");
    label.className = "chat-msg-label";
    label.textContent = role === "user" ? "你" : "AI 助手";
    div.appendChild(label);

    const body = document.createElement("div");
    body.className = "chat-msg-content";
    if (role === "assistant" && content) {
      body.innerHTML = renderMarkdown(content);
    } else {
      body.textContent = content;
    }
    div.appendChild(body);

    if (proposals && proposals.length > 0) {
      const cardsDiv = document.createElement("div");
      cardsDiv.className = "proposal-cards";
      for (let i = 0; i < proposals.length; i++) {
        cardsDiv.appendChild(createProposalCard(proposals[i], i));
      }
      div.appendChild(cardsDiv);
    }

    $chatMessages.appendChild(div);
    $chatMessages.scrollTop = $chatMessages.scrollHeight;
    return { div, body };
  }

  function createProposalCard(proposal, idx) {
    const card = document.createElement("div");
    card.className = "proposal-card";
    card.dataset.idx = idx;

    const actionLabels = { add: "新增", edit: "修改", delete: "刪除" };
    const action = (proposal.action || "add").toLowerCase();
    const badgeClass = action;
    const badgeText = actionLabels[action] || action;

    let html = `<div class="proposal-header">`;
    html += `<span class="proposal-badge ${escapeHtml(badgeClass)}">${escapeHtml(badgeText)}</span>`;
    html += `<span class="proposal-topic">${escapeHtml(proposal.topic || "")}</span>`;
    if (proposal.category) {
      html += ` <span style="color:var(--text-dim);font-size:0.8rem;">(${escapeHtml(proposal.category)})</span>`;
    }
    html += `</div>`;

    if (proposal.content && action !== "delete") {
      const full = escapeHtml(proposal.content);
      const short = escapeHtml(truncate(proposal.content, 200));
      const needsExpand = proposal.content.length > 200;
      html += `<div class="proposal-content">${needsExpand ? short : full}</div>`;
      if (needsExpand) {
        html += `<button class="proposal-expand-btn">展開全文</button>`;
      }
    }

    html += `<div class="proposal-actions">`;
    html += `<button class="btn-accept">採用</button>`;
    html += `<button class="btn-reject">忽略</button>`;
    html += `</div>`;

    card.innerHTML = html;

    // Expand/collapse full content
    const expandBtn = card.querySelector(".proposal-expand-btn");
    if (expandBtn) {
      let expanded = false;
      const contentDiv = card.querySelector(".proposal-content");
      const full = escapeHtml(proposal.content);
      const short = escapeHtml(truncate(proposal.content, 200));
      expandBtn.addEventListener("click", () => {
        expanded = !expanded;
        contentDiv.innerHTML = expanded ? full : short;
        expandBtn.textContent = expanded ? "收起" : "展開全文";
      });
    }

    // Accept with confirmation for delete — use event delegation so retry buttons work too
    card.addEventListener("click", async (ev) => {
      if (!ev.target.closest(".btn-accept")) return;
      if (action === "delete") {
        if (!confirm(`確定要刪除「${proposal.topic}」？`)) return;
      }
      const actionsDiv = card.querySelector(".proposal-actions");
      actionsDiv.innerHTML = '<span style="color:var(--text-dim);font-size:0.8rem;">套用中...</span>';
      try {
        const res = await applyProposals([proposal]);
        if (res.ok) {
          card.classList.add("applied");
          actionsDiv.innerHTML = '<span style="color:#7fdb96;font-size:0.8rem;">已採用</span>';
          await refreshLoreList();
        } else {
          actionsDiv.innerHTML =
            '<span style="color:#db7f7f;font-size:0.8rem;">套用失敗</span> ' +
            '<button class="btn-accept" style="margin-left:8px;">重試</button>';
        }
      } catch {
        actionsDiv.innerHTML =
          '<span style="color:#db7f7f;font-size:0.8rem;">連線錯誤</span> ' +
          '<button class="btn-accept" style="margin-left:8px;">重試</button>';
      }
    });

    card.querySelector(".btn-reject").addEventListener("click", () => {
      card.classList.add("rejected");
      card.querySelector(".proposal-actions").innerHTML =
        '<span style="color:var(--text-dim);font-size:0.8rem;">已忽略</span>';
    });

    return card;
  }

  /** Build a grounding section (search indicator + collapsible source list) */
  function buildGroundingSection(grounding) {
    if (!grounding || (!grounding.sources?.length && !grounding.searchQueries?.length)) {
      return null;
    }

    const section = document.createElement("div");
    section.className = "grounding-section";

    // Search indicator with inline queries (visible on mobile, not tooltip-only)
    const indicator = document.createElement("div");
    indicator.className = "grounding-indicator";
    const icon = document.createElement("span");
    icon.className = "grounding-icon";
    icon.textContent = "\uD83D\uDD0D";
    indicator.appendChild(icon);
    const label = document.createElement("span");
    label.textContent = " \u5DF2\u641C\u5C0B\u7DB2\u8DEF";
    indicator.appendChild(label);
    if (grounding.searchQueries && grounding.searchQueries.length > 0) {
      const queries = document.createElement("span");
      queries.className = "grounding-queries";
      queries.textContent = `\uFF1A${grounding.searchQueries.join("\u3001")}`;
      indicator.appendChild(queries);
    }
    section.appendChild(indicator);

    // Source list (collapsible)
    const sources = (grounding.sources || []).filter(
      (s) => /^https?:\/\//i.test(s.url)
    );
    if (sources.length > 0) {
      const toggle = document.createElement("button");
      toggle.className = "grounding-toggle";
      toggle.textContent = `\u4F86\u6E90 (${sources.length}) \u25B8`;
      toggle.setAttribute("aria-expanded", "false");
      section.appendChild(toggle);

      const list = document.createElement("div");
      list.className = "grounding-source-list collapsed";

      for (const src of sources) {
        const item = document.createElement("div");
        item.className = "grounding-source-item";
        const link = document.createElement("a");
        link.href = src.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = src.title || src.domain || src.url;
        item.appendChild(link);
        if (src.domain && src.title !== src.domain) {
          const domain = document.createElement("span");
          domain.className = "grounding-source-domain";
          domain.textContent = ` \u2014 ${src.domain}`;
          item.appendChild(domain);
        }
        list.appendChild(item);
      }

      section.appendChild(list);

      toggle.addEventListener("click", () => {
        const expanded = list.classList.toggle("collapsed");
        toggle.classList.toggle("expanded");
        const isExpanded = !list.classList.contains("collapsed");
        toggle.setAttribute("aria-expanded", isExpanded ? "true" : "false");
        toggle.textContent = isExpanded
          ? `\u4F86\u6E90 (${sources.length}) \u25BE`
          : `\u4F86\u6E90 (${sources.length}) \u25B8`;
      });
    }

    return section;
  }

  function finishStreaming() {
    isStreaming = false;
    streamAbortController = null;
    $chatSendBtn.disabled = false;
    $chatStopBtn.classList.add("hidden");
  }

  async function sendChat() {
    const text = $chatInput.value.trim();
    if (!text || isStreaming) return;

    // Trim chat history to last 40 messages to avoid unbounded growth
    if (chatMessages.length > 40) {
      chatMessages = chatMessages.slice(-40);
    }

    chatMessages.push({ role: "user", content: text });
    appendChatMessage("user", text);
    $chatInput.value = "";
    $chatInput.focus();
    isStreaming = true;
    $chatSendBtn.disabled = true;
    $chatStopBtn.classList.remove("hidden");
    streamAbortController = new AbortController();

    const { div: msgDiv, body: msgBody } = appendChatMessage("assistant", "");
    msgBody.innerHTML = '<span class="streaming-cursor"></span>';

    let fullText = "";
    let proposals = [];
    let grounding = null;
    let renderTimer = null;
    let aborted = false;

    try {
      const response = await fetch("/api/lore/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: chatMessages }),
        signal: streamAbortController.signal,
      });

      if (!response.ok) {
        fullText = `[伺服器錯誤: ${response.status}]`;
        msgBody.textContent = fullText;
        chatMessages.push({ role: "assistant", content: fullText });
        finishStreaming();
        return;
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          let event;
          try {
            event = JSON.parse(line.slice(6));
          } catch {
            continue;
          }

          if (event.type === "text") {
            fullText += event.chunk;
            if (!renderTimer) {
              renderTimer = setTimeout(() => {
                msgBody.innerHTML = renderMarkdown(fullText);
                const cursor = document.createElement("span");
                cursor.className = "streaming-cursor";
                const lastEl = msgBody.lastElementChild;
                if (lastEl) lastEl.appendChild(cursor);
                else msgBody.appendChild(cursor);
                $chatMessages.scrollTop = $chatMessages.scrollHeight;
                renderTimer = null;
              }, 120);
            }
          } else if (event.type === "done") {
            fullText = event.response || fullText;
            proposals = event.proposals || [];
            grounding = event.grounding || null;
          } else if (event.type === "error") {
            fullText += `\n[Error: ${event.message}]`;
          }
        }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        fullText += "\n[已中斷]";
      } else {
        fullText += `\n[連線錯誤: ${err.message}]`;
      }
      aborted = true;
    }

    // Finalize message with markdown
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    if (aborted) {
      msgBody.textContent = fullText;
    } else {
      msgBody.innerHTML = renderMarkdown(fullText);
    }

    if (grounding) {
      const groundingEl = buildGroundingSection(grounding);
      if (groundingEl) msgDiv.appendChild(groundingEl);
    }

    if (proposals.length > 0) {
      const cardsDiv = document.createElement("div");
      cardsDiv.className = "proposal-cards";
      for (let i = 0; i < proposals.length; i++) {
        cardsDiv.appendChild(createProposalCard(proposals[i], i));
      }
      msgDiv.appendChild(cardsDiv);
    }

    if (!aborted) {
      chatMessages.push({ role: "assistant", content: fullText });
    }
    finishStreaming();
    $chatMessages.scrollTop = $chatMessages.scrollHeight;
  }

  // ------------------------------------------------------------------
  // Mobile tabs
  // ------------------------------------------------------------------
  function setupMobileTabs() {
    const tabs = document.querySelectorAll(".mobile-tab");
    const listPanel = document.getElementById("lore-list-panel");
    const chatPanel = document.getElementById("lore-chat-panel");

    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        tabs.forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");

        if (tab.dataset.panel === "list") {
          listPanel.classList.remove("hidden-mobile");
          chatPanel.classList.add("hidden-mobile");
        } else {
          chatPanel.classList.remove("hidden-mobile");
          listPanel.classList.add("hidden-mobile");
        }
      });
    });
  }

  // ------------------------------------------------------------------
  // Fetch story name
  // ------------------------------------------------------------------
  async function fetchStoryName() {
    try {
      const res = await fetch("/api/stories");
      const data = await res.json();
      if (data.ok && data.active_story_id && data.stories) {
        const story = data.stories.find((s) => s.id === data.active_story_id);
        if (story) $storyName.textContent = story.name || story.id;
      }
    } catch {
      // ignore
    }
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------
  async function init() {
    await fetchLoreAll();
    renderLoreList();
    fetchStoryName();
    setupMobileTabs();

    // Platform-aware shortcut hint
    const mod = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";
    $chatInput.placeholder = `輸入訊息... (${mod}+Enter 送出)`;

    // Search
    $searchInput.addEventListener("input", () => {
      renderLoreList($searchInput.value);
    });

    // Add button
    $addBtn.addEventListener("click", () => openModal(null));

    // Toggle all expand/collapse
    const $toggleAllBtn = document.getElementById("lore-toggle-all-btn");
    function updateToggleBtn() {
      $toggleAllBtn.textContent = allCollapsed ? "\u25BC" : "\u25B2";
      $toggleAllBtn.title = allCollapsed ? "全部展開" : "全部收起";
    }
    updateToggleBtn();
    $toggleAllBtn.addEventListener("click", () => {
      allCollapsed = !allCollapsed;
      collapsedCats.clear();
      updateToggleBtn();
      renderLoreList($searchInput.value);
    });

    // Modal handlers
    $modalSave.addEventListener("click", saveModal);
    $modalCancel.addEventListener("click", closeModal);
    $modalClose.addEventListener("click", closeModal);
    $modalDelete.addEventListener("click", deleteFromModal);
    $modal.addEventListener("click", (ev) => {
      if (ev.target === $modal) closeModal();
    });

    // Escape key closes modal
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !$modal.classList.contains("hidden")) {
        closeModal();
      }
    });

    // Chat send — Cmd/Ctrl+Enter
    $chatSendBtn.addEventListener("click", sendChat);
    $chatInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
        ev.preventDefault();
        sendChat();
      }
    });

    // Stop button — abort streaming
    $chatStopBtn.addEventListener("click", () => {
      if (streamAbortController) streamAbortController.abort();
    });

    // Batch delete
    $batchDeleteBtn.addEventListener("click", async () => {
      if (checkedTopics.size === 0) return;
      const topics = [...checkedTopics];
      const preview = topics.length <= 5
        ? topics.map((t) => `「${t}」`).join("\n")
        : topics.slice(0, 5).map((t) => `「${t}」`).join("\n") + `\n...及其他 ${topics.length - 5} 項`;
      if (!confirm(`確定要刪除以下 ${topics.length} 個設定？\n\n${preview}`)) return;
      $batchDeleteBtn.disabled = true;
      $batchDeleteBtn.textContent = `刪除中 0/${topics.length}...`;
      const failures = [];
      for (let i = 0; i < topics.length; i++) {
        $batchDeleteBtn.textContent = `刪除中 ${i + 1}/${topics.length}...`;
        try {
          const entry = allEntries.find((e) => e.topic === topics[i]);
          let res;
          if (entry && entry.layer === "branch") {
            res = await fetch("/api/lore/branch/entry", {
              method: "DELETE",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ topic: topics[i], subcategory: entry.subcategory || "", branch_id: activeBranchId }),
            }).then((r) => r.json());
          } else {
            res = await deleteEntry(topics[i], entry ? entry.subcategory : "");
          }
          if (res.ok) {
            checkedTopics.delete(topics[i]);
            selectedTopics.delete(topics[i]);
          } else {
            failures.push(topics[i]);
          }
        } catch {
          failures.push(topics[i]);
        }
      }
      $batchDeleteBtn.disabled = false;
      $batchDeleteBtn.textContent = "刪除所選";
      if (failures.length > 0) {
        alert(`以下項目刪除失敗：${failures.join("、")}`);
      }
      updateBatchBar();
      await refreshLoreList();
    });

    $batchClearBtn.addEventListener("click", () => {
      checkedTopics.clear();
      updateBatchBar();
      renderLoreList($searchInput.value);
    });
  }

  init();
})();
